import hashlib
import http.server
import json
import stat
import threading

import pytest
from minisign_helper import keypair, sign

from smartheat_host import bundles, device_api, updater

COMPOSE = ("services:\n  agent:\n    image: ghcr.io/x/gw@sha256:" + "a" * 64 + "\n").encode()
MOSQ = b"listener 1883\n"
BASE = "https://release.example.test/0.3.0/"


class FakeApi:
    """offline=True: weder desired noch update_result erreichen den Server (Netz weg, z. B. Router nach Stromausfall)."""

    def __init__(self, desired):
        self.desired_answer, self.results, self.offline, self.desired_calls = desired, [], False, 0

    def desired(self):
        self.desired_calls += 1
        if self.offline:
            raise device_api.DeviceApiError("desired: URLError")
        if isinstance(self.desired_answer, Exception):
            raise self.desired_answer
        return self.desired_answer

    def update_result(self, version, result, reason):
        if self.offline:
            raise device_api.DeviceApiError("update_result: URLError")
        self.results.append((version, result, reason))


class FakeCompose:
    def __init__(self, fail_up=(), fail_pull=False):
        self.calls, self.fail_up, self.fail_pull = [], set(fail_up), fail_pull
        self.on_up = None

    def pull(self, version):
        self.calls.append(("pull", version))
        if self.fail_pull:
            raise bundles.ComposeError("kein Netz")

    def up(self, version):
        self.calls.append(("up", version))
        if self.on_up:
            self.on_up(version)
        if version in self.fail_up:
            raise bundles.ComposeError("boom")

    def prune(self, keep):
        self.calls.append(("prune", frozenset(keep)))


class World:
    def __init__(self, tmp_path, *, manifest_over=None, compose=COMPOSE, healthy=True, version="0.2.0",
                 url=BASE + "manifest.json", tamper=False, fail_up=(), fail_pull=False, health_seconds=None):
        self.secret, self.key_id, self.public = keypair()
        files = {"docker-compose.yml": compose, "mosquitto.conf": MOSQ}
        manifest = {"version": "0.3.0", "min_updater_version": "0.2.0",
                    "files": {n: hashlib.sha256(d).hexdigest() for n, d in files.items()},
                    "images": {"gateway": "ghcr.io/x/gw@sha256:" + "a" * 64}}
        manifest.update(manifest_over or {})
        raw = json.dumps(manifest).encode()
        self.served = {BASE + "manifest.json": raw, **{BASE + n: d for n, d in files.items()}}
        if tamper:
            self.served[BASE + "docker-compose.yml"] = compose + b"#x\n"
        self.api = FakeApi({"version": "0.3.0", "manifest_url": url,
                            "manifest_sha256": hashlib.sha256(raw).hexdigest(),
                            "signature": sign(self.secret, self.key_id, raw)})
        self.compose = FakeCompose(fail_up, fail_pull)
        self.store = bundles.BundleStore(tmp_path)
        self.store.install("0.2.0", {"docker-compose.yml": COMPOSE, "mosquitto.conf": MOSQ})  # laufendes Bundle
        self.state_path = tmp_path / "updater" / "state.json"
        updater.State(current="0.2.0").save(self.state_path)
        self.healthy = healthy
        self.clock = [0.0]
        self.extra = {} if health_seconds is None else {"health_seconds": health_seconds}
        self.context = {"updater": version, "key": self.key_id.hex()}
        self.updater = self.rebuild(version=version)

    def rebuild(self, *, version="0.2.0", public_key_text=None):
        """Neuer Updater ueber derselben Zustandsdatei (Neustart, neue Host-Version, neuer Schluessel)."""
        return updater.Updater(
            api_factory=lambda: self.api, store=self.store, compose=self.compose, fetch=self.fetch,
            public_key_text=public_key_text or self.public, state_path=self.state_path, version=version,
            health=lambda version: self.healthy, clock=lambda: self.clock[0], sleep=self.sleep, **self.extra,
        )

    def fetch(self, url, max_bytes):
        if url not in self.served:
            raise updater.FetchError(url)
        return self.served[url]

    def sleep(self, seconds):
        self.clock[0] += seconds

    def state(self):
        return updater.State.load(self.state_path)


