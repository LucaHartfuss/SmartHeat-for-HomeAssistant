"""Spec 5b 3.4 (Plan D2 Task 2): Befehle des Gateways im Befehlsregister des Geraetekerns. Einmal-Ausfuehrung und
Gruende-Pruefung testet D1; hier die Handler und die Uebersetzung der Treiber-Fehler."""
import json
import threading
import time

import pytest
from configs import SENSOR, THERMOSTAT, bedienung, dokumente_schreiben, konfiguration
from fake_z2m import FakeZigbee2Mqtt
from fakes import FakeBus, SofortHintergrund
from world import GatewayWorld

from smartheat_device import wire
from smartheat_device.commands import CommandRegister, Done, Failed, Waiting
from smartheat_gateway import befehle
from smartheat_gateway.drivers.base import DriverError
from smartheat_gateway.drivers.simulation import SimulationDriver
from smartheat_gateway.files import write_json
from smartheat_gateway.paths import Paths
from smartheat_gateway.quota import QuotaExhausted
from smartheat_gateway.zigbee import ZigbeeMirror


class Umgebung:
    def __init__(self) -> None:
        self.konf: dict | None = konfiguration()
        self.status: dict | None = None
        self.restarts: list[int] = []


@pytest.fixture
def ctx(data_dir, clock):
    bus = FakeBus()
    z2m = FakeZigbee2Mqtt(bus)
    mirror = ZigbeeMirror(bus, clock)
    mirror.start()
    z2m.bridge(online=True)
    z2m.add_sensor(SENSOR)
    z2m.add_thermostat(THERMOSTAT)
    z2m.report(SENSOR, temperature=20.5, battery=80)
    umgebung = Umgebung()
    context = befehle.Kontext(
        Paths(data_dir), mirror, konfiguration=lambda: umgebung.konf, runtime_status=lambda: umgebung.status,
        neustart=lambda: umgebung.restarts.append(1), diagnostics=lambda: {"geraet": "shg-x"}, clock=clock,
        wall=lambda: 1_700_000_000.0, hintergrund=SofortHintergrund())
    context.z2m = z2m
    context.umgebung = umgebung
    return context


@pytest.fixture
def register(ctx, tmp_path, clock):
    sent: list[dict] = []
    reg = CommandRegister(tmp_path / "befehle", lambda body: sent.append(body) or True, clock=clock,
                          uebersetzen=befehle.fehler_uebersetzen)
    befehle.eintragen(reg, ctx)
    reg.sent = sent
    return reg


def _run(register, kind, payload, command_id="c1"):
    register.empfangen({"schema": 1, "command_id": command_id, "kind": kind, "payload": payload,
                        "expires_at": "2027-01-01T00:00:00+00:00"})
    return register.sent[-1] if register.sent else None


def test_the_gateway_registers_exactly_its_commands(register):
    assert set(befehle.HANDLERS) == {"zigbee_permit_join", "zigbee_devices", "driver_login", "driver_probe",
                                     "inventory", "diagnostics", "sign_off"}
    assert set(befehle.HANDLERS) <= set(wire.COMMANDS) and set(befehle.HANDLERS) <= register.kinds


def test_bad_payloads(register):
    assert _run(register, "zigbee_permit_join", {"seconds": 999})["error"]["grund"] == "ungueltige_nutzlast"
    assert _run(register, "zigbee_permit_join", {"seconds": True}, "c2")["error"]["grund"] == "ungueltige_nutzlast"


def test_permit_join_and_device_list(ctx, register):
    assert _run(register, "zigbee_permit_join", {"seconds": 120})["ok"] is True
    assert ctx.z2m.permit_join_requests == [120]
    devices = _run(register, "zigbee_devices", {}, "c2")["result"]["devices"]
    sensor = next(d for d in devices if d["ieee"] == SENSOR)
    assert sensor["art"] == "fuehler" and sensor["werte"]["temperature"] == 20.5 and sensor["batterie"] == 80
    assert set(sensor) == set(wire.ZIGBEE_DEVICE_FIELDS)
    assert sensor["last_seen"] == "2023-11-14T22:13:20+00:00"


def test_zigbee_not_ready(ctx, register):
    ctx.z2m.bridge(online=False)
    assert _run(register, "zigbee_devices", {})["error"]["grund"] == "zigbee_nicht_bereit"
    assert _run(register, "zigbee_permit_join", {"seconds": 60}, "c2")["error"]["grund"] == "zigbee_nicht_bereit"
    assert ctx.z2m.permit_join_requests == []


