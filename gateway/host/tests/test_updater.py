import ast
import hashlib
import http.server
import json
import os
import random
import stat
import threading
import time
import urllib.request
from pathlib import Path
from unittest.mock import create_autospec

import pytest
from minisign_helper import keypair, sign

from smartheat_host import bundles, kanal, updater

COMPOSE = ("services:\n  runtime:\n    image: ghcr.io/x/gw@sha256:" + "a" * 64 + "\n").encode()
MOSQ = b"listener 1883\n"
BASE = "https://release.example.test/0.3.0/"


class FakeKanal:
    """offline=True: der Broker ist nicht erreichbar (Netz weg, z. B. Router nach Stromausfall). Meldungen gehen
    lokal immer durch, ausser broken=True (Datentraeger nicht schreibbar)."""

    def __init__(self, desired):
        self.desired_answer, self.results, self.offline, self.broken = desired, [], False, False
        self.desired_calls = self.reach_calls = 0

    def desired(self):
        self.desired_calls += 1
        if isinstance(self.desired_answer, Exception):
            raise self.desired_answer
        return self.desired_answer

    def update_result(self, version, result, reason):
        if self.broken:
            raise kanal.KanalFehler("nicht schreibbar")
        self.results.append((version, result, reason))

    def erreichbar(self):
        self.reach_calls += 1
        return not self.offline


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
        self.api = FakeKanal({"version": "0.3.0", "manifest_url": url,
                            "manifest_sha256": hashlib.sha256(raw).hexdigest(),
                            "signature": sign(self.secret, self.key_id, raw)})
        self.compose = FakeCompose(fail_up, fail_pull)
        self.store = bundles.BundleStore(tmp_path)
        self.store.install("0.2.0", {"docker-compose.yml": COMPOSE, "mosquitto.conf": MOSQ})  # laufendes Bundle
        self.state_path = tmp_path / "updater" / "state.json"
        updater.State(current="0.2.0").save(self.state_path)
        self.healthy = healthy
        self.fetched = []
        self.clock = [0.0]
        self.extra = {} if health_seconds is None else {"health_seconds": health_seconds}
        self.context = {"updater": version, "key": self.key_id.hex()}
        self.updater = self.rebuild(version=version)

    def rebuild(self, *, version="0.2.0", public_key_text=None):
        """Neuer Updater ueber derselben Zustandsdatei (Neustart, neue Host-Version, neuer Schluessel)."""
        return updater.Updater(
            kanal=self.api, store=self.store, compose=self.compose, fetch=self.fetch,
            public_key_text=public_key_text or self.public, state_path=self.state_path, version=version,
            health=lambda version: self.healthy, clock=lambda: self.clock[0], sleep=self.sleep, **self.extra,
        )

    def fetch(self, url, max_bytes):
        """Wie `fetch_url`: eine Antwort ueber `max_bytes` ist ein FetchError; `fetched` haelt jede Anfrage fest."""
        self.fetched.append(url)
        if url not in self.served:
            raise updater.FetchError(url)
        if len(self.served[url]) > max_bytes:
            raise updater.FetchError(f"{url}: zu gross")
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
    ({"compose": b"services:\n  runtime:\n    image: ghcr.io/x/gw:latest\n"}, "manifest_ungueltig"),
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


def _serve_manifest(world, raw, *, signed=None):
    """Bietet `raw` als Manifest an: die SHA-256 im Angebot passt zu `raw`, die Signatur gilt `signed` (Standard `raw`)."""
    world.served[BASE + "manifest.json"] = raw
    world.api.desired_answer["manifest_sha256"] = hashlib.sha256(raw).hexdigest()
    world.api.desired_answer["signature"] = sign(world.secret, world.key_id, raw if signed is None else signed)


def _manifest_with(world, **changes):
    manifest = json.loads(world.served[BASE + "manifest.json"])
    manifest.update(changes)
    return json.dumps(manifest).encode()


def _assert_nothing_installed(world, version="0.3.0"):
    assert not any(call[0] in ("pull", "up") for call in world.compose.calls)
    assert not world.store.exists(version) and not world.store.dir(version).exists()  # auch kein halbes Bundle
    assert not (world.store.root / "bundles" / f".tmp-{version}").exists()


EVIL_COMPOSE = b"services:\n  runtime:\n    image: ghcr.io/evil/gw@sha256:" + b"c" * 64 + b"\n"


