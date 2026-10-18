"""Spec 5b 3.4 (Plan D2 Task 2): Befehle des Gateways im Befehlsregister des Geraetekerns. Einmal-Ausfuehrung und
Gruende-Pruefung testet D1; hier die Handler und die Uebersetzung der Treiber-Fehler."""
import json
import math
import threading
import time

import pytest
from configs import SENSOR, THERMOSTAT, bedienung, dokumente_schreiben, konfiguration
from fake_z2m import FakeZigbee2Mqtt
from fakes import FakeBus, SofortHintergrund
from world import GatewayWorld

from smartheat_core import dateien
from smartheat_device import fehler, wire
from smartheat_device.commands import CommandRegister, Done, Failed, Waiting
from smartheat_gateway import befehle
from smartheat_gateway.drivers.base import DriverError
from smartheat_gateway.drivers.simulation import SimulationDriver
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


@pytest.mark.parametrize("kind", sorted(befehle.HANDLERS))
def test_kein_handler_meldet_einen_text(ctx, register, kind):
    """Spec Stufe 3 S5 8: jeder Fehler eines Gateway-Befehls traegt nur grund und details aus dem Vertrag."""
    ctx.umgebung.konf = None  # nicht eingerichtet: inventory stunden >= 1 und sign_off scheitern
    nutzlasten = [{}, {"seconds": -1}, {"stunden": 0}, {"stunden": 1}, {"stunden": 0, "filter": {"treiber": "erfunden"}},
                  {"phase": "x"}, {"phase": "begin", "driver_id": "simulation"}]
    for number, payload in enumerate(nutzlasten):
        antwort = _run(register, kind, payload, f"c{number}")
        if antwort is not None and antwort["command_id"] == f"c{number}" and antwort["ok"] is False:
            assert set(antwort["error"]) <= set(wire.ERROR_FIELDS), antwort
            assert fehler.gueltig(antwort["error"].get("details")), antwort


def test_fehlende_nutzlast_nennt_feld_und_bereich(register):
    assert _run(register, "zigbee_permit_join", {"seconds": 999})["error"] == {
        "grund": "ungueltige_nutzlast", "details": {"feld": "seconds", "regel": "bereich", "min": 1, "max": 254}}


def test_unbekannte_phase_und_fehlender_treiber_nennen_feld_und_regel(register):
    assert _run(register, "driver_login", {"phase": "x", "driver_id": "simulation"})["error"] == {
        "grund": "ungueltige_nutzlast", "details": {"feld": "phase", "regel": "unbekannt"}}
    fehlt = {"grund": "ungueltige_nutzlast", "details": {"feld": "treiber", "regel": "fehlt"}}
    ungueltig = {"grund": "ungueltige_nutzlast", "details": {"feld": "treiber", "regel": "ungueltig"}}
    assert _run(register, "inventory", {"stunden": 0}, "c2")["error"] == fehlt
    assert _run(register, "inventory", {"stunden": 0, "filter": {}}, "c3")["error"] == fehlt
    assert _run(register, "inventory", {"stunden": 0, "filter": "simulation"}, "c4")["error"] == ungueltig
    assert _run(register, "inventory", {"stunden": 0, "filter": {"treiber": 5}}, "c5")["error"] == ungueltig
    assert _run(register, "inventory", {"stunden": 0, "filter": {"treiber": "simulation", "x": 1}}, "c6")["error"] == {
        "grund": "ungueltige_nutzlast", "details": {"regel": "unbekannt"}}  # fremder Schluessel: strikt, ohne Namen


def test_the_gateway_registers_exactly_its_commands(register):
    assert set(befehle.HANDLERS) == {"zigbee_permit_join", "zigbee_devices", "driver_login",
                                     "inventory", "diagnostics", "sign_off"}
    assert set(befehle.HANDLERS) <= set(wire.COMMANDS) and set(befehle.HANDLERS) <= register.kinds


