"""Spec 5c 1.3: das HA-Add-on als DeviceHost (Faehigkeiten, Uhr, Pruefung, Befehle, Dokumente, Wunsch, Raum)."""
import json
import time

import pytest
from conftest import FakeClock
from fakes import FakeHa
from geraet_helpers import konfiguration_aus_optionen
from test_runtime import OPTIONS, env  # noqa: F401 - env ist eine Fixture

from heizungsbruecke import config, dokument, geraet_host
from heizungsbruecke.host import EV_RAUM, RAUM_SECONDS, HaHost
from smartheat_core.config_check import NichtPruefbar
from smartheat_device import wire
from smartheat_device.commands import CommandRegister, Done, Failed, Waiting
from smartheat_device.host import Anzeige
from smartheat_runtime.soll_eingang import EV_TARGET_INPUT
from smartheat_runtime.status import STATUS_ABGEMELDET
from smartheat_runtime.worker import RegulationWorker


class Supervisor:
    def __init__(self, info=None, error=None):
        self.info, self.error, self.calls = info or {"dt_synchronized": True}, error, 0

    def host_info(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.info


class Doc:
    def __init__(self, dirty):
        self.dirty = dirty


class Device:
    def __init__(self, konf=None, bed=None, dirty=False):
        self.wuensche, self.aus, self.konf, self.bed, self.dirty = [], [], konf, bed, dirty
        self.meldungen = []
        self.documents = self

    def wunsch(self, wert, *, herkunft, durch=None):
        self.wuensche.append((wert, herkunft, durch))
        return min(max(float(wert), 15.0), 25.0)

    def soll_quelle_aus(self, aus, wirksam=None):
        self.aus.append((aus, wirksam))

    def konfiguration(self):
        return self.konf

    def bedienung(self):
        return self.bed

    def aktuell(self, art):
        return Doc(self.dirty) if art == "bedienung" and self.bed is not None else None

    def abo_status(self):
        return "aktiv"

    def meldung(self, key, text, *, offen, kritisch=True):
        self.meldungen.append((key, text, offen, kritisch))


def _paths(tmp_path, monkeypatch):
    for name in ("RUNTIME_DIR", "ABGEMELDET_PATH", "AUFRAEUMEN_PATH", "ROOM_TARGET_PATH", "BACKUP_PATH"):
        monkeypatch.setattr(config, name, tmp_path / getattr(config, name).name)


def _host(tmp_path, monkeypatch, supervisor=None, device=None, ha=None):
    _paths(tmp_path, monkeypatch)
    clock = FakeClock()
    host = geraet_host.HaDeviceHost(ha or FakeHa(), supervisor or Supervisor(), clock=clock,
                                    wall=lambda: 1_800_000_000)
    device = device or Device()
    host.verbinden(device)
    return host, device, clock


def _register(host, tmp_path):
    register = CommandRegister(tmp_path, lambda body: True)
    host.befehle_eintragen(register)
    return register


def _bis(outcome, schritte=200):
    """Wartet (echte Zeit, Hintergrund-Thread) auf das Ergebnis eines Waiting."""
    for _ in range(schritte):
        done = outcome.check()
        if done is not None:
            return done
        time.sleep(0.01)
    raise AssertionError("kein Ergebnis")


def test_host_identity():
    assert geraet_host.HaDeviceHost.host == "ha" and geraet_host.HaDeviceHost.host in wire.HOSTS
    assert geraet_host.HaDeviceHost.version == geraet_host.ADDON_VERSION


def test_capabilities_and_clock(tmp_path, monkeypatch):
    host, _, clock = _host(tmp_path, monkeypatch)
    assert host.faehigkeiten() == {"drivers": [], "zigbee": False}  # kein updater: Updates macht der Supervisor
    assert host.uhr_synchron() is True
    host, _, _ = _host(tmp_path, monkeypatch, Supervisor(error=OSError("weg")))
    assert host.uhr_synchron() is None
    host, _, _ = _host(tmp_path, monkeypatch, Supervisor(info={"dt_synchronized": "ja"}))
    assert host.uhr_synchron() is None  # kein bool: unbekannt
    host, _, _ = _host(tmp_path, monkeypatch, Supervisor(info={"dt_synchronized": False}))
    assert host.uhr_synchron() is False


def test_clock_is_cached_for_a_minute(tmp_path, monkeypatch):
    supervisor = Supervisor()
    host, _, clock = _host(tmp_path, monkeypatch, supervisor)
    host.uhr_synchron()
    host.uhr_synchron()
    clock.advance(61)
    host.uhr_synchron()
    assert supervisor.calls == 2


def test_clock_error_logs_only_the_kind(tmp_path, monkeypatch, caplog):
    host, _, _ = _host(tmp_path, monkeypatch, Supervisor(error=OSError("Bearer geheim")))
    host.uhr_synchron()
    assert "OSError" in caplog.text and "geheim" not in caplog.text


def test_software_is_ignored(tmp_path, monkeypatch):
    host, _, _ = _host(tmp_path, monkeypatch)
    host.software_soll({"version": "9.9.9"})  # Updates kommen ueber Supervisor und Add-on-Store
    assert not any(tmp_path.iterdir())


def test_foreign_commands_are_not_supported(tmp_path, monkeypatch):
    host, _, _ = _host(tmp_path, monkeypatch)
    register = _register(host, tmp_path)
    assert {"inventory", "sign_off", "diagnostics", "zigbee_permit_join", "zigbee_devices", "driver_login",
            "driver_probe"} <= register.kinds
    for kind in ("zigbee_permit_join", "zigbee_devices", "driver_login", "driver_probe"):
        outcome = register._handlers[kind]({"driver_id": "x"})
        assert isinstance(outcome, Failed) and outcome.grund == "nicht_unterstuetzt"
        assert outcome.text == geraet_host.NICHT_UNTERSTUETZT_TEXT
        assert outcome.grund in wire.command_errors(kind)


def test_inventory_runs_in_the_background(tmp_path, monkeypatch):
    host, _, clock = _host(tmp_path, monkeypatch)
    seen = []
    monkeypatch.setattr(geraet_host.inventur, "inventur", lambda ha, f: seen.append(f) or {
        "entities": [], "geraete": [], "dienste": [], "einheit_temperatur": "°C", "zeitzone": "x"})
    register = _register(host, tmp_path)
    outcome = register._handlers["inventory"]({"stunden": 0, "filter": {"plattformen": ["mypyllant"]}})
    assert isinstance(outcome, Waiting)
    done = _bis(outcome)
    assert isinstance(done, Done) and done.inventur["einheit_temperatur"] == "°C" and done.result == {}
    assert seen == [{"plattformen": ["mypyllant"]}]
    host.hintergrund.stop()


def test_inventory_needs_a_filter(tmp_path, monkeypatch):
    host, _, _ = _host(tmp_path, monkeypatch)
    outcome = _register(host, tmp_path)._handlers["inventory"]({"stunden": 0})
    assert isinstance(outcome, Failed) and outcome.grund == "ungueltige_nutzlast"


@pytest.mark.parametrize("filter_", [
    {"plattformen": [["liste"]]}, {"domaenen": "climate"}, {"dienste": [1]}, {"geraeteklassen": [["sensor"]]},
    {"geraeteklassen": [{"a": 1}, ["sensor", "x"]]}, {"geraeteklassen": "sensor"}])
def test_inventory_with_a_malformed_filter_is_an_invalid_payload(tmp_path, monkeypatch, filter_):
    host, _, _ = _host(tmp_path, monkeypatch)
    monkeypatch.setattr(geraet_host.inventur, "inventur", lambda ha, f: pytest.fail("nicht aufgerufen"))
    outcome = _register(host, tmp_path)._handlers["inventory"]({"stunden": 0, "filter": filter_})
    assert isinstance(outcome, Failed) and outcome.grund == "ungueltige_nutzlast"


def test_inventory_with_ha_down_fails_without_contents_in_the_log(tmp_path, monkeypatch, caplog):
    ha = FakeHa()
    ha.offline = True
    host, _, _ = _host(tmp_path, monkeypatch, ha=ha)
    outcome = _register(host, tmp_path)._handlers["inventory"]({"stunden": 0, "filter": {"domaenen": ["climate"]}})
    done = _bis(outcome)
    assert isinstance(done, Failed) and done.grund == "ha_nicht_erreichbar"
    assert done.grund in wire.command_errors("inventory")
    assert "OSError" in caplog.text and "HA nicht erreichbar" not in caplog.text  # nur die Fehlerart (Regel 6)
    host.hintergrund.stop()


def test_inventory_that_hangs_ends_at_its_deadline(tmp_path, monkeypatch):
    import threading
    frei = threading.Event()
    host, _, clock = _host(tmp_path, monkeypatch)
    monkeypatch.setattr(geraet_host.inventur, "inventur", lambda ha, f: frei.wait(5))
    outcome = _register(host, tmp_path)._handlers["inventory"]({"stunden": 0, "filter": {}})
    assert outcome.check() is None
    clock.advance(geraet_host.INVENTUR_SECONDS)
    done = outcome.check()
    assert isinstance(done, Failed) and done.grund == "ha_nicht_erreichbar"
    frei.set()
    host.hintergrund.stop(timeout=2)


def test_portal_operation_reaches_the_worker(tmp_path, monkeypatch):
    host, _, clock = _host(tmp_path, monkeypatch)
    worker = RegulationWorker(clock)
    seen = []
    worker.register(EV_TARGET_INPUT, lambda event: seen.append(event.data))
    host.bedienung_uebernommen({"raum_soll": 22.0})  # vor dem Binden: nachgestellt
    host.worker_binden(worker)
    worker.run_pending()
    host.bedienung_uebernommen({"raum_soll": 22.5})
    host.bedienung_uebernommen({"raum_soll": True})  # kein Wert
    worker.run_pending()
    assert seen == [{"value": 22.0, "source": "portal"}, {"value": 22.5, "source": "portal"}]


def test_in_the_idle_state_operations_do_not_reach_the_worker(tmp_path, monkeypatch, caplog):
    host, _, clock = _host(tmp_path, monkeypatch)
    worker = RegulationWorker(clock)
    host.bedienung_uebernommen({"raum_soll": 22.0})
    host.worker_binden(worker, soll_eingang=False)
    host.bedienung_uebernommen({"raum_soll": 22.5})
    worker.run_pending()
    assert "Kein Handler" not in caplog.text


def test_configuration_change_requests_a_restart_and_empty_marks_cleanup(tmp_path, monkeypatch):
    host, _, clock = _host(tmp_path, monkeypatch)
    alt = konfiguration_aus_optionen(OPTIONS)
    host.konfiguration_uebernommen(alt, {**alt, "anlage": None})
    assert json.loads(config.AUFRAEUMEN_PATH.read_text()) == {"anlage": alt["anlage"]} and host._neustart
    worker = RegulationWorker(clock)
    reloads = []
    worker.register(geraet_host.EV_RELOAD, lambda event: reloads.append(event.kind))
    host.worker_binden(worker)  # vor dem Binden angefordert: nachgestellt
    worker.run_pending()
    assert reloads == [geraet_host.EV_RELOAD]


def test_an_irrelevant_configuration_change_keeps_running(tmp_path, monkeypatch):
    host, _, _ = _host(tmp_path, monkeypatch)
    alt = konfiguration_aus_optionen(OPTIONS)
    host.konfiguration_uebernommen(alt, {**alt, "profil": "anderes"})
    assert not host._neustart and not config.AUFRAEUMEN_PATH.exists()


def test_wish_goes_to_the_device_with_origin(tmp_path, monkeypatch):
    host, device, _ = _host(tmp_path, monkeypatch)
    assert host.wunsch(30.0, "soll_entity", "nutzer") == 25.0
    host.wunsch(22.5, herkunft="smartheat_entity")
    host.soll_quelle_aus(True, 21.0)
    assert device.wuensche == [(30.0, "soll_entity", "nutzer"), (22.5, "smartheat_entity", None)]
    assert device.aus == [(True, 21.0)]


def test_document_view_for_the_runtime(tmp_path, monkeypatch):
    konf = konfiguration_aus_optionen(OPTIONS)
    host, _, _ = _host(tmp_path, monkeypatch, device=Device(konf, {"raum_soll": 21.0}, dirty=True))
    assert host.konfiguration() == konf and host.bedienung() == ({"raum_soll": 21.0}, True)
    assert host.abo_status() == "aktiv"
    unverbunden = geraet_host.HaDeviceHost(FakeHa(), Supervisor())
    assert unverbunden.konfiguration() is None and unverbunden.bedienung() == (None, False)
    assert unverbunden.wunsch(30.0, "soll_entity") == 30.0


def test_the_ha_check_is_skipped_against_the_accepted_configuration(tmp_path, monkeypatch):
    """Controller-Entscheid 1: treiber_pruefen gibt die uebernommene Konfiguration mit; bleiben Bindung, Raumfuehler und
    Hebelsatz gleich (etwa nur das Abo aendert sich), braucht es HA nicht."""
    ha = FakeHa()
    ha.offline = True
    akzeptiert = konfiguration_aus_optionen(OPTIONS)
    host, _, _ = _host(tmp_path, monkeypatch, ha=ha, device=Device(akzeptiert))
    assert host.treiber_pruefen({**akzeptiert, "abo": "inaktiv"}) is None
    with pytest.raises(NichtPruefbar):  # neue Bindung: HA-Teil noetig, HA aus -> spaeter erneut
        host.treiber_pruefen({**akzeptiert, "raumfuehler": ["sensor.anderer"]})


def test_the_ha_check_gets_the_accepted_configuration(tmp_path, monkeypatch):
    akzeptiert = konfiguration_aus_optionen(OPTIONS)
    calls = []
    monkeypatch.setattr(dokument, "zuordnung_pruefen", lambda k, ha, a: calls.append((k, ha, a)) or "Text")
    host, _, _ = _host(tmp_path, monkeypatch, device=Device(akzeptiert))
    assert host.treiber_pruefen({"neu": 1}) == "Text"
    assert calls == [({"neu": 1}, host._ha, akzeptiert)]
    host.verbinden(Device(None))
    host.treiber_pruefen({"neu": 2})
    assert calls[-1][2] is None


def test_sign_off_marks_restarts_and_reports_the_restore_point(tmp_path, monkeypatch):
    konf = konfiguration_aus_optionen(OPTIONS)
    host, _, clock = _host(tmp_path, monkeypatch, device=Device(konf))
    config.BACKUP_PATH.write_text(json.dumps({"restore_point": {"curve": 1.1}}))
    register = _register(host, tmp_path)
    outcome = register._handlers["sign_off"]({})
    assert isinstance(outcome, Waiting) and host._neustart
    assert json.loads(config.ABGEMELDET_PATH.read_text()) == {"anlage": konf["anlage"]}
    assert outcome.check() is None
    host.runtime_status_event = {"status": STATUS_ABGEMELDET, "tenant_id": konf["anlage"], "grund": None}
    assert outcome.check() == Done({"zurueckgesetzt": True, "werte": {"curve": 1.1}})
    host._neustart = False  # erneut zugestellt: dieselbe Markierung, kein zweiter Neustart
    register._handlers["sign_off"]({})
    assert not host._neustart


def test_sign_off_without_setup_fails(tmp_path, monkeypatch):
    host, _, _ = _host(tmp_path, monkeypatch)
    outcome = _register(host, tmp_path)._handlers["sign_off"]({})
    assert isinstance(outcome, Failed) and outcome.grund == "nicht_eingerichtet"


def test_diagnostics(tmp_path, monkeypatch):
    host, _, _ = _host(tmp_path, monkeypatch)
    register = _register(host, tmp_path)
    assert register._handlers["diagnostics"]({}).result["zustand"] == "startet"
    host.anzeigen(Anzeige("regelt", True, "uebernommen", True, (), (), None))
    host.uhr_synchron()
    host.runtime_status_event = {"status": "regelt"}
    result = register._handlers["diagnostics"]({}).result
    assert (result["zustand"], result["eingerichtet"], result["server_ok"], result["laufzeit"],
            result["uhr_synchron"]) == ("regelt", True, True, "regelt", True)


def test_display_goes_to_the_status_sink_and_never_raises(tmp_path, monkeypatch, caplog):
    host, _, _ = _host(tmp_path, monkeypatch)
    anzeige = Anzeige("regelt", True, "uebernommen", True, (), (), None)
    host.anzeigen(anzeige)  # ohne Sink
    seen = []
    host.status_sink = type("S", (), {"anzeige": lambda self, a: seen.append(a)})()
    host.anzeigen(anzeige)
    assert seen == [anzeige] and host.letzte_anzeige == anzeige

    def kaputt(a):
        raise RuntimeError("weg")

    host.status_sink = type("S", (), {"anzeige": lambda self, a: kaputt(a)})()
    caplog.clear()
    host.anzeigen(anzeige)  # wirft nicht
    assert "Status-Ereignis nicht gesendet (RuntimeError)" in caplog.text and "weg" not in caplog.text


class _Discovery:
    def __init__(self, error=None):
        self.calls, self.error = [], error

    def aktualisieren(self, geraet_id, zustand, claim_code):
        self.calls.append((geraet_id, zustand, claim_code))
        if self.error:
            raise self.error


def test_display_reports_to_the_discovery_with_the_code_only_while_it_may_be_shown(tmp_path, monkeypatch):
    """Spec 5c 1.1/F1: Code nur, solange das Geraet nicht uebernommen ist (Anzeige.code_zeigen)."""
    host, device, _ = _host(tmp_path, monkeypatch)
    device.identity = type("I", (), {"device_id": "shg-a"})()
    host.anzeigen(Anzeige("startet", False, "nicht_uebernommen", False, (), (), None))  # ohne Discovery: nichts
    host.discovery = _Discovery()
    host.claim_code = lambda: "ABCDEFGHIJKL"
    host.anzeigen(Anzeige("nicht_uebernommen", True, "nicht_uebernommen", False, (), (), None))
    host.anzeigen(Anzeige("wartet_auf_einrichtung", True, "uebernommen", False, (), (), None))
    host.anzeigen(Anzeige("regelt", True, None, True, (), (), None))
    assert host.discovery.calls == [("shg-a", "nicht_uebernommen", "ABCDEFGHIJKL"), ("shg-a", "uebernommen", None),
                                    ("shg-a", None, None)]


def test_a_failing_discovery_never_breaks_the_display(tmp_path, monkeypatch, caplog):
    host, device, _ = _host(tmp_path, monkeypatch)
    device.identity = type("I", (), {"device_id": "shg-a"})()
    host.discovery = _Discovery(error=RuntimeError("geheim"))
    anzeige = Anzeige("regelt", True, "uebernommen", True, (), (), None)
    host.anzeigen(anzeige)
    assert host.letzte_anzeige == anzeige
    assert "Discovery nicht aktualisiert (RuntimeError)" in caplog.text and "geheim" not in caplog.text


def test_runtime_status_and_room(tmp_path, monkeypatch):
    host, _, _ = _host(tmp_path, monkeypatch)
    assert host.runtime_status() is None and host.raum() is None
    host.runtime_status_event = {"status": "regelt"}
    host.raum_merken({"ist": 20.0, "soll": 21.0, "soll_quelle": "portal", "ts": "t"})
    assert host.runtime_status() == {"status": "regelt"} and host.raum()["ist"] == 20.0


def test_notify_sink_sends_to_the_given_services(tmp_path, monkeypatch):
    ha = FakeHa()
    host, _, _ = _host(tmp_path, monkeypatch, ha=ha)
    host.notify_sink_fuer(["notify.handy"]).push("k", "Hallo")
    assert ha.pushes == ["Hallo"]


def test_runtime_messages_go_to_ha_and_to_the_device_core(tmp_path, monkeypatch):
    """Spec 5c 3 (Portal zeigt HA-Geraete wie Gateways, mit Hinweisen): Meldungen der Laufzeit gehen wie beim Gateway
    (MeldungSink) als up/notifications an den Geraetekern und weiter an HA (Push, persistent_notification)."""
    ha = FakeHa()
    host, device, _ = _host(tmp_path, monkeypatch, ha=ha)
    sink = host.notify_sink_fuer(["notify.handy"])
    sink.show("notbetrieb", "Notbetrieb aktiv")
    sink.push("notbetrieb", "Notbetrieb aktiv")
    sink.push("batterie:sensor.x", "Batterie schwach")
    sink.withdraw("notbetrieb")
    assert device.meldungen == [
        ("notbetrieb", "Notbetrieb aktiv", True, True), ("notbetrieb", "Notbetrieb aktiv", True, True),
        ("batterie:sensor.x", "Batterie schwach", False, False), ("notbetrieb", "Notbetrieb aktiv", False, True)]
    assert ha.pushes == ["Notbetrieb aktiv", "Batterie schwach"]
    assert ha.persistent == [("smartheat_notbetrieb", "Notbetrieb aktiv")]
    assert ha.dismissed == ["smartheat_notbetrieb"]


def test_a_failing_ha_notification_still_reaches_the_device_core(tmp_path, monkeypatch):
    class _HaDown(FakeHa):
        def create_persistent_notification(self, title, message, notification_id):
            raise ConnectionError("HA Core startet")

        def dismiss_persistent_notification(self, notification_id):
            raise ConnectionError("HA Core startet")

    host, device, _ = _host(tmp_path, monkeypatch, ha=_HaDown())
    sink = host.notify_sink_fuer([])
    with pytest.raises(ConnectionError):  # der Notifier faengt und loggt das (show/withdraw duerfen werfen)
        sink.show("schreibbudget", "Budget erschoepft")
    with pytest.raises(ConnectionError):
        sink.withdraw("schreibbudget")
    assert device.meldungen == [("schreibbudget", "Budget erschoepft", True, True),
                                ("schreibbudget", "Budget erschoepft", False, True)]


def test_the_notifier_reports_a_critical_runtime_message_to_the_device_core(tmp_path, monkeypatch):
    """Durch den echten Notifier der Laufzeit: Notbetrieb an, dann wieder ok."""
    from smartheat_runtime.notifier import Notifier
    from smartheat_runtime.state import StateStore
    ha = FakeHa()
    host, device, _ = _host(tmp_path, monkeypatch, ha=ha)
    notifier = Notifier(StateStore(tmp_path / "backup.json", tmp_path / "failsafe.json"),
                        host.notify_sink_fuer(["notify.handy"]))
    notifier.notify("notbetrieb", "aktiv", "Notbetrieb aktiv", critical=True)
    notifier.notify("notbetrieb", "ok", "Notbetrieb beendet", critical=True)
    # Regeln des Gateways (MeldungSink): der Geraetekern sendet je key nur den letzten Stand (Upsert), am Ende geschlossen
    assert device.meldungen == [("notbetrieb", "Notbetrieb aktiv", False, False),  # Push vor der offenen Meldung
                                ("notbetrieb", "Notbetrieb aktiv", True, True),
                                ("notbetrieb", "Notbetrieb beendet", True, True),
                                ("notbetrieb", "Notbetrieb aktiv", False, True)]
    assert ha.persistent == [("smartheat_notbetrieb", "Notbetrieb aktiv")] and ha.dismissed == ["smartheat_notbetrieb"]


# --- Raum fuer up/status (Controller-Entscheid 2) und Soll-Speicher mit dem echten Geraete-Host -------------------

def _laufzeit(env, *, bed=None, dirty=False, **overrides):
    device = Device(konfiguration_aus_optionen({**OPTIONS, **overrides}), bed, dirty)
    geraet = geraet_host.HaDeviceHost(env.ha, Supervisor(), clock=env.clock)
    geraet.verbinden(device)
    host = HaHost(geraet, env.ha, clock=env.clock)
    loaded = host.load()
    worker = RegulationWorker(env.clock)
    host.trigger_source(loaded.manifest, worker)
    geraet.worker_binden(worker)
    return geraet, host, worker, device


def test_the_room_carries_actual_target_and_source(env):
    geraet, host, worker, _ = _laufzeit(env, bed={"raum_soll": 21.0})
    assert geraet.raum() is None  # erst der Worker veroeffentlicht
    worker.run_pending()
    raum = geraet.raum()
    assert set(raum) == set(wire.RAUM_FIELDS)
    assert (raum["ist"], raum["soll"], raum["soll_quelle"]) == (20.0, 21.0, "portal") and raum["ts"]
    env.ha.states["sensor.room_actual"] = 20.5
    env.clock.advance(RAUM_SECONDS)
    worker.run_pending()
    assert geraet.raum()["ist"] == 20.5


def test_a_target_change_publishes_the_room_at_once(env):
    geraet, host, worker, _ = _laufzeit(env, bed={"raum_soll": 21.0})
    worker.run_pending()
    geraet.bedienung_uebernommen({"raum_soll": 22.0})
    worker.run_pending()
    assert (geraet.raum()["soll"], geraet.raum()["soll_quelle"]) == (22.0, "portal")
    assert host.store.value == 22.0


def test_an_unreadable_room_actual_is_none_and_the_tick_continues(env):
    geraet, host, worker, _ = _laufzeit(env, bed={"raum_soll": 21.0})
    env.ha.states["sensor.room_actual"] = ValueError("unavailable")
    worker.run_pending()
    assert geraet.raum()["ist"] is None and geraet.raum()["soll"] == 21.0
    env.ha.states["sensor.room_actual"] = 19.0
    env.clock.advance(RAUM_SECONDS)
    worker.run_pending()
    assert geraet.raum()["ist"] == 19.0
    assert sum(1 for _, _, event in worker._schedule if event.kind == EV_RAUM) == 1


def test_a_new_operation_during_operation_is_applied_and_written_to_the_entity(env):
    """Controller-Entscheid 3c: Bedienung -> Soll-Eingang (portal) -> Soll-Speicher und Soll-Entity, ohne Wunsch."""
    env.ha.states.update({"climate.wz": "heat", "climate.wz::temperature": 21.0})
    geraet, host, worker, device = _laufzeit(env, bed={"raum_soll": 21.0}, entity_room_target="climate.wz::temperature")
    worker.run_pending()
    assert env.ha.writes == []  # zeigt schon den Wert
    geraet.bedienung_uebernommen({"raum_soll": 23.0})
    worker.run_pending()
    assert host.store.value == 23.0 and ("climate.wz::temperature", 23.0) in env.ha.writes
    assert host.store.echo_rest() > 0 and device.wuensche == []