@pytest.mark.parametrize("attack", ["images", "compose"])
def test_manifest_changed_after_signing_is_refused_even_with_a_matching_sha(tmp_path, attack):
    """F-gateway-host-05/-43: ein Angreifer mit dem Server (Angebot, Manifest, Dateien) ersetzt das Manifest nach dem
    Signieren und zieht die SHA-256 im Angebot nach; "compose" tauscht zusaetzlich die Compose-Datei samt SHA im Manifest
    aus (in sich stimmig, mit Digest). Allein die Signatur ueber die urspruenglichen Bytes haelt ihn auf."""
    world = World(tmp_path)
    original = world.served[BASE + "manifest.json"]
    if attack == "images":
        changed = _manifest_with(world, images={"gateway": "ghcr.io/evil/gw@sha256:" + "c" * 64})
    else:
        files = json.loads(original)["files"] | {"docker-compose.yml": hashlib.sha256(EVIL_COMPOSE).hexdigest()}
        changed = _manifest_with(world, files=files)
        world.served[BASE + "docker-compose.yml"] = EVIL_COMPOSE
    assert changed != original
    _serve_manifest(world, changed, signed=original)
    assert world.updater.run_once() == "abgelehnt"
    assert world.api.results == [("0.3.0", "rollback", "signatur_ungueltig")]
    assert "signatur_ungueltig" in updater.REASONS  # Grund aus der festen Liste
    assert world.fetched == [BASE + "manifest.json"]  # keine Bundle-Datei geladen
    _assert_nothing_installed(world)
    assert world.state().rejected == ["0.3.0"] and world.state().current == "0.2.0"
    # Gegenprobe: dieselben Bytes, mit dem Schluessel signiert, werden angenommen - allein die Signatur trennt
    (tmp_path / "zwei").mkdir()
    signed = World(tmp_path / "zwei")
    if attack == "compose":
        signed.served[BASE + "docker-compose.yml"] = EVIL_COMPOSE
    _serve_manifest(signed, changed)
    assert signed.updater.run_once() == "aktualisiert"


MANIFEST_LIMIT = 65536  # Byte, Spec G2b-1 (64 KiB)
FILE_LIMIT = 1048576  # Byte (1 MiB)


def _padded(raw: bytes, size: int) -> bytes:
    assert len(raw) <= size
    return raw + b" " * (size - len(raw))  # Leerraum am Ende bleibt gueltiges JSON


def test_manifest_of_exactly_64_kib_is_accepted(tmp_path):
    world = World(tmp_path)
    raw = _padded(world.served[BASE + "manifest.json"], MANIFEST_LIMIT)
    assert len(raw) == 65536
    _serve_manifest(world, raw)
    assert world.updater.run_once() == "aktualisiert"
    assert (world.store.dir("0.3.0") / "manifest.json").stat().st_size == 65536


def test_manifest_one_byte_over_64_kib_is_temporary_and_nothing_is_unpacked(tmp_path):
    """Uebergroesse ist ein Abruffehler (FetchError): "vorlaeufig", nicht abgelehnt, keine Meldung, keine weitere Datei."""
    world = World(tmp_path)
    raw = _padded(world.served[BASE + "manifest.json"], MANIFEST_LIMIT + 1)
    assert len(raw) == 65537
    _serve_manifest(world, raw)  # SHA und Signatur passen: allein die Groesse haelt es auf
    assert world.updater.run_once() == "vorlaeufig"
    assert world.fetched == [BASE + "manifest.json"]
    assert world.api.results == [] and world.state().rejected == []
    _assert_nothing_installed(world)


def _padded_file(base: bytes, size: int) -> bytes:
    assert len(base) + 2 <= size
    return base + b"#" + b"x" * (size - len(base) - 2) + b"\n"  # Kommentarzeile: Compose und Konfiguration bleiben gueltig


def _serve_file(world, name, data):
    files = json.loads(world.served[BASE + "manifest.json"])["files"] | {name: hashlib.sha256(data).hexdigest()}
    world.served[BASE + name] = data
    _serve_manifest(world, _manifest_with(world, files=files))


FILE_BASES = {"docker-compose.yml": COMPOSE, "mosquitto.conf": MOSQ}


@pytest.mark.parametrize("name", FILE_BASES)
def test_bundle_file_of_exactly_1_mib_is_accepted(tmp_path, name):
    world = World(tmp_path)
    data = _padded_file(FILE_BASES[name], FILE_LIMIT)
    assert len(data) == 1048576
    _serve_file(world, name, data)
    assert world.updater.run_once() == "aktualisiert"
    assert (world.store.dir("0.3.0") / name).stat().st_size == 1048576


@pytest.mark.parametrize(("name", "fetched"), [
    ("docker-compose.yml", ["manifest.json", "docker-compose.yml"]),
    ("mosquitto.conf", ["manifest.json", "docker-compose.yml", "mosquitto.conf"]),
])
def test_bundle_file_one_byte_over_1_mib_is_temporary_and_nothing_is_unpacked(tmp_path, name, fetched):
    world = World(tmp_path)
    data = _padded_file(FILE_BASES[name], FILE_LIMIT + 1)
    assert len(data) == 1048577
    _serve_file(world, name, data)  # SHA im Manifest und Signatur passen: allein die Groesse haelt es auf
    assert world.updater.run_once() == "vorlaeufig"
    assert world.fetched == [BASE + item for item in fetched]  # danach wird nichts mehr geladen
    assert world.api.results == [] and world.state().rejected == []
    assert world.state().current == "0.2.0" and world.state().in_progress is None
    _assert_nothing_installed(world)