def test_healthy_update(tmp_path):
    world = World(tmp_path)
    assert world.updater.run_once() == "aktualisiert"
    state = world.state()
    assert (state.current, state.previous, state.in_progress) == ("0.3.0", "0.2.0", None)
    assert world.api.results == [("0.3.0", "ok", "")]
    assert ("pull", "0.3.0") in world.compose.calls and ("up", "0.3.0") in world.compose.calls
    assert world.store.exists("0.3.0")
    assert world.compose.calls[-1] == ("prune", frozenset({"ghcr.io/x/gw@sha256:" + "a" * 64}))


def test_state_file_is_world_readable_and_survives_load(tmp_path):
    world = World(tmp_path)
    world.updater.run_once()
    assert stat.S_IMODE(world.state_path.stat().st_mode) == 0o644
    assert json.loads(world.state_path.read_text()) == {
        "current": "0.3.0", "previous": "0.2.0", "rejected": [], "in_progress": None,
        "rejected_context": {"updater": "0.2.0", "key": world.key_id.hex()}, "pending_reports": []}


def test_in_progress_is_persisted_before_the_switch(tmp_path):
    """Stromausfall waehrend des Umschaltens: die Datei muss schon vor compose up den Auftrag tragen."""
    world = World(tmp_path)
    seen = []
    world.compose.on_up = lambda version: seen.append(world.state().in_progress) if version == "0.3.0" else None
    world.updater.run_once()
    assert seen == [{"version": "0.3.0", "previous": "0.2.0", "since": seen[0]["since"]}]
    assert isinstance(seen[0]["since"], float)


def test_unhealthy_update_rolls_back_within_ten_minutes(tmp_path):
    world = World(tmp_path, healthy=False)
    assert world.updater.run_once() == "zurueckgerollt"
    assert world.compose.calls[-1] == ("up", "0.2.0")
    assert world.clock[0] >= updater.HEALTH_SECONDS  # Standardfrist 10 min
    assert world.clock[0] < updater.HEALTH_SECONDS + updater.HEALTH_POLL_SECONDS
    state = world.state()
    assert (state.current, state.rejected, state.in_progress) == ("0.2.0", ["0.3.0"], None)
    assert world.api.results == [("0.3.0", "rollback", "ungesund")]
    assert world.updater.run_once() == "aktuell"  # abgelehnt bleibt abgelehnt


def test_health_deadline_is_configurable(tmp_path):
    world = World(tmp_path, healthy=False, health_seconds=30)
    assert world.updater.run_once() == "zurueckgerollt"
    assert 30 <= world.clock[0] < 30 + updater.HEALTH_POLL_SECONDS


def test_health_that_arrives_late_is_accepted(tmp_path):
    world = World(tmp_path, healthy=False)
    original_sleep = world.sleep

    def sleep(seconds):
        original_sleep(seconds)
        world.healthy = world.clock[0] >= 100

    world.updater._sleep = sleep
    assert world.updater.run_once() == "aktualisiert"


def test_start_failure_rolls_back(tmp_path):
    world = World(tmp_path, fail_up={"0.3.0"})
    assert world.updater.run_once() == "zurueckgerollt"
    assert world.api.results == [("0.3.0", "rollback", "start_fehlgeschlagen")]
    assert world.compose.calls[-1] == ("up", "0.2.0") and world.clock[0] == 0.0  # kein Warten auf Gesundheit
    assert world.state().current == "0.2.0"


@pytest.mark.parametrize(("kwargs", "reason"), [
    ({"tamper": True}, "manifest_ungueltig"),
    ({"manifest_over": {"version": "0.4.0"}}, "manifest_ungueltig"),
    ({"url": "http://release.example.test/0.3.0/manifest.json"}, "manifest_ungueltig"),
    ({"manifest_over": {"min_updater_version": "9.0.0"}}, "updater_zu_alt"),
    ({"compose": b"services:\n  agent:\n    image: ghcr.io/x/gw:latest\n"}, "manifest_ungueltig"),
])
def test_refused_bundles_are_never_started(tmp_path, kwargs, reason):
    world = World(tmp_path, **kwargs)
    if "url" in kwargs:  # http-URL: der Fetcher wuerde liefern, der Updater darf trotzdem nicht laden
        world.served[kwargs["url"]] = world.served[BASE + "manifest.json"]
    assert world.updater.run_once() == "abgelehnt"
    assert not any(call[0] in ("up", "pull") for call in world.compose.calls)
    assert not world.store.exists("0.3.0")  # nichts davon landet im Bundle-Ordner
    assert world.api.results == [("0.3.0", "rollback", reason)]
    assert world.state().current == "0.2.0" and world.state().rejected == ["0.3.0"]