def test_probe_adds_safety_warnings(register):
    result = _run(register, "driver_probe", {"driver_id": "simulation"})["result"]
    assert result["driver_id"] == "simulation" and all("sicherheitswarnungen" in k for k in result["kandidaten"])


def test_driver_errors_become_their_contract_reason(monkeypatch, register):
    def down(self):
        raise DriverError("anlage_nicht_erreichbar", "Die Anlage antwortet nicht.")

    monkeypatch.setattr("smartheat_gateway.drivers.simulation.SimulationDriver.probe", down)
    assert _run(register, "driver_probe", {"driver_id": "simulation"})["error"] == {
        "grund": "anlage_nicht_erreichbar", "text": "Die Anlage antwortet nicht."}


def test_quota_exhausted_is_reported_where_the_command_lists_it(monkeypatch, register):
    def quota(self):
        raise QuotaExhausted("x")

    monkeypatch.setattr("smartheat_gateway.drivers.simulation.SimulationDriver.probe", quota)
    assert _run(register, "driver_probe", {"driver_id": "simulation"})["error"]["grund"] == "kontingent_erschoepft"


def test_unknown_driver_and_login_not_needed(register):
    assert _run(register, "driver_probe", {"driver_id": "erfunden"})["error"]["grund"] == "treiber_unbekannt"
    login = _run(register, "driver_login", {"phase": "begin", "driver_id": "simulation"}, "c2")
    assert login["error"]["grund"] == "login_nicht_noetig"


def test_inventory_needs_a_configuration(ctx, register):
    ctx.umgebung.konf = None
    assert _run(register, "inventory", {"stunden": 1})["error"]["grund"] == "nicht_eingerichtet"
    ctx.umgebung.konf = {"anlage": None}
    assert _run(register, "inventory", {"stunden": 1}, "c2")["error"]["grund"] == "nicht_eingerichtet"


def test_inventory_of_the_simulation_goes_up_as_inventory_data(ctx, clock):
    outcome = befehle.inventory(ctx, {"stunden": 1})
    assert isinstance(outcome, Waiting)
    clock.advance(3601)
    done = outcome.check()
    assert isinstance(done, Done) and done.result == {} and isinstance(done.inventur["proben"], list)
    assert len(done.inventur["proben"]) == 1


def test_diagnostics_is_the_snapshot_without_secrets(register):
    result = _run(register, "diagnostics", {})["result"]
    assert result == {"geraet": "shg-x"}


def test_sign_off_marks_restarts_and_finishes_when_the_runtime_is_signed_off(ctx):
    outcome = befehle.sign_off(ctx, {})
    assert isinstance(outcome, Waiting) and ctx.umgebung.restarts == [1]
    assert json.loads(ctx.paths.abgemeldet.read_text()) == {"anlage": "test-tenant"}
    assert outcome.check() is None
    ctx.umgebung.status = {"status": "abgemeldet", "tenant_id": "andere", "grund": None}
    assert outcome.check() is None
    write_json(ctx.paths.backup, {"restore_point": {"curve": 1.1, "room_setpoint": 21.0}})
    ctx.umgebung.status = {"status": "abgemeldet", "tenant_id": "test-tenant", "grund": None}
    assert outcome.check() == Done({"zurueckgesetzt": True, "werte": {"curve": 1.1, "room_setpoint": 21.0}})


def test_sign_off_after_the_restart_does_not_restart_again(ctx):
    befehle.sign_off(ctx, {})
    befehle.sign_off(ctx, {})  # erneute Zustellung nach dem Neustart
    assert ctx.umgebung.restarts == [1]