def test_bad_payloads(register):
    assert _run(register, "zigbee_permit_join", {"seconds": 999})["error"]["grund"] == "ungueltige_nutzlast"
    assert _run(register, "zigbee_permit_join", {"seconds": True}, "c2")["error"]["grund"] == "ungueltige_nutzlast"


@pytest.mark.parametrize(("sekunden", "angenommen"), [
    (0, False), (1, True), (254, True), (255, False), (-1, False), (1.5, False), (60.0, False), (None, False),
    ("60", False)])
def test_permit_join_takes_exactly_1_to_254_whole_seconds(ctx, register, sekunden, angenommen):
    antwort = _run(register, "zigbee_permit_join", {"seconds": sekunden})
    if angenommen:
        assert antwort["ok"] is True and ctx.z2m.permit_join_requests == [sekunden]
    else:
        assert antwort["error"] == {"grund": "ungueltige_nutzlast",
                                    "details": {"feld": "seconds", "regel": "bereich", "min": 1, "max": 254}}
        assert ctx.z2m.permit_join_requests == []  # nichts an Zigbee2MQTT gesendet


NICHT_ENDLICH = [float("nan"), math.inf, -math.inf, 10**400]
NICHT_ENDLICH_IDS = ["nan", "inf", "minus-inf", "gross"]


def _ohne_nan_sendbar(antwort) -> None:
    """So sendet link.publish: allow_nan=False, sonst ValueError."""
    json.dumps(antwort, allow_nan=False)


@pytest.mark.parametrize("wert", NICHT_ENDLICH, ids=NICHT_ENDLICH_IDS)
@pytest.mark.parametrize("feld", ["temperature", "local_temperature", "occupied_heating_setpoint", "humidity"])
def test_the_device_list_leaves_out_a_value_that_is_not_finite(ctx, register, feld, wert):
    """Ein Zigbee-Wert, der nicht endlich ist (json.loads liest NaN, Infinity), bleibt aus `werte` weg wie ein Feld, das
    das Geraet nicht meldet; die uebrigen Werte und die Batterie bleiben, die Antwort ist sendbar."""
    werte = {"temperature": 20.5, "humidity": 45.0}
    ctx.z2m.report(SENSOR, battery=80, **{**werte, feld: wert})
    antwort = _run(register, "zigbee_devices", {})
    assert antwort["ok"] is True
    _ohne_nan_sendbar(antwort)
    sensor = next(d for d in antwort["result"]["devices"] if d["ieee"] == SENSOR)
    erwartet = {key: value for key, value in werte.items() if key != feld}
    assert sensor["werte"] == erwartet and sensor["batterie"] == 80


@pytest.mark.parametrize("wert", NICHT_ENDLICH, ids=NICHT_ENDLICH_IDS)
def test_the_device_list_reports_no_battery_when_the_level_is_not_finite(ctx, register, wert):
    ctx.z2m.report(SENSOR, temperature=20.5, battery=wert)
    antwort = _run(register, "zigbee_devices", {})
    _ohne_nan_sendbar(antwort)
    sensor = next(d for d in antwort["result"]["devices"] if d["ieee"] == SENSOR)
    assert sensor["batterie"] is None and sensor["werte"] == {"temperature": 20.5}


def test_the_device_list_leaves_out_a_nested_value_with_a_non_finite_number_and_keeps_the_flag(ctx, register):
    ctx.z2m.report(SENSOR, temperature=[float("nan")], humidity={"x": math.inf}, battery_low=True)
    antwort = _run(register, "zigbee_devices", {})
    _ohne_nan_sendbar(antwort)
    sensor = next(d for d in antwort["result"]["devices"] if d["ieee"] == SENSOR)
    assert sensor["werte"] == {} and sensor["batterie"] is True  # Gegenprobe: ein Wahrheitswert bleibt


def _verschachtelt(ebenen: int) -> list:
    wert: list = []
    for _ in range(ebenen - 1):
        wert = [wert]
    return wert