def test_manifest_with_wrong_sha_is_refused(tmp_path):
    world = World(tmp_path)
    world.api.desired_answer["manifest_sha256"] = "0" * 64
    assert world.updater.run_once() == "abgelehnt"
    assert world.api.results == [("0.3.0", "rollback", "manifest_ungueltig")]


def test_http_manifest_is_loaded_when_allowed(tmp_path):
    url = "http://release.example.test/0.3.0/manifest.json"
    world = World(tmp_path, url=url)
    world.served[url] = world.served[BASE + "manifest.json"]
    for name in bundles.MANIFEST_FILES:
        world.served["http://release.example.test/0.3.0/" + name] = world.served[BASE + name]
    world.updater._allow_http = True
    assert world.updater.run_once() == "aktualisiert"


def test_wrong_signature_is_refused(tmp_path):
    world = World(tmp_path)
    other_secret, other_id, _ = keypair()
    world.api.desired_answer["signature"] = sign(other_secret, other_id, world.served[BASE + "manifest.json"])
    assert world.updater.run_once() == "abgelehnt"
    assert world.api.results == [("0.3.0", "rollback", "signatur_ungueltig")]
    assert not any(call[0] == "up" for call in world.compose.calls)


def test_placeholder_release_key_refuses_every_update(tmp_path):
    world = World(tmp_path)
    world.updater = updater.Updater(
        api_factory=lambda: world.api, store=world.store, compose=world.compose, fetch=world.fetch,
        public_key_text="untrusted comment: SMARTHEAT-PLATZHALTER\nSMARTHEAT-PLATZHALTER\n",
        state_path=world.state_path, version="0.2.0", health=lambda version: True, clock=lambda: 0.0,
        sleep=world.sleep)
    assert world.updater.run_once() == "abgelehnt"
    assert world.api.results == [("0.3.0", "rollback", "signatur_ungueltig")]


@pytest.mark.parametrize("error", [device_api.DeviceApiError("x"), device_api.NotAuthenticated("desired")])
def test_server_trouble_is_temporary(tmp_path, error):
    world = World(tmp_path)
    world.api.desired_answer = error
    assert world.updater.run_once() == "vorlaeufig"
    assert world.state().rejected == [] and world.api.results == []


def test_missing_device_key_is_temporary(tmp_path):
    world = World(tmp_path)

    def no_key():
        raise device_api.DeviceApiError("Geraeteschluessel unlesbar")

    world.updater._api_factory = no_key
    assert world.updater.run_once() == "vorlaeufig"


def test_missing_download_is_temporary(tmp_path):
    world = World(tmp_path)
    del world.served[BASE + "mosquitto.conf"]
    assert world.updater.run_once() == "vorlaeufig"
    assert world.state().rejected == []
    assert not world.store.exists("0.3.0") and not (tmp_path / "bundles" / "0.3.0").exists()  # kein halbes Bundle


def test_failed_pull_is_temporary_and_leaves_no_in_progress(tmp_path):
    world = World(tmp_path, fail_pull=True)
    assert world.updater.run_once() == "vorlaeufig"
    state = world.state()
    assert (state.current, state.rejected, state.in_progress) == ("0.2.0", [], None)
    assert not any(call[0] == "up" for call in world.compose.calls) and world.api.results == []


def test_no_update_offered(tmp_path):
    world = World(tmp_path)
    world.api.desired_answer = {"version": "0.2.0"}
    assert world.updater.run_once() == "aktuell"
    world.api.desired_answer = {}
    assert world.updater.run_once() == "aktuell"


def test_rejected_list_is_capped(tmp_path):
    world = World(tmp_path)
    updater.State(current="0.2.0", rejected=[f"0.1.{i}" for i in range(updater.MAX_REJECTED)],
                  rejected_context=world.context).save(world.state_path)
    world.api.desired_answer["signature"] = "kaputt"
    assert world.updater.run_once() == "abgelehnt"
    rejected = world.state().rejected
    assert len(rejected) == updater.MAX_REJECTED and rejected[-1] == "0.3.0" and "0.1.0" not in rejected