def test_sign_off_through_the_register_survives_the_restart_it_causes(ctx, tmp_path, clock):
    """Der Neustart gehoert zum Befehl: das Register stellt ihn nach dem Neustart erneut zu (D1: wiederholbar), die
    Markierung steht schon, es gibt keinen zweiten Neustart, und das Ergebnis geht hoch, sobald der Status abgemeldet ist."""
    befehl = {"schema": 1, "command_id": "c1", "kind": "sign_off", "payload": {},
              "expires_at": "2027-01-01T00:00:00+00:00"}
    sent: list[dict] = []

    def neues_register() -> CommandRegister:
        reg = CommandRegister(tmp_path / "befehle", lambda body: sent.append(body) or True, clock=clock,
                              uebersetzen=befehle.fehler_uebersetzen)
        befehle.eintragen(reg, ctx)
        return reg

    erstes = neues_register()
    erstes.empfangen(befehl)
    assert ctx.umgebung.restarts == [1] and sent == []
    assert json.loads(ctx.paths.abgemeldet.read_text()) == {"anlage": "test-tenant"}
    neustart = neues_register()  # der Prozess startet neu, der Server stellt den Befehl nach hello erneut zu
    neustart.empfangen(befehl)
    neustart.pruefen()
    assert ctx.umgebung.restarts == [1] and sent == []  # kein zweiter Neustart, noch kein Ergebnis
    ctx.umgebung.status = {"status": "abgemeldet", "tenant_id": "andere", "grund": None}
    neustart.pruefen()
    assert sent == []
    ctx.umgebung.status = {"status": "abgemeldet", "tenant_id": "test-tenant", "grund": None}
    neustart.pruefen()
    assert sent == [{"schema": 1, "command_id": "c1", "ok": True,
                     "result": {"zurueckgesetzt": True, "werte": {}}, "error": None}]
    neustart.empfangen(befehl)  # fertig: das gespeicherte Ergebnis, kein weiterer Lauf
    assert len(sent) == 2 and sent[1] == sent[0] and ctx.umgebung.restarts == [1]


def test_sign_off_with_a_failed_restore_reports_it(ctx):
    outcome = befehle.sign_off(ctx, {})
    ctx.umgebung.status = {"status": "abgemeldet", "tenant_id": "test-tenant", "grund": "Zurücksetzen scheitert"}
    assert outcome.check() == Done({"zurueckgesetzt": False, "werte": {}})


def test_sign_off_of_a_device_that_is_not_set_up(ctx):
    ctx.umgebung.konf = {"anlage": None}
    assert befehle.sign_off(ctx, {}) == Failed("nicht_eingerichtet", befehle.NOT_SET_UP_TEXT)
    assert ctx.umgebung.restarts == []


def test_every_reason_the_handlers_produce_is_listed():
    produced = {
        "zigbee_permit_join": {"ungueltige_nutzlast", "zigbee_nicht_bereit"},
        "zigbee_devices": {"zigbee_nicht_bereit"},
        "driver_login": {"ungueltige_nutzlast", "treiber_unbekannt", "login_nicht_noetig", "login_fehlgeschlagen",
                         "anlage_nicht_erreichbar"},
        "driver_probe": {"treiber_unbekannt", "nicht_angemeldet", "kontingent_erschoepft", "anlage_nicht_erreichbar"},
        "inventory": {"nicht_eingerichtet", "treiber_unbekannt", "nicht_angemeldet", "kontingent_erschoepft",
                      "anlage_nicht_erreichbar", "ungueltige_nutzlast"},
        "sign_off": {"nicht_eingerichtet", "keine_bestaetigung"},
        "diagnostics": set(),
    }
    for kind, reasons in produced.items():
        assert reasons <= set(wire.command_errors(kind)), kind


def server_calls(server) -> int:
    return len(server.calls)


def test_inventory_samples_every_15_minutes_and_returns_the_series(vicare_kontext):
    ctx, server, clock = vicare_kontext
    outcome = befehle.inventory(ctx, {"stunden": 1})
    assert isinstance(outcome, Waiting)
    done = None
    for _ in range(5):  # 5 x 15 min > 1 h
        clock.advance(900)
        ctx.wall = lambda: 1_000_000 + clock()
        done = outcome.check()
        if done is not None:
            break
    assert isinstance(done, Done)
    assert len(done.inventur["proben"]) == 3  # bei 900, 1800 und 2700 s; bei 3600 s ist das Fenster zu
    assert "Seriennummer" not in str(done.inventur) and "7637415" not in str(done.inventur)


def test_a_restart_in_the_middle_resumes_the_series_instead_of_starting_a_second_one(vicare_kontext):
    ctx, server, clock = vicare_kontext
    first = befehle.inventory(ctx, {"stunden": 2})
    clock.advance(900)
    first.check()
    used = server_calls(server)
    second = befehle.inventory(ctx, {"stunden": 2})  # erneute Zustellung
    second.check()
    assert server_calls(server) == used  # nichts doppelt abgerufen
    saved = json.loads(ctx.paths.inventur.read_text())
    assert saved["driver_id"] == "vicare_cloud" and len(saved["samples"]) >= 1