def _tree(root: Path, skip=("bundles", "updater")) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*"))
            if path.is_file() and path.relative_to(root).parts[0] not in skip}


def _host_files(root: Path):
    """Dateien ausserhalb der Bundle-Ablage, die ein Angebot nie ersetzen darf: Host-Dienste, Umgebung, Daten."""
    for name, content in (("host/gateway.env", b"SECRET=1\n"), ("host/smartheat_host/updater.py", b"# host\n"),
                          ("data/software_soll.json", b"{}"), ("etc/passwd", b"root:x:0:0\n")):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(content)


@pytest.mark.parametrize("extra", [
    "../host/gateway.env", "host/gateway.env", "../../host/smartheat_host/updater.py", "smartheat_host/updater.py",
    "/etc/passwd", "https://evil.test/updater.py",
])
@pytest.mark.parametrize("replace", [False, True])
def test_an_offer_that_names_other_files_is_refused_and_touches_nothing_outside_the_bundle_store(
        tmp_path, extra, replace):
    """F-gateway-host-43: ein Angebot kann Host-Dienste nicht ersetzen (5b-D2-1: das geht nur mit neuem Image oder
    Installer). Das signierte Manifest darf genau docker-compose.yml und mosquitto.conf nennen; jeder andere Name
    (Pfad aus dem Bundle heraus, absolut, URL), zusaetzlich oder an Stelle einer der beiden, wird abgelehnt."""
    world = World(tmp_path)
    _host_files(tmp_path)
    before = _tree(tmp_path)
    assert before  # die Probe sieht die Dateien
    evil = b"# ersetzt den Host-Dienst\n"
    files = json.loads(world.served[BASE + "manifest.json"])["files"]
    if replace:
        del files["mosquitto.conf"]
    files[extra] = hashlib.sha256(evil).hexdigest()
    for url in (BASE + extra, "https://release.example.test/host/gateway.env", "https://release.example.test" + extra):
        world.served[url] = evil
    _serve_manifest(world, _manifest_with(world, files=files))
    assert world.updater.run_once() == "abgelehnt"
    assert world.api.results == [("0.3.0", "rollback", "manifest_ungueltig")]
    assert world.fetched == [BASE + "manifest.json"]
    _assert_nothing_installed(world)
    assert _tree(tmp_path) == before


def test_a_complete_update_changes_only_the_bundle_store_and_the_state(tmp_path):
    """Gegenprobe zum vorigen Test: auch ein gelungenes Update schreibt nur unter bundles/ und updater/."""
    world = World(tmp_path)
    _host_files(tmp_path)
    before = _tree(tmp_path)
    assert world.updater.run_once() == "aktualisiert"
    assert _tree(tmp_path) == before
    assert sorted(path.name for path in world.store.dir("0.3.0").iterdir()) == [
        "docker-compose.yml", "manifest.json", "manifest.json.minisig", "mosquitto.conf"]


def test_the_running_version_is_current_without_download_report_or_state_change(tmp_path):
    """F-gateway-host-01/-06/-43: ein vollstaendiges Angebot fuer die laufende Version (0.2.0) ist "aktuell"."""
    world = World(tmp_path)
    offer = dict(world.api.desired_answer)
    world.api.desired_answer = offer | {"version": "0.2.0"}
    state_before = world.state_path.read_bytes()
    assert world.updater.run_once() == "aktuell"
    assert world.fetched == [] and world.compose.calls == [] and world.api.results == []
    assert world.state_path.read_bytes() == state_before and world.api.desired_calls == 1
    # Gegenprobe: dasselbe Angebot fuer eine neuere Version wird geladen
    world.api.desired_answer = offer
    assert world.updater.run_once() == "aktualisiert"
    assert world.fetched != []


@pytest.mark.parametrize("offer", [
    {"version": "0.3.0"},
    {"version": "0.3.0", "manifest_url": None, "manifest_sha256": "", "signature": None},
])
def test_a_newer_version_without_any_bundle_field_is_current_without_download(tmp_path, offer):
    world = World(tmp_path)
    world.api.desired_answer = offer
    state_before = world.state_path.read_bytes()
    assert world.updater.run_once() == "aktuell"
    assert world.fetched == [] and world.compose.calls == [] and world.api.results == []
    assert world.state_path.read_bytes() == state_before


@pytest.mark.parametrize(("field", "value", "reason"), [
    ("manifest_url", BASE + "manifest.json", "manifest_ungueltig"),
    ("manifest_sha256", "a" * 64, "manifest_ungueltig"),
    ("signature", "x", "manifest_ungueltig"),
])
def test_one_bundle_field_alone_makes_the_version_an_offer(tmp_path, field, value, reason):
    """Gegenprobe zum vorigen Test: schon ein einziges Bundle-Feld ist ein Angebot (und hier ein unvollstaendiges)."""
    world = World(tmp_path)
    world.api.desired_answer = {"version": "0.3.0", field: value}
    assert world.updater.run_once() == "abgelehnt"
    assert world.api.results == [("0.3.0", "rollback", reason)]
    assert world.fetched == [] and world.compose.calls == []


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
        kanal=world.api, store=world.store, compose=world.compose, fetch=world.fetch,
        public_key_text="untrusted comment: SMARTHEAT-PLATZHALTER\nSMARTHEAT-PLATZHALTER\n",
        state_path=world.state_path, version="0.2.0", health=lambda version: True, clock=lambda: 0.0,
        sleep=world.sleep)
    assert world.updater.run_once() == "abgelehnt"
    assert world.api.results == [("0.3.0", "rollback", "signatur_ungueltig")]