def test_recover_finishes_or_rolls_back_an_interrupted_update(tmp_path):
    world = World(tmp_path)
    world.store.install("0.3.0", {"docker-compose.yml": COMPOSE, "mosquitto.conf": MOSQ})  # 0.2.0 legt World an
    updater.State(current="0.2.0", in_progress={"version": "0.3.0", "previous": "0.2.0", "since": 0.0}).save(
        world.state_path)
    world.healthy = False
    world.updater.recover()
    state = world.state()
    assert (state.current, state.in_progress, state.rejected) == ("0.2.0", None, ["0.3.0"])
    assert world.compose.calls[-1] == ("up", "0.2.0")
    assert world.api.results == [("0.3.0", "rollback", "ungesund")]


def _interrupted(world, previous="0.2.0", **extra):
    world.store.install("0.3.0", {"docker-compose.yml": COMPOSE, "mosquitto.conf": MOSQ})
    updater.State(current=previous, rejected_context=world.context,
                  in_progress={"version": "0.3.0", "previous": previous, "since": 0.0, **extra}).save(world.state_path)


def test_recover_restarts_the_new_bundle_before_trusting_health(tmp_path):
    """Stromausfall vor/waehrend up: die alten Container kommen zurueck und wirken gesund (healthy=True), das neue
    Bundle muss trotzdem hochgefahren werden, sonst liefe nie das, was als current gilt."""
    world = World(tmp_path)
    _interrupted(world)
    world.updater.recover()
    state = world.state()
    assert (state.current, state.previous, state.in_progress) == ("0.3.0", "0.2.0", None)
    assert world.api.results == [("0.3.0", "ok", "")]
    assert world.compose.calls[0] == ("up", "0.3.0")


def test_recover_rolls_back_when_the_restart_fails(tmp_path):
    world = World(tmp_path, fail_up={"0.3.0"})
    _interrupted(world)
    world.updater.recover()
    state = world.state()
    assert (state.current, state.in_progress, state.rejected) == ("0.2.0", None, ["0.3.0"])
    assert world.compose.calls == [("up", "0.3.0"), ("up", "0.2.0")]
    assert world.api.results == [("0.3.0", "rollback", "start_fehlgeschlagen")]


def test_failed_rollback_is_kept_and_retried_before_anything_else(tmp_path):
    world = World(tmp_path, healthy=False, fail_up={"0.3.0", "0.2.0"})
    assert world.updater.run_once() == "vorlaeufig"
    state = world.state()
    assert state.in_progress == {"version": "0.3.0", "previous": "0.2.0", "since": state.in_progress["since"],
                                 "rollback": "start_fehlgeschlagen"}
    assert (state.current, state.rejected) == ("0.2.0", []) and world.api.results == []  # noch nichts gemeldet
    # naechster Durchlauf: Rueckweg wird zuerst wiederholt, weiter scheitert er -> kein Abruf, kein Bericht
    world.api.desired_answer = RuntimeError("darf nicht abgefragt werden")
    assert world.updater.run_once() == "vorlaeufig"
    assert world.compose.calls[-1] == ("up", "0.2.0") and world.state().in_progress is not None
    # Rueckweg gelingt: abgelehnt, genau einmal gemeldet
    world.compose.fail_up.clear()
    world.api.desired_answer = {"version": "0.2.0"}
    assert world.updater.run_once() == "aktuell"
    state = world.state()
    assert (state.current, state.in_progress, state.rejected) == ("0.2.0", None, ["0.3.0"])
    assert world.api.results == [("0.3.0", "rollback", "start_fehlgeschlagen")]


def test_pending_rollback_survives_a_restart(tmp_path):
    world = World(tmp_path)
    _interrupted(world, rollback="ungesund")
    world.rebuild().recover()
    state = world.state()
    assert (state.current, state.in_progress, state.rejected) == ("0.2.0", None, ["0.3.0"])
    assert world.compose.calls == [("up", "0.2.0")]  # kein erneuter Start des abgelehnten Bundles
    assert world.api.results == [("0.3.0", "rollback", "ungesund")]