def test_a_series_for_another_driver_or_older_than_its_window_starts_fresh(vicare_kontext):
    ctx, server, clock = vicare_kontext
    write_json(ctx.paths.inventur,
               {"driver_id": "simulation", "started": 0, "hours": 1, "samples": [{}], "next_at": 0})
    outcome = befehle.inventory(ctx, {"stunden": 1})
    outcome.check()
    assert json.loads(ctx.paths.inventur.read_text())["driver_id"] == "vicare_cloud"


def test_the_saved_series_is_private_free_of_ids_and_not_resampled_within_15_minutes(vicare_kontext):
    ctx, server, clock = vicare_kontext
    outcome = befehle.inventory(ctx, {"stunden": 2})
    clock.advance(900)
    outcome.check()
    used = server_calls(server)
    outcome.check()  # erneuter Aufruf derselben Pruefung im selben 15-Minuten-Fenster: keine zweite Probe
    clock.advance(300)
    outcome.check()
    assert server_calls(server) == used
    text = ctx.paths.inventur.read_text()
    assert ctx.paths.inventur.stat().st_mode & 0o777 == 0o600
    for secret in ("7637415", "2012345", "access_token", "refresh_token", "Bearer"):
        assert secret not in text


def test_a_series_that_starts_in_the_future_after_a_backward_clock_jump_starts_fresh(vicare_kontext):
    ctx, server, clock = vicare_kontext
    write_json(ctx.paths.inventur, {
        "driver_id": "vicare_cloud", "started": ctx.wall() + 90_000, "hours": 1, "samples": [{"ts": 1.0}],
        "next_at": ctx.wall() + 90_000,
    })
    befehle.inventory(ctx, {"stunden": 1})
    saved = json.loads(ctx.paths.inventur.read_text())
    assert saved["samples"] == [] and saved["started"] == ctx.wall()


def test_two_live_commands_for_the_same_series_sample_each_boundary_once(vicare_kontext):
    ctx, server, clock = vicare_kontext
    first = befehle.inventory(ctx, {"stunden": 1})
    second = befehle.inventory(ctx, {"stunden": 1})  # neue Command-ID
    clock.advance(900)
    first.check()
    used = server_calls(server)
    second.check()
    assert server_calls(server) == used  # die zweite Pruefung nimmt die Probe der ersten aus der Datei
    assert len(json.loads(ctx.paths.inventur.read_text())["samples"]) == 1
    done = []
    for _ in range(4):
        clock.advance(900)
        ctx.wall = lambda: 1_000_000 + clock()
        done = [first.check(), second.check()]
        if all(d is not None for d in done):
            break
    assert all(isinstance(d, Done) for d in done)
    assert done[0].inventur["proben"] == done[1].inventur["proben"] and len(done[0].inventur["proben"]) == 3


def test_a_pending_inventory_does_not_recreate_its_series_after_the_setup_was_removed(vicare_kontext):
    ctx, server, clock = vicare_kontext
    outcome = befehle.inventory(ctx, {"stunden": 2})
    clock.advance(900)
    ctx.wall = lambda: 1_000_000 + clock()
    assert outcome.check() is None and len(json.loads(ctx.paths.inventur.read_text())["samples"]) == 1
    befehle.forget_inventory(ctx.paths)  # Abmelden oder Neueinrichtung
    used = server_calls(server)
    clock.advance(900)
    ctx.wall = lambda: 1_000_000 + clock()
    result = outcome.check()
    assert isinstance(result, Failed) and result.grund in wire.command_errors("inventory")
    assert not ctx.paths.inventur.exists() and server_calls(server) == used
    clock.advance(7200)
    ctx.wall = lambda: 1_000_000 + clock()
    assert isinstance(outcome.check(), Failed)  # auch nach Ablauf des Fensters keine alten Proben als Done
    assert not ctx.paths.inventur.exists()


# --- Inventur: eine fehlgeschlagene Probe darf die Reihe nicht beenden ---


def _eigene_logs(caplog) -> str:
    """Was das Gateway loggt; urllib3 loggt auf DEBUG die URL (mit Anlagen- und Seriennummer aus den Treiber-Parametern)
    selbst, das prueft dieser Test nicht."""
    return "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("smartheat_gateway"))