@pytest.mark.parametrize("feld", ["temperature", "battery"])
@pytest.mark.parametrize(("ebenen", "bleibt"), [(32, True), (33, False), (5000, False)])
def test_a_value_nested_deeper_than_32_levels_is_left_out_of_the_device_list(ctx, register, feld, ebenen, bleibt):
    """Zigbee2MQTT liefert flache Werte: bis 32 Ebenen aus Listen bleiben, ab 33 fallen sie weg (`werte` ohne das Feld,
    `batterie` None), die Antwort ist in jedem Fall sendbar. Der Wert geht den oeffentlichen Weg: Bus-Nachricht, `decode`,
    Spiegel. 5000 statt 100000: json.loads liest unter pytest bis rund 9980 Ebenen (im blanken Interpreter bis 9998), 5000 liest
    es sicher; schon dort wirft json.dumps nicht, die Regel muss also nicht erst am Rekursionslimit greifen."""
    wert = _verschachtelt(ebenen)
    ctx.z2m.report(SENSOR, **{feld: wert})
    antwort = _run(register, "zigbee_devices", {})
    assert antwort["ok"] is True
    _ohne_nan_sendbar(antwort)
    sensor = next(d for d in antwort["result"]["devices"] if d["ieee"] == SENSOR)
    if feld == "temperature":
        assert sensor["werte"] == ({feld: wert} if bleibt else {}) and sensor["batterie"] is None
    else:
        assert sensor["batterie"] == (wert if bleibt else None) and sensor["werte"] == {}


@pytest.mark.parametrize("wert", [{"a": {"b": [1, {"c": 2}]}}, [[1.5, [2]], "x"], {}, []], ids=["gemischt", "liste", "leeres-objekt", "leere-liste"])
def test_flat_and_mixed_nested_values_stay_in_the_device_list(ctx, register, wert):
    ctx.z2m.report(SENSOR, temperature=wert)
    sensor = next(d for d in _run(register, "zigbee_devices", {})["result"]["devices"] if d["ieee"] == SENSOR)
    assert sensor["werte"] == {"temperature": wert}


def test_permit_join_and_device_list(ctx, register):
    assert _run(register, "zigbee_permit_join", {"seconds": 120})["ok"] is True
    assert ctx.z2m.permit_join_requests == [120]
    devices = _run(register, "zigbee_devices", {}, "c2")["result"]["devices"]
    sensor = next(d for d in devices if d["ieee"] == SENSOR)
    assert sensor["art"] == "fuehler" and sensor["werte"]["temperature"] == 20.5 and sensor["batterie"] == 80
    assert set(sensor) == set(wire.ZIGBEE_DEVICE_FIELDS)
    assert sensor["last_seen"] == "2023-11-14T22:13:20+00:00"


def test_zigbee_geraete_mit_vertragsbruch_enden_mit_intern_ohne_werte_im_log(monkeypatch, register, caplog):
    monkeypatch.setattr(wire, "ZIGBEE_DEVICE_FIELDS", wire.ZIGBEE_DEVICE_FIELDS[:-1])
    antwort = _run(register, "zigbee_devices", {})
    assert antwort["error"] == {"grund": "intern"}
    assert "Vertragsfehler" in caplog.text and SENSOR not in caplog.text


def test_zigbee_art_ausserhalb_des_vertrags_endet_mit_intern(monkeypatch, register, caplog):
    monkeypatch.setattr(wire, "ZIGBEE_ARTEN", ("thermostat", "sonstig"))  # "fuehler" fehlt: der Sensor bricht ihn
    antwort = _run(register, "zigbee_devices", {})
    assert antwort["error"] == {"grund": "intern"}
    assert "Vertragsfehler" in caplog.text and SENSOR not in caplog.text


def test_zigbee_not_ready(ctx, register):
    ctx.z2m.bridge(online=False)
    assert _run(register, "zigbee_devices", {})["error"]["grund"] == "zigbee_nicht_bereit"
    assert _run(register, "zigbee_permit_join", {"seconds": 60}, "c2")["error"]["grund"] == "zigbee_nicht_bereit"
    assert ctx.z2m.permit_join_requests == []