def test_rollback_without_a_previous_bundle_is_logged_and_reported(tmp_path, caplog):
    world = World(tmp_path, healthy=False)
    _interrupted(world, previous=None)
    updater.State(current=None, rejected_context=world.context,
                  in_progress={"version": "0.3.0", "previous": None, "since": 0.0}).save(world.state_path)
    world.updater.recover()
    state = world.state()
    assert (state.current, state.in_progress, state.rejected) == (None, None, ["0.3.0"])
    assert world.compose.calls == [("up", "0.3.0")]  # kein Rueckweg-up
    assert world.api.results == [("0.3.0", "rollback", "ungesund")]
    assert "Kein Rueckweg moeglich" in caplog.text


def _reject_once(world):
    world.api.desired_answer["signature"] = "kaputt"
    assert world.updater.run_once() == "abgelehnt"
    assert world.state().rejected == ["0.3.0"]


def test_rejected_is_kept_for_the_same_updater_and_key(tmp_path):
    world = World(tmp_path)
    _reject_once(world)
    assert world.rebuild().run_once() == "aktuell"
    assert world.state().rejected == ["0.3.0"]


def test_rejected_is_cleared_after_an_updater_upgrade(tmp_path):
    world = World(tmp_path, manifest_over={"min_updater_version": "0.3.0"})
    assert world.updater.run_once() == "abgelehnt"
    assert world.api.results == [("0.3.0", "rollback", "updater_zu_alt")]
    assert world.rebuild().run_once() == "aktuell"
    assert world.rebuild(version="0.3.0").run_once() == "aktualisiert"
    assert world.state().rejected == []


def test_rejected_is_cleared_after_a_release_key_change(tmp_path):
    world = World(tmp_path)
    placeholder = "untrusted comment: SMARTHEAT-PLATZHALTER\nSMARTHEAT-PLATZHALTER\n"
    placeholder_updater = world.rebuild(public_key_text=placeholder)
    assert placeholder_updater.run_once() == "abgelehnt"
    assert world.state().rejected_context == {"updater": "0.2.0", "key": "placeholder"}
    assert world.rebuild(public_key_text=placeholder).run_once() == "aktuell"  # gleicher Schluessel: bleibt abgelehnt
    assert world.rebuild().run_once() == "aktualisiert"  # echter Schluessel: neuer Versuch
    assert world.state().rejected_context == world.context


def test_state_from_an_older_file_without_context_loads(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"current": "0.2.0", "previous": None, "rejected": ["0.3.0"], "in_progress": None}))
    state = updater.State.load(path)
    assert (state.rejected, state.rejected_context) == (["0.3.0"], None)


def test_recover_without_interrupted_update_does_nothing(tmp_path):
    world = World(tmp_path)
    world.updater.recover()
    assert world.compose.calls == [] and world.api.results == []


def test_corrupt_state_counts_as_empty(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{kaputt")
    assert updater.State.load(path) == updater.State()
    path.write_text("[1, 2]")
    assert updater.State.load(path) == updater.State()
    assert updater.State.load(tmp_path / "fehlt.json") == updater.State()


def test_state_without_a_usable_in_progress_entry_is_cleaned(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"current": "0.2.0", "in_progress": {"previous": "0.1.0"}}))
    assert updater.State.load(path).in_progress is None


# --- Umgebung statt Bundle: Server nicht erreichbar, Stick fehlt (Final-Review FW-1, FW-2) ---


def _offline_until(world, seconds, healthy_from=None):
    """Netz weg bis Uhrzeit `seconds` (monoton), gesund ab `healthy_from` (None: nie)."""
    original_sleep = world.sleep

    def sleep(step):
        original_sleep(step)
        world.api.offline = world.clock[0] < seconds
        world.healthy = healthy_from is not None and world.clock[0] >= healthy_from

    world.updater._sleep = sleep


def test_health_countdown_pauses_while_the_server_is_unreachable(tmp_path):
    """Stromausfall im ganzen Haus: der Router kommt nach dem Pi. Solange der Host die Geraete-API nicht erreicht, zaehlt
    die Frist nicht; wird das Bundle danach gesund, ist das Update gelungen."""
    world = World(tmp_path, healthy=False)
    _offline_until(world, 1500, healthy_from=1700)
    world.api.offline = False  # desired des Durchlaufs selbst geht noch durch
    assert world.updater.run_once() == "aktualisiert"
    assert world.clock[0] >= 1700 > updater.HEALTH_SECONDS
    state = world.state()
    assert (state.current, state.rejected, state.pending_reports) == ("0.3.0", [], [])
    assert world.api.results == [("0.3.0", "ok", "")]