def _tick(ctx, clock, outcome, seconds=900):
    clock.advance(seconds)
    ctx.wall = lambda: 1_000_000 + clock()
    return outcome.check()


def _run_to_the_end(ctx, clock, outcome, limit=5):
    """Prueft im 15-Minuten-Takt bis zum Ergebnis; liefert (Ergebnis, Anzahl der Pruefungen)."""
    for ticks in range(1, limit + 1):
        result = _tick(ctx, clock, outcome)
        if result is not None:
            return result, ticks
    return None, limit


def test_a_failed_sample_is_skipped_and_the_inventory_still_finishes_with_the_others(vicare_kontext, caplog):
    ctx, server, clock = vicare_kontext
    outcome = befehle.inventory(ctx, {"stunden": 2})
    assert _tick(ctx, clock, outcome) is None  # 900 s: Probe 1
    server.control({"offline": True})
    with caplog.at_level("DEBUG"):
        assert _tick(ctx, clock, outcome) is None  # 1800 s: Cloud antwortet mit 503, die Probe faellt aus
    server.control({"offline": False})
    saved = json.loads(ctx.paths.inventur.read_text())
    assert len(saved["samples"]) == 1 and saved["next_at"] == ctx.wall() + befehle.INVENTORY_EVERY_SECONDS
    done = None
    for _ in range(6):
        done = _tick(ctx, clock, outcome)
        if done is not None:
            break
    assert isinstance(done, Done)
    assert len(done.inventur["proben"]) == 6  # sieben Zeitpunkte (900 ... 6300 s) minus der ausgefallene
    for secret in ("7637415", "2012345", "access_token", "refresh_token", "Bearer"):
        assert secret not in _eigene_logs(caplog) and secret not in str(done.inventur)
    assert "DriverError" in _eigene_logs(caplog)  # nur der Typname der Ausnahme


def test_after_a_failed_sample_the_next_attempt_waits_for_next_at(vicare_kontext):
    ctx, server, clock = vicare_kontext
    outcome = befehle.inventory(ctx, {"stunden": 2})
    server.control({"offline": True})
    assert _tick(ctx, clock, outcome) is None  # 900 s: Fehlversuch
    attempted = server_calls(server)
    assert attempted >= 1
    server.control({"offline": False})
    assert _tick(ctx, clock, outcome, 300) is None  # 1200 s < next_at (1800 s): kein Abruf
    assert server_calls(server) == attempted
    assert _tick(ctx, clock, outcome, 600) is None  # 1800 s: naechster Versuch
    assert server_calls(server) > attempted
    assert len(json.loads(ctx.paths.inventur.read_text())["samples"]) == 1


def test_an_inventory_without_any_sample_fails_at_the_deadline_with_a_contract_reason(vicare_kontext, caplog):
    ctx, server, clock = vicare_kontext
    outcome = befehle.inventory(ctx, {"stunden": 1})
    server.control({"offline": True})
    with caplog.at_level("DEBUG"):
        result, ticks = _run_to_the_end(ctx, clock, outcome)
    assert ticks == 4  # erst am Ende des Fensters (3600 s), nicht schon beim ersten Fehlversuch
    assert isinstance(result, Failed)
    assert result.grund == "anlage_nicht_erreichbar" and result.grund in wire.command_errors("inventory")
    assert json.loads(ctx.paths.inventur.read_text())["samples"] == []
    for secret in ("7637415", "2012345", "access_token", "refresh_token", "Bearer"):
        assert secret not in _eigene_logs(caplog) and secret not in result.text


def test_an_inventory_whose_samples_are_all_rate_limited_reports_the_quota_at_the_deadline(vicare_kontext):
    ctx, server, clock = vicare_kontext
    outcome = befehle.inventory(ctx, {"stunden": 1})
    server.control({"rate_limit_after": len(server.calls)})
    result, ticks = _run_to_the_end(ctx, clock, outcome)
    assert ticks == 4
    assert isinstance(result, Failed) and result.grund == "kontingent_erschoepft"
    assert result.grund in wire.command_errors("inventory")