@pytest.mark.parametrize("error", [kanal.KanalFehler("x")])
def test_server_trouble_is_temporary(tmp_path, error):
    world = World(tmp_path)
    world.api.desired_answer = error
    assert world.updater.run_once() == "vorlaeufig"
    assert world.state().rejected == [] and world.api.results == []


def test_missing_desired_file_is_temporary(tmp_path):
    world = World(tmp_path)
    world.api.desired_answer = kanal.KanalFehler("Soll-Software noch nicht bekannt")
    assert world.updater.run_once() == "vorlaeufig" and world.compose.calls == []


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
    assert world.api.reach_calls > 1  # Erreichbarkeit wird je Takt geprueft
    assert world.api.results == [("0.3.0", "rollback", "ungesund")]  # abgelehnt, nicht server_unerreichbar


def test_unreachable_until_the_cap_rolls_back_without_rejecting_and_keeps_the_report(tmp_path):
    world = World(tmp_path, healthy=False)
    _offline_until(world, 10 ** 9)
    world.api.broken = True  # Datentraeger nicht schreibbar: die Meldung bleibt in pending_reports
    assert world.updater.run_once() == "zurueckgerollt"
    assert updater.HEALTH_CAP_SECONDS <= world.clock[0] < updater.HEALTH_CAP_SECONDS + updater.HEALTH_POLL_SECONDS
    assert world.compose.calls[-1] == ("up", "0.2.0")
    state = world.state()
    assert (state.current, state.rejected, state.in_progress) == ("0.2.0", [], None)  # nicht abgelehnt
    assert world.api.results == []  # nicht schreibbar: nichts gemeldet ...
    assert state.pending_reports == [{"version": "0.3.0", "result": "rollback", "reason": "server_unerreichbar"}]  # ... gemerkt
    # Netz wieder da: zuerst die offene Meldung, dann der neue Versuch derselben Version
    world.api.offline, world.api.broken, world.healthy = False, False, True
    world.updater._sleep = world.sleep
    assert world.rebuild().run_once() == "aktualisiert"
    assert world.api.results == [("0.3.0", "rollback", "server_unerreichbar"), ("0.3.0", "ok", "")]
    assert world.state().pending_reports == []


def test_cap_rollback_that_fails_keeps_the_no_reject_marker(tmp_path):
    world = World(tmp_path, healthy=False, fail_up={"0.2.0"})
    _offline_until(world, 10 ** 9)
    assert world.updater.run_once() == "vorlaeufig"
    assert world.state().in_progress["rollback"] == "server_unerreichbar"
    world.compose.fail_up.clear()
    world.updater.recover()
    state = world.state()
    assert (state.current, state.rejected, state.in_progress) == ("0.2.0", [], None)


def test_report_that_cannot_be_sent_is_kept_and_sent_first_next_run(tmp_path):
    world = World(tmp_path)
    world.api.desired_answer["signature"] = "kaputt"
    original = world.api.update_result

    def broken(*args):
        raise kanal.KanalFehler("update_result")

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
    world.api.desired_answer = {"version": "0.2.0"}
    world.api.broken = True
    assert world.updater.run_once() == "aktuell"
    assert len(world.state().pending_reports) == 2
    world.api.broken = False
    world.updater.run_once()
    assert world.api.results == [("0.2.8", "rollback", "ungesund"), ("0.2.9", "ok", "")]


def test_updater_verwirft_fremde_gruende_beim_laden(tmp_path, caplog):
    """Die Zustandsdatei ist lokale Eingabe: eine vorgemerkte Meldung mit fremdem Grund geht nie an den Server, das Laden
    wirft nicht (der Rueckweg darf an einer Meldung nicht scheitern)."""
    gueltig = {"version": "0.2.8", "result": "rollback", "reason": "ungesund"}
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"current": "0.2.0", "pending_reports": [
        gueltig, {"version": "0.2.9", "result": "rollback", "reason": "frei"},
        {"version": "0.3.0", "result": "ok", "reason": ""}]}))
    assert updater.State.load(path).pending_reports == [gueltig, {"version": "0.3.0", "result": "ok", "reason": ""}]
    assert "REASONS" in caplog.text and "frei" not in caplog.text  # ein Hinweis, nie der fremde Grund
    caplog.clear()
    path.write_text(json.dumps({"current": "0.2.0", "pending_reports": [gueltig]}))  # Gegenprobe: ohne den fremden
    assert updater.State.load(path).pending_reports == [gueltig] and caplog.records == []


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