def test_reachable_but_unhealthy_still_rolls_back_after_ten_minutes(tmp_path):
    world = World(tmp_path, healthy=False)
    _offline_until(world, 0)
    assert world.updater.run_once() == "zurueckgerollt"
    assert updater.HEALTH_SECONDS <= world.clock[0] < updater.HEALTH_SECONDS + updater.HEALTH_POLL_SECONDS
    assert world.state().rejected == ["0.3.0"]
    assert world.api.desired_calls > 1  # Erreichbarkeit wird je Takt geprueft


def test_unreachable_until_the_cap_rolls_back_without_rejecting_and_keeps_the_report(tmp_path):
    world = World(tmp_path, healthy=False)
    _offline_until(world, 10 ** 9)
    assert world.updater.run_once() == "zurueckgerollt"
    assert updater.HEALTH_CAP_SECONDS <= world.clock[0] < updater.HEALTH_CAP_SECONDS + updater.HEALTH_POLL_SECONDS
    assert world.compose.calls[-1] == ("up", "0.2.0")
    state = world.state()
    assert (state.current, state.rejected, state.in_progress) == ("0.2.0", [], None)  # nicht abgelehnt
    assert world.api.results == []  # offline: nichts gemeldet ...
    assert state.pending_reports == [{"version": "0.3.0", "result": "rollback", "reason": "ungesund"}]  # ... gemerkt
    # Netz wieder da: zuerst die offene Meldung, dann der neue Versuch derselben Version
    world.api.offline, world.healthy = False, True
    world.updater._sleep = world.sleep
    assert world.rebuild().run_once() == "aktualisiert"
    assert world.api.results == [("0.3.0", "rollback", "ungesund"), ("0.3.0", "ok", "")]
    assert world.state().pending_reports == []


def test_cap_rollback_that_fails_keeps_the_no_reject_marker(tmp_path):
    world = World(tmp_path, healthy=False, fail_up={"0.2.0"})
    _offline_until(world, 10 ** 9)
    assert world.updater.run_once() == "vorlaeufig"
    assert world.state().in_progress["rollback"] == "ungesund"
    world.compose.fail_up.clear()
    world.updater.recover()
    state = world.state()
    assert (state.current, state.rejected, state.in_progress) == ("0.2.0", [], None)


def test_report_that_cannot_be_sent_is_kept_and_sent_first_next_run(tmp_path):
    world = World(tmp_path)
    world.api.desired_answer["signature"] = "kaputt"
    original = world.api.update_result

    def broken(*args):
        raise device_api.NotAuthenticated("update_result")

    world.api.update_result = broken
    assert world.updater.run_once() == "abgelehnt"
    assert world.state().pending_reports == [{"version": "0.3.0", "result": "rollback", "reason": "signatur_ungueltig"}]
    world.api.update_result = original
    world.api.desired_answer = {"version": "0.2.0"}
    assert world.rebuild().run_once() == "aktuell"
    assert world.api.results == [("0.3.0", "rollback", "signatur_ungueltig")]
    assert world.state().pending_reports == []


def test_pending_reports_go_out_before_recover_reports(tmp_path):
    world = World(tmp_path)
    _interrupted(world)
    state = world.state()
    state.pending_reports = [{"version": "0.2.9", "result": "rollback", "reason": "ungesund"}]
    state.save(world.state_path)
    world.updater.recover()
    assert world.api.results == [("0.2.9", "rollback", "ungesund"), ("0.3.0", "ok", "")]


def test_pending_reports_keep_their_order_while_offline(tmp_path):
    world = World(tmp_path)
    updater.State(current="0.2.0", rejected_context=world.context, pending_reports=[
        {"version": "0.2.8", "result": "rollback", "reason": "ungesund"},
        {"version": "0.2.9", "result": "ok", "reason": ""}]).save(world.state_path)
    world.api.offline = True
    assert world.updater.run_once() == "vorlaeufig"
    assert len(world.state().pending_reports) == 2
    world.api.offline = False
    world.api.desired_answer = {"version": "0.2.0"}
    world.updater.run_once()
    assert world.api.results == [("0.2.8", "rollback", "ungesund"), ("0.2.9", "ok", "")]