# --- Blockierende Treiber-Aufrufe (Cloud-HTTP) laufen neben dem Geraete-Thread (Review D2, Important 1) ---
# Bis 0.5.0 liefen Anmeldung, Probe und Inventur im Agenten-Prozess. Im Geraete-Thread wuerde ein Aufruf mit Token-
# Erneuerung und 401-Wiederholung (bis 80 s) down/setpoints, Dokumente und hello aufhalten; die Laufzeit liefe nach
# zwei Ack-Timeouts (je 30 s) in den Notbetrieb.

#: Sicherung: gibt ein haengender Test den blockierten Aufruf nach dieser Zeit frei.
FREIGABE_SPAETESTENS = 5.0
LEVERS = {"curve": 1.4, "level": 3.0, "room_setpoint": 20.0}


@pytest.fixture
def regelnde_welt(data_dir, monkeypatch, clock):
    world = GatewayWorld(data_dir, monkeypatch, clock)
    try:
        dokumente_schreiben(world.paths, konfiguration())
        world.start_runtime()
        world.connect()
        assert world.result.store.state.delivery.pending is not None  # Erststart-Tick wartet auf down/setpoints
        yield world
    finally:
        # Ein Test, der vor frei.set() scheitert, laesst den blockierten Aufruf sonst bis zur Sicherung haengen; erst
        # freigeben, dann den Hintergrund-Thread beenden und die Sicherung (Timer-Thread) abbrechen.
        for freigabe, sicherung in _FREIGABEN:
            freigabe.set()
            sicherung.cancel()
        _FREIGABEN.clear()
        world.close()


#: (frei, Sicherung) der blockierten Aufrufe des laufenden Tests; die Fixture regelnde_welt raeumt sie im Teardown ab.
_FREIGABEN: list[tuple[threading.Event, threading.Timer]] = []


def _blockieren(monkeypatch, ziel: str, danach) -> tuple[threading.Event, threading.Event]:
    """Ersetzt den Treiber-Aufruf `ziel` durch einen, der bis zur Freigabe blockiert und dann `danach(self, ...)` liefert."""
    gestartet, frei = threading.Event(), threading.Event()

    def blockiert(self, *args):
        gestartet.set()
        frei.wait(FREIGABE_SPAETESTENS)
        return danach(self, *args)

    monkeypatch.setattr(ziel, blockiert)
    sicherung = threading.Timer(FREIGABE_SPAETESTENS, frei.set)
    sicherung.daemon = True
    sicherung.start()
    _FREIGABEN.append((frei, sicherung))
    return gestartet, frei


def _befehl(world, command_id: str, kind: str, payload: dict) -> None:
    world.server("down/command", {"schema": 1, "command_id": command_id, "kind": kind, "payload": payload,
                                  "expires_at": "2026-01-17T08:00:00+01:00"})


def _ergebnisse(world, **match) -> list[dict]:
    return [body for link in world.links for body in link.of("up/result")
            if all(body.get(key) == value for key, value in match.items())]


def _bis(world, bedingung, versuche: int = 300):
    """Geraetekern und Worker abarbeiten, bis der Hintergrund fertig ist (echter Thread, kurze Pausen)."""
    for _ in range(versuche):
        world.run()
        if (wert := bedingung()):
            return wert
        time.sleep(0.01)
    raise AssertionError("Hintergrund-Aufruf nicht fertig geworden")


def _geraete_thread_arbeitet_weiter(world, gestartet: threading.Event, starten) -> None:
    """Startet den Befehl; waehrend sein Treiber-Aufruf blockiert, verarbeitet der Geraetekern down/setpoints (die
    Laufzeit bekommt ihre Antwort) und eine Bedienung (Ergebnis geht hoch)."""
    begonnen = time.monotonic()
    starten()
    assert gestartet.wait(2.0)
    world.answer(LEVERS)
    world.bedienung_senden(bedienung(21.0), 1)
    assert time.monotonic() - begonnen < FREIGABE_SPAETESTENS / 2  # nichts hat auf den Treiber gewartet
    assert world.result.store.state.delivery.pending is None  # down/setpoints verarbeitet
    assert _ergebnisse(world, dokument="bedienung", version=1)[-1]["ok"] is True