def test_a_missing_device_in_a_multiline_flow_list_is_not_noticed(tmp_path):
    """Dokumentierte Grenze (bundles.compose_devices): der fehlende Stick in einer mehrzeiligen Flussliste haelt das
    Update nicht zurueck; dasselbe Bundle mit der einzeiligen Flussliste wartet (Gegenprobe)."""
    stick = tmp_path / "zigbee"  # fehlt
    service = f"  zigbee2mqtt:\n    image: koenkk/z2m@sha256:{'b' * 64}\n    devices"
    one_line = (COMPOSE.decode() + f"{service}: [\"{stick}:/dev/zigbee\"]\n").encode()
    multi_line = (COMPOSE.decode() + f"{service}: [\n      \"{stick}:/dev/zigbee\",\n    ]\n").encode()
    assert World(tmp_path, compose=one_line).updater.run_once() == "vorlaeufig"
    (tmp_path / "zwei").mkdir()
    world = World(tmp_path / "zwei", compose=multi_line)
    assert world.updater.run_once() == "aktualisiert"
    assert ("up", "0.3.0") in world.compose.calls


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


HEALTHY = {"runtime": "healthy"}


@pytest.mark.parametrize(("data", "containers", "ok"), [
    ({"server_ok": True, "runtime_status": "regelt"}, HEALTHY, True),
    ({"server_ok": True, "runtime_status": "notbetrieb"}, HEALTHY, True),
    ({"server_ok": True, "runtime_status": None}, HEALTHY, True),       # nicht eingerichtet: Status abgeraeumt
    ({"server_ok": True, "runtime_status": "startet"}, HEALTHY, False),
    ({"server_ok": False, "runtime_status": "regelt"}, HEALTHY, False),
    ({"server_ok": None, "runtime_status": "regelt"}, HEALTHY, False),
    ({"runtime_status": "regelt"}, HEALTHY, False),
    ({"server_ok": True, "runtime_status": "regelt"}, {"runtime": "unhealthy"}, False),
    ({"server_ok": True, "runtime_status": "regelt"}, {"runtime": "starting"}, False),
    ({"server_ok": True, "runtime_status": "regelt"}, {"zigbee2mqtt": "healthy"}, False),  # Laufzeit fehlt/startet neu
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


def _redirected_response(final_url: str, body: bytes = b"inhalt"):
    """Antwort von urllib.request.urlopen, nachdem urllib einer Weiterleitung gefolgt ist: `geturl()` nennt die
    Adresse, von der der Inhalt tatsaechlich kam (urllib folgt auch einer Weiterleitung von https auf http)."""
    response = create_autospec(http.client.HTTPResponse, instance=True)
    response.__enter__.return_value = response
    response.geturl.return_value = final_url
    response.read.return_value = body
    return response


def test_fetch_refuses_a_redirect_from_https_to_http_unless_allowed(monkeypatch):
    """F-gateway-host-02: ein Angreifer im Netz (oder ein kompromittierter Server) leitet einen https-Abruf auf http um.
    Die Attrappe steht an der Grenze, `urllib.request.urlopen`; die Adresse der Antwort entscheidet."""
    opened = create_autospec(urllib.request.urlopen, return_value=_redirected_response("http://evil.test/x"))
    monkeypatch.setattr(urllib.request, "urlopen", opened)
    with pytest.raises(updater.FetchError, match="https"):
        updater.fetch_url("https://release.example.test/x", 100, allow_http=False)
    # Gegenprobe: dieselbe Antwort wird mit dem Schalter (Labor) angenommen, ...
    assert updater.fetch_url("https://release.example.test/x", 100, allow_http=True) == b"inhalt"
    # ... und ohne Weiterleitung (Antwort von https) auch ohne ihn.
    opened.return_value = _redirected_response("https://cdn.example.test/x")
    assert updater.fetch_url("https://release.example.test/x", 100, allow_http=False) == b"inhalt"


@pytest.mark.parametrize("final_url", ["file:///etc/passwd", "ftp://evil.test/x"])
def test_fetch_refuses_a_redirect_to_other_schemes_even_when_http_is_allowed(monkeypatch, final_url):
    opened = create_autospec(urllib.request.urlopen, return_value=_redirected_response(final_url))
    monkeypatch.setattr(urllib.request, "urlopen", opened)
    with pytest.raises(updater.FetchError, match="erlaubt"):
        updater.fetch_url("https://release.example.test/x", 100, allow_http=True)


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


@pytest.fixture
def healthz_bodies():
    """Server, der je Pfad einen vorgegebenen Koerper liefert (Status 200)."""
    bodies = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(bodies[self.path])

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", bodies
    server.shutdown()
    server.server_close()
    thread.join()


def _healthz_object(size: int) -> bytes:
    raw = json.dumps({"server_ok": True, "runtime_status": None}).encode()
    return raw[:-1] + b" " * (size - len(raw)) + b"}"  # Leerraum vor der Klammer: gueltiges JSON


@pytest.mark.parametrize("body", [b"", b"<html>502</html>", b"{\"server_ok\": true", b"[1, 2]", b"\"server_ok\"", b"null",
                                  b"true", b"42"])
def test_healthz_that_is_not_a_json_object_is_not_healthy(healthz_bodies, body):
    url, bodies = healthz_bodies
    bodies["/ok"], bodies["/x"] = _healthz_object(100), body
    assert updater.fetch_health(url + "/ok") == {"server_ok": True, "runtime_status": None}  # Gegenprobe
    assert updater.health_ok(updater.fetch_health(url + "/ok"), {"runtime": "healthy"}) is True
    assert updater.fetch_health(url + "/x") is None
    assert updater.health_ok(updater.fetch_health(url + "/x"), {"runtime": "healthy"}) is False


def test_healthz_over_64_kib_is_not_healthy_but_exactly_64_kib_is_read(healthz_bodies):
    url, bodies = healthz_bodies
    bodies["/genau"], bodies["/zuviel"] = _healthz_object(65536), _healthz_object(65537)
    assert len(bodies["/genau"]) == 65536 and len(bodies["/zuviel"]) == 65537
    assert updater.fetch_health(url + "/genau") == {"server_ok": True, "runtime_status": None}
    assert updater.fetch_health(url + "/zuviel") is None  # abgeschnitten: kein gueltiges JSON mehr


def test_the_health_deadline_follows_the_monotonic_clock_not_the_wall_clock(tmp_path, monkeypatch):
    """F-gateway-host-07: die Wanduhr springt beim Boot. Sprunghaft vorlaufende `time.time()` waehrend der Gesundheits-
    pruefung darf die Frist nicht verkuerzen: der Updater (Standarduhr) wartet die Frist auf der monotonen Uhr ab. Die
    Attrappe von `time.time` ist die Grenze zur Systemuhr; ausserhalb des Wartens laeuft sie echt."""
    world = World(tmp_path, healthy=False)
    wall = {"jump": False, "calls": 0}
    real_time = time.time

    def jumping_time():
        if wall["jump"]:
            wall["calls"] += 1
            return real_time() + 86400.0 * wall["calls"]  # jeder Blick auf die Wanduhr: einen Tag spaeter
        return real_time()

    waits = []

    def sleep(seconds):
        waits.append(seconds)
        assert len(waits) < 500, "Die Frist endet nicht"
        time.sleep(0.01)

    monkeypatch.setattr(time, "time", create_autospec(time.time, side_effect=jumping_time))
    world.compose.on_up = lambda version: wall.update(jump=version == "0.3.0")  # Sprung erst nach dem Umschalten
    short = updater.Updater(kanal=world.api, store=world.store, compose=world.compose, fetch=world.fetch,
                            public_key_text=world.public, state_path=world.state_path, version="0.2.0",
                            health=lambda version: False, sleep=sleep, health_seconds=0.1)  # Standarduhr
    started = time.monotonic()
    assert short.run_once() == "zurueckgerollt"
    assert time.monotonic() - started >= 0.1  # die Frist ist auf der monotonen Uhr abgelaufen
    assert len(waits) >= 5  # die Pruefung lief ueber viele Takte, obwohl die Wanduhr (falls befragt) Tage vorsprang


def test_state_file_survives_a_failure_while_it_is_written(tmp_path, monkeypatch):
    """F-gateway-host-08: bricht das Schreiben ab (Stromausfall, Datentraeger voll), bleibt die alte Datei stehen und
    laedt. `os.replace`/`os.fsync` sind die Dateisystem-Grenze (Attrappen mit autospec; die Schreibfunktion testet
    client-kern.md)."""
    path = tmp_path / "updater" / "state.json"
    old = updater.State(current="0.2.0", previous="0.1.0", rejected=["0.1.5"])
    old.save(path)
    before = path.read_bytes()
    new = updater.State(current="0.3.0", previous="0.2.0")
    for name in ("fsync", "replace"):
        with monkeypatch.context() as patch:
            patch.setattr(os, name, create_autospec(getattr(os, name), side_effect=OSError("Stromausfall")))
            with pytest.raises(OSError, match="Stromausfall"):
                new.save(path)
        assert path.read_bytes() == before and updater.State.load(path) == old
    new.save(path)  # Gegenprobe: ohne Fehler ersetzt das Schreiben die Datei
    assert updater.State.load(path) == new and path.read_bytes() != before


def test_the_report_queue_holds_20_entries_and_the_oldest_falls_out(tmp_path):
    """F-gateway-host-10: 21 abgelehnte Versionen in Folge, der Kanal nimmt nichts an (Datentraeger nicht schreibbar)."""
    world = World(tmp_path)
    world.api.broken = True
    offer = dict(world.api.desired_answer)

    def reject(number):
        world.api.desired_answer = offer | {"version": f"0.3.{number}", "manifest_sha256": "0" * 64}
        assert world.updater.run_once() == "abgelehnt"

    for number in range(1, 21):
        reject(number)
    assert [entry["version"] for entry in world.state().pending_reports] == [f"0.3.{n}" for n in range(1, 21)]
    reject(21)  # die 21. Meldung
    pending = world.state().pending_reports
    assert len(pending) == 20
    assert pending[0] == {"version": "0.3.2", "result": "rollback", "reason": "manifest_ungueltig"}  # 0.3.1 fiel weg
    assert pending[-1] == {"version": "0.3.21", "result": "rollback", "reason": "manifest_ungueltig"}
    world.api.broken = False  # der Kanal kommt zurueck: die 20 gehen in Reihenfolge hinaus
    world.api.desired_answer = {"version": "0.2.0"}
    assert world.updater.run_once() == "aktuell"
    assert [version for version, _, _ in world.api.results] == [f"0.3.{n}" for n in range(2, 22)]
    assert world.state().pending_reports == []


def _main_environment(monkeypatch, root: Path, *, offer=None, allow_http=None):
    (root / "data").mkdir(parents=True, exist_ok=True)
    if offer is not None:
        (root / "data" / "software_soll.json").write_text(json.dumps(offer))
    (root / "release.pub").write_text("untrusted comment: x\nAAAA\n")
    monkeypatch.setenv("SHG_ROOT", str(root))
    monkeypatch.setenv("SHG_RELEASE_PUB", str(root / "release.pub"))
    monkeypatch.delenv("SHG_UPDATER_ALLOW_HTTP", raising=False)
    if allow_http is not None:
        monkeypatch.setenv("SHG_UPDATER_ALLOW_HTTP", allow_http)


class _Stop(Exception):
    pass


def test_main_waits_120_seconds_then_runs_every_900_plus_up_to_300_seconds(tmp_path, monkeypatch):
    """F-gateway-host-01: Takt des Dienstes. Echte `Updater`, echter lokaler Kanal (ohne Soll-Datei: "vorlaeufig");
    Uhr (`time.sleep`) und Zufall (`random.uniform`) sind die Grenze: die Folge der Ereignisse und der Wartezeiten ist
    das Ergebnis. Der Zufall liefert erst die obere, dann die untere Schranke seines Bereichs."""
    _main_environment(monkeypatch, tmp_path)
    events = []
    real_recover, real_run_once = updater.Updater.recover, updater.Updater.run_once

    def recover(self):
        events.append("recover")
        real_recover(self)

    def run_once(self):
        result = real_run_once(self)
        events.append(f"durchlauf:{result}")
        return result

    def sleep(seconds):
        events.append(f"warten:{float(seconds)}")
        if len([event for event in events if event.startswith("warten")]) == 4:
            raise _Stop

    bounds = iter([lambda low, high: high, lambda low, high: low, lambda low, high: (low + high) / 2])
    monkeypatch.setattr(updater.Updater, "recover", recover)
    monkeypatch.setattr(updater.Updater, "run_once", run_once)
    monkeypatch.setattr(time, "sleep", create_autospec(time.sleep, side_effect=sleep))
    monkeypatch.setattr(random, "uniform", create_autospec(random.uniform, side_effect=lambda a, b: next(bounds)(a, b)))
    with pytest.raises(_Stop):
        updater.main([])
    assert events == ["recover", "warten:120.0", "durchlauf:vorlaeufig", "warten:1200.0",
                      "durchlauf:vorlaeufig", "warten:900.0", "durchlauf:vorlaeufig", "warten:1050.0"]


def test_main_once_recovers_runs_once_and_does_not_wait(tmp_path, monkeypatch, capsys):
    _main_environment(monkeypatch, tmp_path)
    events = []
    real_recover = updater.Updater.recover
    monkeypatch.setattr(updater.Updater, "recover", lambda self: (events.append("recover"), real_recover(self)))
    monkeypatch.setattr(time, "sleep", create_autospec(time.sleep, side_effect=_Stop))
    updater.main(["--once"])
    assert events == ["recover"] and capsys.readouterr().out == "vorlaeufig\n"


@pytest.mark.parametrize(("allow", "outcome"), [
    ("1", "vorlaeufig"),  # http erlaubt: der Abruf wird versucht (Port 1: keine Verbindung)
    (None, "abgelehnt"), ("0", "abgelehnt"), ("", "abgelehnt"), ("true", "abgelehnt"), ("yes", "abgelehnt"),
])
def test_main_accepts_an_http_offer_only_with_the_switch_set_to_1(tmp_path, monkeypatch, capsys, allow, outcome):
    """F-gateway-host-01: der Produktivwert ist "aus"; nur genau "1" schaltet http frei (Labor)."""
    offer = {"version": "0.3.0", "manifest_url": "http://127.0.0.1:1/manifest.json", "manifest_sha256": "a" * 64,
             "signature": "x"}
    _main_environment(monkeypatch, tmp_path, offer=offer, allow_http=allow)
    updater.main(["--once"])
    assert capsys.readouterr().out == outcome + "\n"
    reports = tmp_path / "data" / "software_meldungen.json"
    if outcome == "abgelehnt":  # die http-Adresse wurde ohne Abruf abgelehnt und gemeldet
        assert [(m["version"], m["reason"]) for m in json.loads(reports.read_text())["meldungen"]] == [
            ("0.3.0", "manifest_ungueltig")]
    else:  # der Abruf scheiterte am Netz: vorlaeufig, nichts gemeldet
        assert not reports.exists()


def test_main_once_wires_the_environment(tmp_path, monkeypatch, capsys):
    release_pub = tmp_path / "release.pub"
    release_pub.write_text("pub")
    monkeypatch.setenv("SHG_ROOT", str(tmp_path))
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
    assert isinstance(seen["kanal"], kanal.LokalerKanal) and seen["kanal"]._data == tmp_path / "data"


@pytest.mark.parametrize("current", ["0.3.1", "1.0.0"])
def test_older_desired_version_is_refused_without_download(tmp_path, current):
    world = World(tmp_path)
    updater.State(current=current).save(world.state_path)
    world.served.clear()  # ein Download wuerde scheitern - es darf keinen geben
    assert world.updater.run_once() == "abgelehnt"
    assert world.api.results == [("0.3.0", "rollback", "version_zu_alt")]
    state = world.state()
    assert state.current == current and "0.3.0" in state.rejected
    assert world.compose.calls == []
    assert world.updater.run_once() == "aktuell"  # bleibt abgelehnt, keine zweite Meldung
    assert len(world.api.results) == 1


def test_unreadable_current_version_does_not_block_updates(tmp_path):
    world = World(tmp_path)
    updater.State(current="kaputt").save(world.state_path)
    assert world.updater.run_once() == "aktualisiert"


def test_downgrade_reason_is_in_the_fixed_list():
    assert "version_zu_alt" in updater.REASONS
    assert updater.RETRY_REASON in updater.REASONS


def test_der_updater_meldet_nur_gruende_aus_reasons():
    """A5-85: Jeder Grund, den der Updater an _Refused, _reject, _rollback oder _queue gibt, ist ein Name aus REASONS
    oder ein durchgereichter Wert, nie ein Literal; so meldet er nur Gruende, die test_vertrag_im_host.py gegen den
    Vertrag prueft. Ausnahme: _queue meldet das Ergebnis "ok" mit leerem Grund."""
    tree = ast.parse(Path(updater.__file__).read_text(encoding="utf-8"))
    by_value = {name: getattr(updater, name) for name in dir(updater) if name.isupper()}
    position = {"_Refused": 0, "_reject": 2, "_rollback": 2, "_queue": 3}  # Argument-Index ohne self
    seen = set()
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        name = call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", None)
        if name not in position:
            continue
        if len(call.args) > position[name]:
            reason = call.args[position[name]]
        else:
            keywords = [keyword.value for keyword in call.keywords if keyword.arg == "reason"]
            assert keywords, f"Zeile {call.lineno}: {name} ohne erkennbaren Grund (weder Position noch reason=)"
            reason = keywords[0]
        if name == "_queue" and isinstance(reason, ast.Constant) and reason.value == "":
            assert len(call.args) > 2, (
                f"Zeile {call.lineno}: _queue mit leerem Grund braucht das Ergebnis auf Position 2"
            )
            assert isinstance(call.args[2], ast.Constant) and call.args[2].value == "ok", (
                f"Zeile {call.lineno}: leerer Grund nur beim Ergebnis \"ok\""
            )
            continue
        assert not isinstance(reason, ast.Constant), f"Zeile {call.lineno}: Grund als Literal {reason.value!r}"
        if isinstance(reason, ast.Name) and reason.id in by_value:
            assert by_value[reason.id] in updater.REASONS, f"Zeile {call.lineno}: {reason.id} nicht in REASONS"
            seen.add(by_value[reason.id])
    assert seen == set(updater.REASONS)


def test_reachability_is_the_broker_not_the_bundle(tmp_path):
    # Review Focus 5: das neue Bundle ist ungesund, der Broker erreichbar -> nach der Frist zurueck und abgelehnt;
    # ist der Broker nicht erreichbar, ruht die Frist und die Version wird nicht abgelehnt.
    world = World(tmp_path, healthy=False)
    assert world.updater.run_once() == "zurueckgerollt"
    assert world.api.results[-1] == ("0.3.0", "rollback", "ungesund") and "0.3.0" in world.state().rejected
    (tmp_path / "zwei").mkdir()
    offline = World(tmp_path / "zwei", healthy=False)
    offline.api.offline = True
    assert offline.updater.run_once() == "zurueckgerollt"
    assert offline.api.results[-1] == ("0.3.0", "rollback", updater.RETRY_REASON)
    assert "0.3.0" not in offline.state().rejected


def test_only_the_runtime_container_must_be_healthy():
    assert updater.REQUIRED_HEALTHY == ("runtime",)
    assert updater.health_ok({"server_ok": True, "runtime_status": "regelt"}, {"runtime": "healthy"})