def test_unusable_pending_reports_are_dropped_on_load(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"current": "0.2.0", "pending_reports": [
        {"version": "0.2.9", "result": "ok", "reason": ""}, {"version": 1}, "x", {"result": "ok"}]}))
    assert updater.State.load(path).pending_reports == [{"version": "0.2.9", "result": "ok", "reason": ""}]
    path.write_text(json.dumps({"current": "0.2.0", "pending_reports": "kaputt"}))
    assert updater.State.load(path).pending_reports == []


def _devices_compose(device: str) -> bytes:
    return (COMPOSE.decode() + f"  zigbee2mqtt:\n    image: koenkk/z2m@sha256:{'b' * 64}\n    devices:\n"
            f"    - {device}:/dev/zigbee\n").encode()


def test_missing_device_of_the_target_bundle_is_temporary(tmp_path):
    """Zigbee-Stick abgezogen: `up` wuerde scheitern und der Rueckweg ebenso - also gar nicht erst umschalten."""
    stick = tmp_path / "zigbee"
    world = World(tmp_path, compose=_devices_compose(str(stick)))
    assert world.updater.run_once() == "vorlaeufig"
    state = world.state()
    assert (state.current, state.rejected, state.in_progress) == ("0.2.0", [], None)
    assert not any(call[0] in ("pull", "up") for call in world.compose.calls) and world.api.results == []
    stick.touch()  # Stick wieder da
    assert world.updater.run_once() == "aktualisiert"


def test_recover_waits_for_a_missing_device_instead_of_rolling_back(tmp_path):
    stick = tmp_path / "zigbee"
    world = World(tmp_path)
    world.store.install("0.3.0", {"docker-compose.yml": _devices_compose(str(stick)), "mosquitto.conf": MOSQ})
    updater.State(current="0.2.0", rejected_context=world.context,
                  in_progress={"version": "0.3.0", "previous": "0.2.0", "since": 0.0}).save(world.state_path)
    world.updater.recover()
    assert world.compose.calls == [] and world.state().in_progress is not None
    assert world.updater.run_once() == "vorlaeufig"
    stick.touch()
    world.updater.recover()
    assert world.state().current == "0.3.0" and world.state().in_progress is None


# --- Felder von desired (FW-5, FW-6) ---


@pytest.mark.parametrize("answer", [{"version": 3}, {"version": None}, {"version": ["0.3.0"]}, {"manifest_url": "x"}])
def test_missing_or_non_text_version_counts_as_current(tmp_path, answer):
    world = World(tmp_path)
    world.api.desired_answer = answer
    assert world.updater.run_once() == "aktuell"
    assert world.api.results == [] and world.state().rejected == []


@pytest.mark.parametrize(("field", "value", "reason"), [
    ("manifest_url", 42, "manifest_ungueltig"),
    ("manifest_url", None, "manifest_ungueltig"),
    ("manifest_url", "https://[kaputt/manifest.json", "manifest_ungueltig"),
    ("manifest_url", "https:///manifest.json", "manifest_ungueltig"),
    ("manifest_sha256", 7, "manifest_ungueltig"),
    ("manifest_sha256", "abc", "manifest_ungueltig"),
    ("manifest_sha256", None, "manifest_ungueltig"),
    ("signature", {"x": 1}, "signatur_ungueltig"),
    ("signature", None, "signatur_ungueltig"),
])
def test_non_text_or_malformed_offer_fields_are_refused(tmp_path, field, value, reason):
    world = World(tmp_path)
    world.api.desired_answer[field] = value
    assert world.updater.run_once() == "abgelehnt"
    assert world.api.results == [("0.3.0", "rollback", reason)]
    assert not any(call[0] in ("pull", "up") for call in world.compose.calls)


def test_manifest_sha_is_compared_case_insensitively(tmp_path):
    world = World(tmp_path)
    world.api.desired_answer["manifest_sha256"] = world.api.desired_answer["manifest_sha256"].upper()
    assert world.updater.run_once() == "aktualisiert"


# --- Bundle-Ordner (FW-9, Spec 2.2) ---


def test_bundle_dir_keeps_manifest_and_signature(tmp_path):
    world = World(tmp_path)
    assert world.updater.run_once() == "aktualisiert"
    target = world.store.dir("0.3.0")
    assert (target / "manifest.json").read_bytes() == world.served[BASE + "manifest.json"]
    assert (target / "manifest.json.minisig").read_text() == world.api.desired_answer["signature"]
    assert sorted(p.name for p in target.iterdir()) == [
        "docker-compose.yml", "manifest.json", "manifest.json.minisig", "mosquitto.conf"]