def test_a_blocking_probe_does_not_stall_the_device_thread(regelnde_welt, monkeypatch):
    world = regelnde_welt
    original = SimulationDriver.probe
    gestartet, frei = _blockieren(monkeypatch, "smartheat_gateway.drivers.simulation.SimulationDriver.probe", original)
    _geraete_thread_arbeitet_weiter(
        world, gestartet, lambda: _befehl(world, "c-probe", "driver_probe", {"driver_id": "simulation"}))
    assert _ergebnisse(world, command_id="c-probe") == []  # Ergebnis erst, wenn der Aufruf fertig ist
    frei.set()
    ergebnis = _bis(world, lambda: _ergebnisse(world, command_id="c-probe"))[-1]
    assert ergebnis["ok"] is True and ergebnis["result"]["driver_id"] == "simulation"
    assert all("sicherheitswarnungen" in kandidat for kandidat in ergebnis["result"]["kandidaten"])


def test_a_blocking_login_does_not_stall_the_device_thread_and_keeps_its_contract_reason(regelnde_welt, monkeypatch):
    world = regelnde_welt

    def abgelehnt(self, params):
        raise DriverError("login_fehlgeschlagen", "Der Anmelde-Code ist abgelaufen.")

    gestartet, frei = _blockieren(
        monkeypatch, "smartheat_gateway.drivers.vicare_cloud.driver.ViCareCloudDriver.login_finish", abgelehnt)
    payload = {"phase": "finish", "driver_id": "vicare_cloud", "code": "abc", "redirect_uri": "https://portal.test/cb"}
    _geraete_thread_arbeitet_weiter(world, gestartet, lambda: _befehl(world, "c-login", "driver_login", payload))
    assert _ergebnisse(world, command_id="c-login") == []
    frei.set()
    ergebnis = _bis(world, lambda: _ergebnisse(world, command_id="c-login"))[-1]
    assert ergebnis["ok"] is False
    assert ergebnis["error"] == {"grund": "login_fehlgeschlagen", "text": "Der Anmelde-Code ist abgelaufen."}


def test_a_blocking_inventory_sample_does_not_stall_the_device_thread(regelnde_welt, monkeypatch):
    world = regelnde_welt
    original = SimulationDriver.inventory_sample
    gestartet, frei = _blockieren(
        monkeypatch, "smartheat_gateway.drivers.simulation.SimulationDriver.inventory_sample", original)
    _geraete_thread_arbeitet_weiter(world, gestartet, lambda: _befehl(world, "c-inv", "inventory", {"stunden": 1}))
    assert json.loads(world.paths.inventur.read_text())["samples"] == []  # die Probe laeuft noch
    frei.set()
    _bis(world, lambda: json.loads(world.paths.inventur.read_text())["samples"])  # eingesammelt im naechsten Takt
    for anzahl in (2, 3, 4):  # Proben bei 900, 1800 und 2700 s, jede im Hintergrund
        world.advance(befehle.INVENTORY_EVERY_SECONDS)
        _bis(world, lambda: len(json.loads(world.paths.inventur.read_text())["samples"]) == anzahl)  # noqa: B023
    world.advance(befehle.INVENTORY_EVERY_SECONDS)  # 3600 s: Fenster zu, Ergebnis
    ergebnis = _bis(world, lambda: _ergebnisse(world, command_id="c-inv"))[-1]
    assert ergebnis["ok"] is True
    proben = world.link.of("up/inventory")[-1]["daten"]["proben"]
    assert len(proben) == 4  # bei 0, 900, 1800 und 2700 s; bei 3600 s ist das Fenster zu


def test_the_background_thread_is_a_daemon_and_stop_cancels_queued_calls():
    hintergrund = befehle.Hintergrund()
    frei, gestartet = threading.Event(), threading.Event()

    def erster_aufruf():
        gestartet.set()
        return frei.wait(FREIGABE_SPAETESTENS)

    erster = hintergrund.submit(erster_aufruf)
    zweiter = hintergrund.submit(lambda: "nie")
    threads = [t for t in threading.enumerate() if t.name.startswith(befehle.HINTERGRUND_NAME)]
    assert threads and all(t.daemon for t in threads)  # haelt das Prozessende nie auf
    # Erst stoppen, wenn der erste Aufruf laeuft: sonst bricht stop() auch ihn als wartend ab (CI-Runner langsamer)
    assert gestartet.wait(2.0)
    hintergrund.stop()
    assert zweiter.cancelled()
    frei.set()
    assert erster.result(timeout=2.0) is True
    spaeter = hintergrund.submit(lambda: "nie")
    with pytest.raises(RuntimeError):
        spaeter.result(timeout=1.0)