def test_erkennung_traegt_die_sicherheitswarnungen_je_anlage(ctx):
    daten = befehle.inventory(ctx, {"stunden": 0, "filter": {"treiber": "simulation"}}).inventur
    assert all("sicherheitswarnungen" in anlage for anlage in daten["anlagen"])


def _login_nutzlast(phase="begin", **aenderung) -> dict:
    """Nutzlast des Befehls driver_login mit genau den Feldern der Phase (wire.DRIVER_LOGIN_PHASES)."""
    werte = {"driver_id": "simulation", "client_id": "c", "redirect_uri": "https://portal.test/cb", "scope": "s",
             "code": "x", "ciphertext": "y"}
    return {"phase": phase, **{feld: werte[feld] for feld in wire.DRIVER_LOGIN_PHASES[phase] if feld != "phase"},
            **aenderung}


def test_unknown_driver_and_login_not_needed(register):
    assert _run(register, "driver_login", _login_nutzlast(driver_id="erfunden"))["error"]["grund"] == \
        "treiber_unbekannt"
    login = _run(register, "driver_login", _login_nutzlast(), "c2")
    assert login["error"]["grund"] == "login_nicht_noetig"
    for number, phase in enumerate(wire.DRIVER_LOGIN_PHASES, start=3):  # jede Phase mit ihren Feldern wird angenommen
        antwort = _run(register, "driver_login", _login_nutzlast(phase), f"c{number}")
        assert antwort["error"]["grund"] == "login_nicht_noetig", phase


@pytest.mark.parametrize("phase", sorted(wire.DRIVER_LOGIN_PHASES))
def test_driver_login_verlangt_genau_die_felder_der_phase(register, phase):
    gut = _login_nutzlast(phase)
    for number, feld in enumerate(f for f in gut if f != "phase"):
        fehlt = {k: v for k, v in gut.items() if k != feld}
        assert _run(register, "driver_login", fehlt, f"f{number}")["error"] == {
            "grund": "ungueltige_nutzlast", "details": {"feld": feld, "regel": "fehlt"}}, feld
    fremd = next(f for f in wire.COMMANDS["driver_login"].payload if f not in wire.DRIVER_LOGIN_PHASES[phase])
    assert _run(register, "driver_login", {**gut, fremd: "x"}, "fremd")["error"] == {
        "grund": "ungueltige_nutzlast", "details": {"feld": fremd, "regel": "unbekannt"}}
    assert _run(register, "driver_login", {**gut, "zukunft": 1}, "zukunft")["error"] == {
        "grund": "ungueltige_nutzlast", "details": {"regel": "unbekannt"}}  # kein Name des Vertrags: nie in details


@pytest.mark.parametrize("phase, erwartet", [(None, "fehlt"), (5, "ungueltig"), ([], "ungueltig"), ("x", "unbekannt")])
def test_driver_login_ohne_gueltige_phase(register, phase, erwartet):
    nutzlast = {"driver_id": "simulation"} if phase is None else {"phase": phase, "driver_id": "simulation"}
    assert _run(register, "driver_login", nutzlast)["error"] == {
        "grund": "ungueltige_nutzlast", "details": {"feld": "phase", "regel": erwartet}}


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


def test_inventur_stunden_0_ist_die_erkennung_des_treibers(ctx):
    outcome = befehle.inventory(ctx, {"stunden": 0, "filter": {"treiber": "simulation"}})
    assert isinstance(outcome, Done) and outcome.result == {}  # kein Warten auf eine Messreihe
    daten = outcome.inventur
    assert tuple(daten) == wire.INVENTUR_GATEWAY_FIELDS and daten["treiber"] == "simulation"
    assert all(tuple(a) == wire.INVENTUR_ANLAGE_FIELDS for a in daten["anlagen"])
    assert all(f["anlage"] == 0 for f in daten["features"]) and len(daten["features"]) == 2
    assert not ctx.paths.inventur.exists()  # keine Reihe angelegt