HEALTHY = {"agent": "healthy", "runtime": "healthy"}


@pytest.mark.parametrize(("data", "containers", "ok"), [
    ({"server_ok": True, "runtime_status": "regelt"}, HEALTHY, True),
    ({"server_ok": True, "runtime_status": "notbetrieb"}, HEALTHY, True),
    ({"server_ok": True, "runtime_status": None}, HEALTHY, True),       # nicht eingerichtet: Status abgeraeumt
    ({"server_ok": True, "runtime_status": "startet"}, HEALTHY, False),
    ({"server_ok": False, "runtime_status": "regelt"}, HEALTHY, False),
    ({"server_ok": None, "runtime_status": "regelt"}, HEALTHY, False),
    ({"runtime_status": "regelt"}, HEALTHY, False),
    ({"server_ok": True, "runtime_status": "regelt"}, {"agent": "healthy", "runtime": "unhealthy"}, False),
    ({"server_ok": True, "runtime_status": "regelt"}, {"agent": "healthy", "runtime": "starting"}, False),
    ({"server_ok": True, "runtime_status": "regelt"}, {"agent": "unhealthy", "runtime": "healthy"}, False),
    ({"server_ok": True, "runtime_status": "regelt"}, {"agent": "healthy"}, False),  # Laufzeit fehlt/startet neu
    ({"server_ok": True, "runtime_status": "regelt"}, {}, False),
    (None, HEALTHY, False),
])
def test_health_rule(data, containers, ok):
    assert updater.health_ok(data, containers) is ok


def test_fetch_refuses_http_unless_allowed():
    with pytest.raises(updater.FetchError, match="https"):
        updater.fetch_url("http://example.test/x", 10, allow_http=False)
    with pytest.raises(updater.FetchError, match="https"):
        updater.fetch_url("file:///etc/passwd", 10, allow_http=True)


@pytest.fixture
def local_server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"server_ok": true, "runtime_status": null}' if self.path == "/healthz" else b"x" * 100
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join()


def test_fetch_url_limits_size_and_reads_when_allowed(local_server):
    assert updater.fetch_url(local_server + "/file", 100, allow_http=True) == b"x" * 100
    with pytest.raises(updater.FetchError, match="gross"):
        updater.fetch_url(local_server + "/file", 99, allow_http=True)


def test_fetch_url_network_errors_are_fetch_errors():
    with pytest.raises(updater.FetchError):
        updater.fetch_url("http://127.0.0.1:1/x", 10, allow_http=True)


def test_fetch_health(local_server):
    assert updater.fetch_health(local_server + "/healthz") == {"server_ok": True, "runtime_status": None}
    assert updater.fetch_health("http://127.0.0.1:1/healthz") is None


def test_main_once_wires_the_environment(tmp_path, monkeypatch, capsys):
    release_pub = tmp_path / "release.pub"
    release_pub.write_text("pub")
    monkeypatch.setenv("SHG_ROOT", str(tmp_path))
    monkeypatch.setenv("SHG_DEVICE_API_URL", "https://api.example.test")
    monkeypatch.setenv("SHG_RELEASE_PUB", str(release_pub))
    monkeypatch.setenv("SHG_UPDATER_ALLOW_HTTP", "1")
    monkeypatch.setenv("SHG_UPDATER_HEALTH_SECONDS", "5")
    monkeypatch.setenv("SHG_UPDATER_HEALTH_URL", "http://127.0.0.1:9/healthz")
    monkeypatch.setenv("SHG_COMPOSE_PROJECT", "testprojekt")
    seen = {}

    class FakeUpdater:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def recover(self):
            seen["recovered"] = True

        def run_once(self):
            return "aktuell"

    monkeypatch.setattr(updater, "Updater", FakeUpdater)
    updater.main(["--once"])
    assert capsys.readouterr().out == "aktuell\n"
    assert seen["recovered"] and seen["allow_http"] is True and seen["health_seconds"] == 5.0
    assert seen["public_key_text"] == "pub" and seen["state_path"] == tmp_path / "updater" / "state.json"
    assert seen["version"] == updater.GATEWAY_VERSION
    assert seen["compose"]._project == "testprojekt"