@pytest.mark.parametrize("konf", [None, {"anlage": None}, "eingerichtet"])
def test_inventur_stunden_0_geht_vor_und_nach_der_einrichtung(ctx, konf):
    if konf != "eingerichtet":
        ctx.umgebung.konf = konf
    outcome = befehle.inventory(ctx, {"stunden": 0, "filter": {"treiber": "simulation"}})
    assert isinstance(outcome, Done) and outcome.inventur["treiber"] == "simulation"


@pytest.mark.parametrize("payload", [
    {"stunden": 0}, {"stunden": 0, "filter": None}, {"stunden": 0, "filter": {}}, {"stunden": 0, "filter": []},
    {"stunden": 0, "filter": {"treiber": 5}}, {"stunden": 0, "filter": {"treiber": None}},
    {"stunden": 0, "filter": {"treiber": "simulation", "plattformen": ["x"]}},
    {"stunden": 0, "filter": {"domaenen": ["x"]}}, {"stunden": -1}, {"stunden": 49}, {"stunden": False},
])
def test_inventur_stunden_0_ohne_treiber_ist_ungueltig(register, payload):
    assert _run(register, "inventory", payload)["error"]["grund"] == "ungueltige_nutzlast"


@pytest.mark.parametrize("filter_, details", [
    ({"treiber": "simulation", "plattformen": ["x"]}, {"feld": "plattformen", "regel": "unbekannt"}),
    ({"treiber": "simulation", "geheim": 1}, {"regel": "unbekannt"}),
])
def test_inventur_stunden_0_mit_fremdem_filterschluessel_nennt_nur_vertragsnamen(register, filter_, details):
    """V1 ist beidseitig strikt (Nutzer 2026-10-16): G-6-Format, ein fremder Name steht nur in details, wenn er im
    Vertrag steht."""
    antwort = _run(register, "inventory", {"stunden": 0, "filter": filter_})
    assert antwort["error"] == {"grund": "ungueltige_nutzlast", "details": details}


def test_inventur_stunden_0_mit_vertragsbruch_der_erkennung_endet_mit_intern(monkeypatch, register, caplog):
    """Eine Erkennung, die nicht zu den Feldlisten des Vertrags passt (Programmierfehler oder fremde Daten), endet mit
    intern; im Log stehen Fehlerart und Ort, nie ein Wert."""
    monkeypatch.setattr(wire, "INVENTUR_ANLAGE_FIELDS", wire.INVENTUR_ANLAGE_FIELDS[:-1])
    antwort = _run(register, "inventory", {"stunden": 0, "filter": {"treiber": "simulation"}})
    assert antwort["error"] == {"grund": "intern"}
    assert "Vertragsfehler" in caplog.text and "lever_set" not in caplog.text and "settle_delay" not in caplog.text


def test_inventur_stunden_0_unbekannter_treiber(register):
    assert _run(register, "inventory", {"stunden": 0, "filter": {"treiber": "erfunden"}})["error"]["grund"] == \
        "treiber_unbekannt"


def test_inventur_stunden_0_mit_treiberfehlern_nutzt_die_vertragsgruende(monkeypatch, register):
    def abgelaufen(self):
        raise DriverError("nicht_angemeldet")

    monkeypatch.setattr("smartheat_gateway.drivers.simulation.SimulationDriver.erkennung", abgelaufen)
    antwort = _run(register, "inventory", {"stunden": 0, "filter": {"treiber": "simulation"}})
    assert antwort["error"] == {"grund": "nicht_angemeldet"}

    def quota(self):
        raise QuotaExhausted("x")

    monkeypatch.setattr("smartheat_gateway.drivers.simulation.SimulationDriver.erkennung", quota)
    antwort = _run(register, "inventory", {"stunden": 0, "filter": {"treiber": "simulation"}}, "c2")
    assert antwort["error"]["grund"] == "kontingent_erschoepft"


def test_inventur_stunden_1_und_mehr_bleibt_die_messreihe(ctx):
    assert isinstance(befehle.inventory(ctx, {"stunden": 1, "filter": {"treiber": "simulation"}}), Waiting)


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
    dateien.schreiben_json(ctx.paths.backup, {"restore_point": {"curve": 1.1, "room_setpoint": 21.0}}, modus=dateien.LESBAR)
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
    assert befehle.sign_off(ctx, {}) == Failed("nicht_eingerichtet")
    assert ctx.umgebung.restarts == []


def test_every_reason_the_handlers_produce_is_listed():
    produced = {
        "zigbee_permit_join": {"ungueltige_nutzlast", "zigbee_nicht_bereit"},
        "zigbee_devices": {"zigbee_nicht_bereit"},
        "driver_login": {"ungueltige_nutzlast", "treiber_unbekannt", "login_nicht_noetig", "login_fehlgeschlagen",
                         "anlage_nicht_erreichbar"},
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
    dateien.schreiben_json(ctx.paths.inventur,
               {"driver_id": "simulation", "started": 0, "hours": 1, "samples": [{}], "next_at": 0}, modus=dateien.LESBAR)
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
    dateien.schreiben_json(ctx.paths.inventur, {
        "driver_id": "vicare_cloud", "started": ctx.wall() + 90_000, "hours": 1, "samples": [{"ts": 1.0}],
        "next_at": ctx.wall() + 90_000,
    }, modus=dateien.LESBAR)
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


class _HaengtImHintergrund:
    """Hintergrund, dessen Aufruf nie fertig wird (ein haengender Cloud-Aufruf); die Uhr der Testwelt laeuft weiter."""

    def submit(self, call):
        from concurrent.futures import Future

        return Future()


def _zeitablauf(ctx, clock, outcome, sekunden: float) -> None:
    """Vor der Frist nichts, genau an der Frist der Zeitablauf-Fehler (Treiber haengt, Uhr der Testwelt)."""
    assert isinstance(outcome, Waiting) and outcome.deadline == clock() + sekunden
    assert outcome.check() is None
    clock.advance(sekunden - 1)
    assert outcome.check() is None  # eine Sekunde vor der Frist
    clock.advance(1)
    result = outcome.check()
    assert isinstance(result, Failed) and result.grund == "anlage_nicht_erreichbar"
    assert result == Failed("anlage_nicht_erreichbar") and result.grund in wire.command_errors("inventory")


def test_eine_haengende_erkennung_endet_nach_120_s_mit_dem_zeitablauf(ctx, clock):
    # Abschlussreview S5b I3: die Server-Frist fuer stunden 0 ist 3600 s, diese Grenze haelt den Cloud-Aufruf kurz.
    assert befehle.ERKENNUNG_SECONDS == 120
    ctx.hintergrund = _HaengtImHintergrund()
    outcome = befehle.inventory(ctx, {"stunden": 0, "filter": {"treiber": "simulation"}})
    _zeitablauf(ctx, clock, outcome, 120)


def test_eine_haengende_anmeldung_endet_an_der_frist_des_befehls(ctx, clock):
    ctx.hintergrund = _HaengtImHintergrund()
    outcome = befehle.driver_login(ctx, _login_nutzlast(driver_id="vicare_cloud"))
    frist = wire.COMMANDS["driver_login"].expires_seconds
    assert outcome.deadline == clock() + frist
    clock.advance(frist - 1)
    assert outcome.check() is None
    clock.advance(1)
    assert outcome.check().grund == "anlage_nicht_erreichbar"


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
        assert secret not in _eigene_logs(caplog) and secret not in json.dumps(result.details or {})


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


def test_die_erkennung_geht_als_up_inventory_vor_dem_ergebnis_und_wird_gestueckelt(regelnde_welt, monkeypatch):
    world = regelnde_welt
    eintraege = [{"anlage": 0, "feature": f"heating.circuits.{n}.sensors.temperature.supply",
                  "properties": {"value": {"type": "number", "value": 35.5, "unit": "celsius", "notiz": "x" * 400}},
                  "commands": {}} for n in range(1000)]  # rund 500 KB: mehr als eine Nachricht
    daten = {"treiber": "simulation", "anlagen": [{"kennung": {}, "sicherheitswarnungen": {}}], "features": eintraege}
    monkeypatch.setattr("smartheat_gateway.drivers.simulation.SimulationDriver.erkennung", lambda self: daten)
    _befehl(world, "c-erk", "inventory", {"stunden": 0, "filter": {"treiber": "simulation"}})
    ergebnis = _bis(world, lambda: _ergebnisse(world, command_id="c-erk"))[-1]
    assert ergebnis["ok"] is True
    teile = [body for link in world.links for body in link.of("up/inventory") if body.get("command_id") == "c-erk"]
    assert len(teile) > 1 and [t["teil"] for t in teile] == list(range(1, len(teile) + 1))
    assert all(t["teile"] == len(teile) for t in teile)
    assert [f for t in teile for f in t["daten"].get("features", [])] == eintraege
    assert teile[0]["daten"]["treiber"] == "simulation" and teile[0]["daten"]["anlagen"] == daten["anlagen"]


def test_a_blocking_erkennung_does_not_stall_the_device_thread(regelnde_welt, monkeypatch):
    world = regelnde_welt
    original = SimulationDriver.erkennung
    gestartet, frei = _blockieren(monkeypatch, "smartheat_gateway.drivers.simulation.SimulationDriver.erkennung", original)
    _geraete_thread_arbeitet_weiter(
        world, gestartet,
        lambda: _befehl(world, "c-erk", "inventory", {"stunden": 0, "filter": {"treiber": "simulation"}}))
    assert _ergebnisse(world, command_id="c-erk") == []  # Ergebnis erst, wenn der Aufruf fertig ist
    frei.set()
    ergebnis = _bis(world, lambda: _ergebnisse(world, command_id="c-erk"))[-1]
    assert ergebnis["ok"] is True
    teile = [body for link in world.links for body in link.of("up/inventory") if body.get("command_id") == "c-erk"]
    assert teile and all("sicherheitswarnungen" in a for t in teile for a in t["daten"].get("anlagen", []))


def test_a_blocking_login_does_not_stall_the_device_thread_and_keeps_its_contract_reason(regelnde_welt, monkeypatch):
    world = regelnde_welt

    def abgelehnt(self, params):
        raise DriverError("login_fehlgeschlagen")

    gestartet, frei = _blockieren(
        monkeypatch, "smartheat_gateway.drivers.vicare_cloud.driver.ViCareCloudDriver.login_finish", abgelehnt)
    payload = {"phase": "finish", "driver_id": "vicare_cloud", "code": "abc", "redirect_uri": "https://portal.test/cb"}
    _geraete_thread_arbeitet_weiter(world, gestartet, lambda: _befehl(world, "c-login", "driver_login", payload))
    assert _ergebnisse(world, command_id="c-login") == []
    frei.set()
    ergebnis = _bis(world, lambda: _ergebnisse(world, command_id="c-login"))[-1]
    assert ergebnis["ok"] is False
    assert ergebnis["error"] == {"grund": "login_fehlgeschlagen"}


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


@pytest.mark.parametrize("wert", [float("nan"), math.inf, -math.inf], ids=["nan", "inf", "minus-inf"])
def test_a_sensor_with_a_non_finite_value_still_gets_its_device_list_to_the_server(regelnde_welt, wert):
    """Ende zu Ende ueber Device._publish: der Link sendet wie das echte (allow_nan=False). Ohne die Filterung wirft der
    Link, das Ergebnis geht nie hoch und die Einrichtung sieht keine Geraeteliste."""
    world = regelnde_welt
    world.z2m.report(SENSOR, temperature=wert, local_temperature=wert, humidity=wert, battery=wert)
    _befehl(world, "c-geraete", "zigbee_devices", {})
    [ergebnis] = _ergebnisse(world, command_id="c-geraete")
    assert ergebnis["ok"] is True
    sensor = next(d for d in ergebnis["result"]["devices"] if d["ieee"] == SENSOR)
    assert sensor["werte"] == {} and sensor["batterie"] is None
