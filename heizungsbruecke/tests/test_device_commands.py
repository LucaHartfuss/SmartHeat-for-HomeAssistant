"""Befehle hoechstens einmal ausgefuehrt, Ergebnis vor dem Senden gespeichert, Gruende aus dem Vertrag;
Befehl hinweis (jedes Geraet)."""
import json
import logging

import pytest
from device_helpers import Sent, _command

from smartheat_device import commands as cmd
from smartheat_device import fehler, nachrichten, wire


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _ok(command_id="c1", result=None):
    return {"schema": 1, "command_id": command_id, "ok": True, "result": result or {}, "error": None}


def _fail(grund, details=None, command_id="c1"):
    return {"schema": 1, "command_id": command_id, "ok": False, "result": None, "error": fehler.body(grund, details)}


def test_a_command_runs_once_and_its_result_goes_up(tmp_path):
    sent, calls = Sent(), []
    register = cmd.CommandRegister(tmp_path, sent)
    register.register("diagnostics", lambda payload: calls.append(payload) or cmd.Done({"a": 1}))
    register.empfangen(_command())
    register.empfangen(_command())
    assert calls == [{}] and sent.results == [_ok(result={"a": 1})] * 2


def test_a_duplicate_only_repeats_the_stored_result_also_after_a_restart(tmp_path):
    sent, calls = Sent(), []
    first = cmd.CommandRegister(tmp_path, sent)
    first.register("new_claim_code", lambda payload: calls.append(1) or cmd.Done({}))
    first.empfangen(_command("new_claim_code"))
    restarted = cmd.CommandRegister(tmp_path, sent)
    restarted.register("new_claim_code", lambda payload: calls.append(2) or cmd.Done({}))
    restarted.empfangen(_command("new_claim_code"))
    assert calls == [1] and sent.results == [_ok()] * 2


def test_an_unsent_result_goes_up_with_the_next_delivery(tmp_path):
    sent = Sent(ok=False)
    register = cmd.CommandRegister(tmp_path, sent)
    register.register("diagnostics", lambda payload: cmd.Done({}))
    register.empfangen(_command())
    sent.ok = True
    register.empfangen(_command())  # der Server stellt nach hello erneut zu
    assert sent.results == [_ok(), _ok()]


def test_unknown_kinds_and_broken_payloads(tmp_path):
    sent = Sent()
    register = cmd.CommandRegister(tmp_path, sent)
    register.register("diagnostics", lambda payload: cmd.Done({}))
    register.empfangen(_command("zigbee_devices"))  # nicht eingetragen
    register.empfangen({**_command(command_id="c2"), "payload": [1]})
    assert sent.results == [_fail("ungueltige_nutzlast", {"feld": "kind", "regel": "unbekannt"}),
                            _fail("ungueltige_nutzlast", {"feld": "payload", "regel": "ungueltig"}, "c2")]


def test_the_result_has_exactly_the_contract_fields():
    for payload in (cmd.result_payload("c", True, {}, None),
                    cmd.result_payload("c", False, None, fehler.body("intern", None))):
        assert set(payload) == set(wire.RESULT_FIELDS) | {"command_id"}
    assert cmd.result_payload("c", False, {"x": 1}, fehler.body("intern", None))["result"] is None
    assert cmd.result_payload("c", True, {"x": 1}, fehler.body("intern", None))["error"] is None


def test_a_result_that_leaves_the_contract_is_a_contract_error(monkeypatch):
    monkeypatch.setattr(wire, "RESULT_FIELDS", (*wire.RESULT_FIELDS, "neu"))
    with pytest.raises(nachrichten.Vertragsfehler) as fehlt:
        cmd.result_payload("c", True, {}, None)
    assert "neu" in str(fehlt.value)


def test_the_register_reasons_are_in_the_common_list_of_every_kind():
    for kind in wire.COMMANDS:
        assert {"ungueltige_nutzlast", "intern"} <= set(wire.command_errors(kind))


@pytest.mark.parametrize("feld", [f for f in wire.COMMAND_FIELDS if f != "command_id"])
def test_a_command_missing_a_contract_field_is_refused_and_does_not_run(tmp_path, feld):
    sent, calls = Sent(), []
    register = cmd.CommandRegister(tmp_path, sent)
    register.register("diagnostics", lambda payload: calls.append(1) or cmd.Done({}))
    register.empfangen({key: value for key, value in _command().items() if key != feld})
    assert calls == [] and sent.results == [_fail("ungueltige_nutzlast", {"feld": feld, "regel": "fehlt"})]


@pytest.mark.parametrize("feld, erwartet", [
    ("zukunft", {"regel": "unbekannt"}),  # kein Name des Vertrags: nie als Wert in details
    ("stunden", {"feld": "stunden", "regel": "unbekannt"}),  # Name des Vertrags (PRUEF_FELDER)
])
def test_a_foreign_field_in_a_command_is_refused_and_does_not_run(tmp_path, feld, erwartet):
    sent, calls = Sent(), []
    register = cmd.CommandRegister(tmp_path, sent)
    register.register("diagnostics", lambda payload: calls.append(1) or cmd.Done({}))
    register.empfangen({**_command(), feld: 1})
    assert calls == [] and sent.results == [_fail("ungueltige_nutzlast", erwartet)]


@pytest.mark.parametrize("kind, payload, erwartet", [
    ("diagnostics", {"zukunft": 1}, {"regel": "unbekannt"}),
    ("diagnostics", {"stunden": 1}, {"feld": "stunden", "regel": "unbekannt"}),
    ("inventory", {"stunden": 1, "extra": 2}, {"regel": "unbekannt"}),
    ("hinweis", {"key": "k", "stufe": "info", "text": "t", "zukunft": 1}, {"regel": "unbekannt"}),
])
def test_a_foreign_payload_field_is_refused_and_does_not_run(tmp_path, kind, payload, erwartet):
    sent, calls = Sent(), []
    register = cmd.CommandRegister(tmp_path, sent)
    register.register(kind, lambda nutzlast: calls.append(1) or cmd.Done({}))
    register.empfangen(_command(kind, payload=payload))
    assert calls == [] and sent.results == [_fail("ungueltige_nutzlast", erwartet)]


@pytest.mark.parametrize("kind, payload, feld", [
    ("claim_state", {}, "device_state"),
    ("zigbee_permit_join", {}, "seconds"),
    ("inventory", {"filter": {"treiber": "simulation"}}, "stunden"),
    ("hinweis", {"key": "k", "stufe": "info"}, "text"),
    ("hinweis", {"stufe": "info", "text": "t"}, "key"),
])
def test_a_missing_required_payload_field_is_refused_and_does_not_run(tmp_path, kind, payload, feld):
    sent, calls = Sent(), []
    register = cmd.CommandRegister(tmp_path, sent)
    register.register(kind, lambda nutzlast: calls.append(1) or cmd.Done({}))
    register.empfangen(_command(kind, payload=payload))
    assert calls == [] and sent.results == [_fail("ungueltige_nutzlast", {"feld": feld, "regel": "fehlt"})]


@pytest.mark.parametrize("kind, payload", [
    ("claim_state", {"device_state": "uebernommen"}), ("new_claim_code", {}), ("sign_off", {}), ("diagnostics", {}),
    ("hinweis", {"key": "k", "stufe": "info", "text": "t"}), ("inventory", {"stunden": 0}),
    ("inventory", {"stunden": 1, "filter": {"treiber": "simulation"}}), ("zigbee_permit_join", {"seconds": 60}),
    ("zigbee_devices", {}),
])
def test_every_server_payload_shape_is_accepted(tmp_path, kind, payload):
    """Die Nutzlasten der Server-Erzeuger (Contract-Check, Vertrag V1) bleiben angenommen: alle Pflichtfelder, optionale
    nach Bedarf."""
    sent, calls = Sent(), []
    register = cmd.CommandRegister(tmp_path, sent)
    register.register(kind, lambda nutzlast: calls.append(1) or cmd.Done({}))
    register.empfangen(_command(kind, payload=payload))
    assert calls == [1] and sent.results == [_ok()]


@pytest.mark.parametrize("payload", [{}, {"phase": "begin"}, {"phase": "x", "driver_id": "y"}])
def test_a_phase_command_leaves_the_required_fields_to_its_handler(tmp_path, payload):
    """driver_login: Command.payload ist die Vereinigung aller Phasen; die Annahme verlangt davon keines."""
    sent, calls = Sent(), []
    register = cmd.CommandRegister(tmp_path, sent)
    register.register("driver_login", lambda nutzlast: calls.append(nutzlast) or cmd.Done({}))
    register.empfangen(_command("driver_login", payload=payload))
    assert calls == [payload] and sent.results == [_ok()]
    assert set(cmd.PHASENBEFEHLE) == {"driver_login"}


def test_the_optional_payload_field_of_a_kind_is_accepted(tmp_path):
    sent, calls = Sent(), []
    register = cmd.CommandRegister(tmp_path, sent)
    register.register("inventory", lambda nutzlast: calls.append(1) or cmd.Done({}))
    register.empfangen(_command("inventory", payload={"stunden": 0, "filter": {"treiber": "simulation"}}))
    assert calls == [1] and sent.results == [_ok()]


def test_no_foreign_name_reaches_the_details(tmp_path):
    sent = Sent()
    register = cmd.CommandRegister(tmp_path, sent)
    register.register("diagnostics", lambda payload: cmd.Done({}))
    register.empfangen({**_command(), "GEHEIMER-NAME": 1})
    register.empfangen(_command(command_id="c2", payload={"GEHEIMER-NAME": 1}))
    assert "GEHEIMER-NAME" not in json.dumps(sent.results) and len(sent.results) == 2


def test_a_malformed_duplicate_does_not_replace_the_stored_result(tmp_path):
    sent = Sent()
    register = cmd.CommandRegister(tmp_path, sent)
    register.register("diagnostics", lambda payload: cmd.Done({"a": 1}))
    register.empfangen(_command())
    register.empfangen({key: value for key, value in _command().items() if key != "expires_at"})
    assert sent.results == [_ok(result={"a": 1})] * 2


@pytest.mark.parametrize("command_id", [None, "", "x" * 65, 5])
def test_commands_without_a_valid_id_are_dropped(tmp_path, command_id):
    sent = Sent()
    cmd.CommandRegister(tmp_path, sent).empfangen({**_command(), "command_id": command_id})
    assert sent.results == []


def test_only_contract_kinds_can_be_registered(tmp_path):
    with pytest.raises(ValueError):
        cmd.CommandRegister(tmp_path, Sent()).register("apply_config", lambda payload: cmd.Done({}))


def test_a_reason_outside_the_list_becomes_intern(tmp_path):
    sent = Sent()
    register = cmd.CommandRegister(tmp_path, sent)
    register.register("diagnostics", lambda payload: cmd.Failed("erfunden"))
    register.empfangen(_command())
    assert sent.results == [_fail("intern")]


def test_exceptions_become_reasons(tmp_path):
    class DriverDown(Exception):
        pass

    def uebersetzen(error):
        return cmd.Failed("anlage_nicht_erreichbar") if isinstance(error, DriverDown) else None

    def invalid(payload):
        raise cmd.InvalidPayload("seconds", "fehlt")

    def down(payload):
        raise DriverDown()

    def broken(payload):
        raise RuntimeError("kaputt")

    sent = Sent()
    register = cmd.CommandRegister(tmp_path, sent, uebersetzen=uebersetzen)
    register.register("zigbee_permit_join", invalid)
    register.register("inventory", down)
    register.register("diagnostics", broken)
    nutzlasten = {"zigbee_permit_join": {"seconds": 60}, "inventory": {"stunden": 1}, "diagnostics": {}}
    for number, (kind, nutzlast) in enumerate(nutzlasten.items(), start=1):
        register.empfangen(_command(kind, f"c{number}", nutzlast))
    assert [result["error"] for result in sent.results] == [
        {"grund": "ungueltige_nutzlast", "details": {"feld": "seconds", "regel": "fehlt"}},
        {"grund": "anlage_nicht_erreichbar"},
        {"grund": "intern"},
    ]


def test_kein_ergebnis_traegt_einen_text(tmp_path):
    """Jeder Fehlerweg des Registers meldet nur Felder aus ERROR_FIELDS."""
    def zu_gross(command_id, daten):
        raise ValueError("zu gross")

    def ungueltig(payload):
        raise cmd.InvalidPayload("seconds", "fehlt")

    def kaputt(payload):
        raise RuntimeError("kaputt")

    clock, sent = Clock(), Sent()
    register = cmd.CommandRegister(tmp_path, sent, clock=clock, send_inventory=zu_gross, uebersetzen=lambda error: None)
    register.register("diagnostics", lambda payload: cmd.Failed("erfunden"))
    register.register("zigbee_permit_join", ungueltig)
    register.register("zigbee_devices", kaputt)
    register.register("inventory", lambda payload: cmd.Done({}, inventur={"x": 1}))
    register.register("sign_off", lambda payload: cmd.Waiting(lambda: None, clock.now + 1))
    register.register("hinweis", cmd.Hinweise(tmp_path).handler)
    for number, (kind, payload) in enumerate([("diagnostics", {}), ("zigbee_permit_join", {}), ("zigbee_devices", {}),
                                              ("inventory", {"stunden": 0}), ("sign_off", {}), ("hinweis", {}),
                                              ("erfunden", {}), ("diagnostics", [])]):
        register.empfangen(_command(kind, f"c{number}", payload))
    clock.now += 2
    register.pruefen()
    fehler_ = [result["error"] for result in sent.results if not result["ok"]]
    assert len(fehler_) == 8 and all(set(e) <= set(wire.ERROR_FIELDS) and fehler.gueltig(e.get("details"))
                                     for e in fehler_)


def test_ein_gespeichertes_ergebnis_von_vor_5c_geht_ohne_text_hoch(tmp_path):
    (tmp_path / cmd.STORE_FILE).write_text(json.dumps(
        [["c1", False, None, {"grund": "intern", "text": "Interner Fehler im Gerät."}]]))
    sent = Sent()
    cmd.CommandRegister(tmp_path, sent).empfangen(_command())
    assert sent.results == [_fail("intern")]


def test_a_waiting_command_finishes_later_and_runs_once(tmp_path):
    sent, clock, state, calls = Sent(), Clock(), {"done": False}, []

    def sign_off(payload):
        calls.append(1)
        return cmd.Waiting(lambda: cmd.Done({"zurueckgesetzt": True, "werte": {}}) if state["done"] else None,
                           clock.now + 60)

    register = cmd.CommandRegister(tmp_path, sent, clock=clock)
    register.register("sign_off", sign_off)
    register.empfangen(_command("sign_off"))
    register.empfangen(_command("sign_off"))  # waehrend des Wartens erneut zugestellt
    register.pruefen()
    assert sent.results == [] and calls == [1]
    state["done"] = True
    register.pruefen()
    assert sent.results == [_ok(result={"zurueckgesetzt": True, "werte": {}})]


def test_a_waiting_command_times_out_with_its_contract_reason(tmp_path):
    sent, clock = Sent(), Clock()
    register = cmd.CommandRegister(tmp_path, sent, clock=clock)
    register.register("sign_off", lambda payload: cmd.Waiting(lambda: None, clock.now + 60))
    register.register("diagnostics", lambda payload: cmd.Waiting(lambda: None, clock.now + 60))
    register.empfangen(_command("sign_off", "c1"))
    register.empfangen(_command("diagnostics", "c2"))
    clock.now += 61
    register.pruefen()
    assert sent.results == [_fail(cmd.TIMEOUT_GRUND, None, "c1"), _fail("intern", None, "c2")]


def test_inventory_data_goes_out_before_the_result(tmp_path):
    order = []
    register = cmd.CommandRegister(
        tmp_path, lambda body: order.append(("result", body["command_id"])) or True,
        send_inventory=lambda command_id, daten: order.append(("inventur", command_id, daten)) or [7])
    register.register("inventory", lambda payload: cmd.Done({}, inventur={"k": [1]}))
    register.empfangen(_command("inventory", payload={"stunden": 1}))
    assert order == [("inventur", "c1", {"k": [1]})]  # das Ergebnis erst nach dem PUBACK
    register.inventur_bestaetigt(7)
    register.pruefen()
    assert order == [("inventur", "c1", {"k": [1]}), ("result", "c1")]


def test_an_inventory_waits_while_offline(tmp_path):
    sent, online, parts = Sent(), {"ok": False}, []
    register = cmd.CommandRegister(
        tmp_path, sent,
        send_inventory=lambda command_id, daten: parts.append(command_id) or ([5] if online["ok"] else None))
    register.register("inventory", lambda payload: cmd.Done({}, inventur={"k": [1]}))
    register.empfangen(_command("inventory", payload={"stunden": 1}))
    assert sent.results == [] and parts == ["c1"]
    online["ok"] = True
    register.pruefen()
    assert parts == ["c1", "c1"] and sent.results == []  # gesendet, aber noch nicht bestaetigt
    register.inventur_bestaetigt(5)
    register.pruefen()
    assert sent.results == [_ok()]


def _inventur_register(tmp_path, sent, mids, *, clock=None, calls=None, wiederholbar=True):
    """Register mit einer Erkennung, die eine Inventur liefert; mids: Liste der Aufrufe von send_inventory
    (je Aufruf die mids der Teile)."""
    versuche = iter(mids)
    register = cmd.CommandRegister(tmp_path, sent, clock=clock or Clock(),
                                   send_inventory=lambda command_id, daten: next(versuche))
    register.register("inventory", lambda payload: (calls.append(1) if calls is not None else None)
                      or cmd.Done({"n": 1}, inventur={"k": [1]}), wiederholbar=wiederholbar)
    return register


def test_ergebnis_der_inventur_erst_nach_puback_der_teile(tmp_path):
    """Ergebnis und Journal erst, wenn jedes Inventurteil bestaetigt ist; bis dahin steht der Marker."""
    sent, calls = Sent(), []
    register = _inventur_register(tmp_path, sent, [[1, 2, 3]], calls=calls)
    register.empfangen(_command("inventory", payload={"stunden": 1}))
    marker = [["c1", None, None, None]]
    assert sent.results == [] and json.loads((tmp_path / cmd.STORE_FILE).read_text()) == marker
    for mid in (1, 3):
        register.inventur_bestaetigt(mid)
        register.pruefen()
        assert sent.results == [] and json.loads((tmp_path / cmd.STORE_FILE).read_text()) == marker
    register.empfangen(_command("inventory", payload={"stunden": 1}))  # Duplikat waehrend des Wartens: nichts
    register.inventur_bestaetigt(99)  # fremde mid: zaehlt nicht
    register.pruefen()
    assert sent.results == [] and calls == [1]
    register.inventur_bestaetigt(2)
    register.pruefen()
    assert sent.results == [_ok(result={"n": 1})]
    assert json.loads((tmp_path / cmd.STORE_FILE).read_text()) == [["c1", True, {"n": 1}, None]]


def test_eine_verlorene_bestaetigung_laesst_die_teile_noch_einmal_hoch_gehen(tmp_path):
    sent, clock, calls = Sent(), Clock(), []
    register = _inventur_register(tmp_path, sent, [[1, 2], [3, 4]], clock=clock, calls=calls)
    register.empfangen(_command("inventory", payload={"stunden": 1}))
    register.inventur_bestaetigt(1)
    clock.now += cmd.INVENTUR_BESTAETIGUNG_SECONDS - 1
    register.pruefen()
    assert sent.results == []  # Frist noch nicht um: kein zweiter Versand (das naechste next() wuerfe)
    clock.now += 1
    register.pruefen()  # Frist um: alle Teile erneut (mids 3, 4), die Erkennung selbst laeuft nicht noch einmal
    assert sent.results == [] and calls == [1]
    register.inventur_bestaetigt(2)  # Rest des ersten Versuchs zaehlt nicht fuer den zweiten
    register.pruefen()
    assert sent.results == []
    register.inventur_bestaetigt(3)
    register.inventur_bestaetigt(4)
    register.pruefen()
    assert sent.results == [_ok(result={"n": 1})] and calls == [1]


def test_ein_wechsel_des_clients_laesst_die_teile_neu_hinaus_und_alte_mids_zaehlen_nicht(tmp_path):
    """Ein neuer paho-Client zaehlt mids wieder ab 1: ein PUBACK mit der Nummer eines alten Teils bestaetigt nichts,
    erst das PUBACK der neu gesendeten Teile."""
    sent, calls = Sent(), []
    register = _inventur_register(tmp_path, sent, [[1, 2], [1, 2]], calls=calls)
    register.empfangen(_command("inventory", payload={"stunden": 1}))
    register.inventur_bestaetigt(1)
    register.inventur_neu_senden()
    register.inventur_bestaetigt(2)  # PUBACK des neuen Clients (z. B. hello), zufaellig die Nummer des alten Teils 2
    register.pruefen()  # die Teile gehen mit neuen mids [1, 2] hinaus
    register.inventur_bestaetigt(1)
    register.pruefen()
    assert sent.results == []
    register.inventur_bestaetigt(2)
    register.pruefen()
    assert sent.results == [_ok(result={"n": 1})] and calls == [1]


def test_ein_wechsel_des_clients_nach_dem_letzten_puback_sendet_nichts_neu(tmp_path):
    """Alle Teile sind bestaetigt, das Ergebnis steht nur noch fuer den naechsten Takt aus: ein Client-Wechsel
    dazwischen sendet nicht erneut und zaehlt keinen Versuch (sonst endete der Befehl nach drei Wechseln mit
    keine_bestaetigung)."""
    sent, calls = Sent(), []
    register = _inventur_register(tmp_path, sent, [[1, 2]], calls=calls)  # ein zweiter Versand wuerfe StopIteration
    register.empfangen(_command("inventory", payload={"stunden": 1}))
    register.inventur_bestaetigt(1)
    register.inventur_bestaetigt(2)
    for _ in range(cmd.INVENTUR_VERSUCHE_MAX + 1):
        register.inventur_neu_senden()
    register.pruefen()
    assert sent.results == [_ok(result={"n": 1})] and calls == [1]


def test_das_neusenden_der_inventur_endet_nach_drei_versuchen_mit_keine_bestaetigung(tmp_path):
    sent, clock, calls = Sent(), Clock(), []
    register = _inventur_register(tmp_path, sent, [[1], [2], [3], [4]], clock=clock, calls=calls)
    register.empfangen(_command("inventory", payload={"stunden": 1}))
    for _ in range(cmd.INVENTUR_VERSUCHE_MAX - 1):  # Versuch 1 und 2 laufen ab, jeweils folgt ein neuer Versand
        clock.now += cmd.INVENTUR_BESTAETIGUNG_SECONDS
        register.pruefen()
        assert sent.results == []
    clock.now += cmd.INVENTUR_BESTAETIGUNG_SECONDS  # Versuch 3 laeuft ab: Ende, kein vierter Versand
    register.pruefen()
    assert sent.results == [_fail(cmd.TIMEOUT_GRUND)] and calls == [1]
    assert json.loads((tmp_path / cmd.STORE_FILE).read_text()) == [["c1", False, None, {"grund": cmd.TIMEOUT_GRUND}]]
    clock.now += cmd.INVENTUR_BESTAETIGUNG_SECONDS
    register.pruefen()
    assert len(sent.results) == 1  # nichts mehr offen


def test_wiederholbare_inventur_laeuft_nach_neustart_erneut_statt_intern(tmp_path):
    """Der Neustart fiel in die Erkennung (Marker, kein Ergebnis); die erneute Zustellung fuehrt sie noch
    einmal aus, das Ergebnis ist ok, die Inventur geht hinaus. Ein weiteres Duplikat laeuft nicht noch einmal."""
    sent, calls = Sent(), []
    erste = _inventur_register(tmp_path, sent, [None], calls=calls)  # offline: die Erkennung ist gelaufen, nichts weg
    erste.empfangen(_command("inventory", payload={"stunden": 0}))
    assert json.loads((tmp_path / cmd.STORE_FILE).read_text()) == [["c1", None, None, None]]
    neustart = _inventur_register(tmp_path, sent, [[1]], calls=calls)
    neustart.empfangen(_command("inventory", payload={"stunden": 0}))  # nach hello erneut zugestellt
    neustart.empfangen(_command("inventory", payload={"stunden": 0}))  # Duplikat waehrend der Wartezeit
    neustart.inventur_bestaetigt(1)
    neustart.pruefen()
    neustart.empfangen(_command("inventory", payload={"stunden": 0}))  # Duplikat nach dem Ergebnis: aus dem Journal
    assert calls == [1, 1]
    assert sent.results == [_ok(result={"n": 1})] * 2


def test_eine_nicht_wiederholbare_inventur_endet_nach_neustart_weiter_mit_intern(tmp_path):
    """Gegenprobe zu (a): ohne wiederholbar=True bleibt es bei "intern" (Marker, Ergebnis unbekannt)."""
    sent, calls = Sent(), []
    (tmp_path / cmd.STORE_FILE).write_text(json.dumps([["c1", None, None, None]]))
    register = _inventur_register(tmp_path, sent, [], calls=calls, wiederholbar=False)
    register.empfangen(_command("inventory", payload={"stunden": 0}))
    assert calls == [] and sent.results == [_fail("intern")]


def test_a_too_large_inventory_fails_intern(tmp_path):
    def too_large(command_id, daten):
        raise ValueError("zu gross")

    sent = Sent()
    register = cmd.CommandRegister(tmp_path, sent, send_inventory=too_large)
    register.register("inventory", lambda payload: cmd.Done({}, inventur={"k": [1]}))
    register.empfangen(_command("inventory", payload={"stunden": 1}))
    assert sent.results == [_fail("intern")]


def test_the_log_line_of_a_failed_inventory_escapes_the_command_id_of_the_server(tmp_path, caplog):
    """Die command_id kommt vom Server (bis 64 Zeichen, UTF-8-kodierbar, Steuerzeichen
    erlaubt); im Log steht sie mit Escape auf einer Zeile, keine vorgetaeuschte zweite Logzeile."""
    def too_large(command_id, daten):
        raise ValueError("zu gross")

    register = cmd.CommandRegister(tmp_path, Sent(), send_inventory=too_large)
    register.register("inventory", lambda payload: cmd.Done({}, inventur={"k": [1]}))
    with caplog.at_level(logging.ERROR, logger="smartheat_device.commands"):
        register.empfangen(_command("inventory", "c1\r\nERROR    gefaelscht\x1b[31m", payload={"stunden": 1}))
    (zeile,) = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Inventur zu Befehl")]
    assert zeile == "Inventur zu Befehl c1\\r\\nERROR    gefaelscht\\x1b[31m nicht gesendet (ValueError)"


def test_a_started_marker_is_on_disk_before_the_handler_runs(tmp_path):
    seen = []

    def handler(payload):
        seen.append(json.loads((tmp_path / cmd.STORE_FILE).read_text()))
        return cmd.Done({})

    register = cmd.CommandRegister(tmp_path, Sent())
    register.register("diagnostics", handler)
    register.empfangen(_command())
    assert seen == [[["c1", None, None, None]]]
    assert json.loads((tmp_path / cmd.STORE_FILE).read_text()) == [["c1", True, {}, None]]


def test_a_waiting_command_is_not_run_again_after_a_restart(tmp_path):
    sent, calls, clock = Sent(), [], Clock()

    def sign_off(payload):
        calls.append(1)
        return cmd.Waiting(lambda: None, clock.now + 60)

    first = cmd.CommandRegister(tmp_path, sent, clock=clock)
    first.register("sign_off", sign_off)
    first.empfangen(_command("sign_off"))
    restarted = cmd.CommandRegister(tmp_path, sent, clock=clock)
    restarted.register("sign_off", sign_off)
    restarted.empfangen(_command("sign_off"))  # nach hello erneut zugestellt
    restarted.empfangen(_command("sign_off"))  # weiteres Duplikat
    assert calls == [1]
    assert sent.results == [_fail("intern")] * 2
    assert json.loads((tmp_path / cmd.STORE_FILE).read_text()) == [
        ["c1", False, None, {"grund": "intern"}]]


def test_a_repeatable_command_runs_again_after_a_restart_and_sends_its_result(tmp_path):
    sent, calls, clock = Sent(), [], Clock()
    done = []

    def sign_off(payload):
        calls.append(1)
        return cmd.Waiting(lambda: cmd.Done({"n": 1}) if done else None, clock.now + 60)

    first = cmd.CommandRegister(tmp_path, sent, clock=clock)
    first.register("sign_off", sign_off, wiederholbar=True)
    first.empfangen(_command("sign_off"))
    assert json.loads((tmp_path / cmd.STORE_FILE).read_text()) == [["c1", None, None, None]]
    restarted = cmd.CommandRegister(tmp_path, sent, clock=clock)
    restarted.register("sign_off", sign_off, wiederholbar=True)
    restarted.empfangen(_command("sign_off"))  # nach hello erneut zugestellt: der Handler laeuft noch einmal
    restarted.empfangen(_command("sign_off"))  # weiteres Duplikat waehrend es wartet: nichts
    assert calls == [1, 1] and sent.results == []
    assert json.loads((tmp_path / cmd.STORE_FILE).read_text()) == [["c1", None, None, None]]
    done.append(1)
    restarted.pruefen()
    assert sent.results == [_ok(result={"n": 1})]
    restarted.empfangen(_command("sign_off"))  # fertig: aus dem Speicher beantwortet, nicht noch einmal ausgefuehrt
    assert calls == [1, 1] and sent.results == [_ok(result={"n": 1})] * 2


def test_a_started_command_of_a_kind_that_is_not_repeatable_still_ends_intern(tmp_path):
    sent, calls = Sent(), []
    (tmp_path / cmd.STORE_FILE).write_text(json.dumps([["c1", None, None, None]]))
    register = cmd.CommandRegister(tmp_path, sent)
    register.register("diagnostics", lambda payload: calls.append(1) or cmd.Done({}))
    register.register("sign_off", lambda payload: calls.append(2) or cmd.Done({}), wiederholbar=True)
    register.empfangen(_command("diagnostics"))  # erneut zugestellt als Art ohne wiederholbar: intern
    assert calls == [] and sent.results == [_fail("intern")]


def test_a_crash_between_handler_and_result_is_not_run_again(tmp_path):
    sent, calls = Sent(), []
    (tmp_path / cmd.STORE_FILE).write_text(json.dumps([["c1", None, None, None]]))  # Marker ohne Ergebnis
    register = cmd.CommandRegister(tmp_path, sent)
    register.register("diagnostics", lambda payload: calls.append(1) or cmd.Done({}))
    register.empfangen(_command())
    assert calls == [] and sent.results == [_fail("intern")]


def test_a_handler_crash_logs_kind_type_and_frame_only(tmp_path, caplog):
    def broken(payload):
        raise KeyError("geheim-im-text")

    register = cmd.CommandRegister(tmp_path, Sent())
    register.register("diagnostics", broken)
    with caplog.at_level("ERROR"):
        register.empfangen(_command())
    assert "diagnostics" in caplog.text and "KeyError" in caplog.text and "test_device_commands.py" in caplog.text
    assert "geheim-im-text" not in caplog.text


def test_the_store_keeps_the_last_500(tmp_path):
    register = cmd.CommandRegister(tmp_path, Sent())
    register.register("diagnostics", lambda payload: cmd.Done({}))
    for number in range(cmd.DONE_KEEP + 5):
        register.empfangen(_command(command_id=f"c{number}"))
    stored = json.loads((tmp_path / cmd.STORE_FILE).read_text())
    assert len(stored) == cmd.DONE_KEEP and stored[0][0] == "c5"


def test_a_broken_store_counts_as_empty(tmp_path):
    (tmp_path / cmd.STORE_FILE).write_text("[")
    calls = []
    register = cmd.CommandRegister(tmp_path, Sent())
    register.register("diagnostics", lambda payload: calls.append(1) or cmd.Done({}))
    register.empfangen(_command())
    assert calls == [1]


# --- hinweis ---

def test_a_hint_is_set_persisted_and_cleared(tmp_path):
    hinweise = cmd.Hinweise(tmp_path)
    assert hinweise.handler({"key": "geraet_doppelt", "stufe": "kritisch", "text": "Kopie stoppen"}) == cmd.Done({})
    assert hinweise.alle() == ({"key": "geraet_doppelt", "stufe": "kritisch", "text": "Kopie stoppen"},)
    assert cmd.Hinweise(tmp_path).alle() == hinweise.alle()
    hinweise.handler({"key": "geraet_doppelt", "stufe": "kritisch", "text": ""})
    assert hinweise.alle() == () and cmd.Hinweise(tmp_path).alle() == ()


@pytest.mark.parametrize("payload", [
    {"key": "", "stufe": "info", "text": "x"}, {"key": "k", "stufe": "laut", "text": "x"},
    {"key": "k", "stufe": "info", "text": 5}, {"key": "k" * 65, "stufe": "info", "text": "x"},
])
def test_invalid_hints_are_refused(tmp_path, payload):
    with pytest.raises(cmd.InvalidPayload) as error:
        cmd.Hinweise(tmp_path).handler(payload)
    assert error.value.details["feld"] in ("key", "stufe", "text")


def test_hinweis_ueber_der_obergrenze_wird_abgelehnt(tmp_path):
    """Ein neuer Schluessel nur bis 32; Ueberschreiben und Loeschen gehen weiter, die Datei
    bleibt bei der Ablehnung unveraendert (der Server sendet nur die fuenf wire.MELDUNGEN, keine gueltige Eingabe faellt)."""
    hinweise = cmd.Hinweise(tmp_path)
    for nr in range(32):
        hinweise.handler({"key": f"k{nr}", "stufe": "info", "text": "t"})
    vorher = (tmp_path / cmd.HINWEISE_FILE).read_bytes()
    with pytest.raises(cmd.InvalidPayload) as error:
        hinweise.handler({"key": "zu_viel", "stufe": "info", "text": "t"})
    assert error.value.details["feld"] == "key" and error.value.details["regel"] == "ungueltig"
    assert (tmp_path / cmd.HINWEISE_FILE).read_bytes() == vorher and len(hinweise.alle()) == 32
    hinweise.handler({"key": "k0", "stufe": "kritisch", "text": "neu"})  # Ueberschreiben geht
    assert {"key": "k0", "stufe": "kritisch", "text": "neu"} in hinweise.alle()
    hinweise.handler({"key": "unbekannt", "stufe": "info", "text": ""})  # Loeschen eines fehlenden Schluessels geht
    hinweise.handler({"key": "k1", "stufe": "info", "text": ""})
    assert len(hinweise.alle()) == 32 - 1
    hinweise.handler({"key": "zu_viel", "stufe": "info", "text": "t"})  # wieder Platz
    assert len(hinweise.alle()) == 32


def test_the_hint_command_through_the_register(tmp_path):
    sent = Sent()
    register = cmd.CommandRegister(tmp_path, sent)
    register.register("hinweis", cmd.Hinweise(tmp_path).handler)
    register.empfangen(_command("hinweis", payload={"key": "k", "stufe": "laut", "text": "x"}))
    assert sent.results[0]["error"]["grund"] == "ungueltige_nutzlast"


def test_a_command_id_with_a_lone_surrogate_is_dropped_like_an_invalid_one(tmp_path):
    """Das Ergebnis steht unter der command_id in befehle.json: ein einzelnes Surrogat liesse sich nicht speichern
    (UnicodeEncodeError aus empfangen). Es gilt wie jede ungueltige ID."""
    sent, calls = Sent(), []
    register = cmd.CommandRegister(tmp_path, sent)
    register.register("diagnostics", lambda payload: calls.append(payload) or cmd.Done({}))
    register.empfangen(_command(command_id="c\ud800"))
    assert calls == [] and sent.results == [] and not (tmp_path / cmd.STORE_FILE).exists()
    register.empfangen(_command(command_id="c1"))  # Gegenprobe: eine gueltige ID laeuft
    assert calls == [{}] and len(sent.results) == 1


def test_a_hint_with_a_lone_surrogate_is_refused_and_does_not_poison_the_stored_hints(tmp_path):
    """Der Befehl hinweis legte den Hinweis erst in den Speicher und scheiterte dann beim Schreiben; jeder spaetere Hinweis
    scheiterte mit."""
    sent = Sent()
    register, hinweise = cmd.CommandRegister(tmp_path, sent), cmd.Hinweise(tmp_path)
    register.register("hinweis", hinweise.handler)
    register.empfangen(_command("hinweis", "c1", {"key": "k", "stufe": "info", "text": "a\ud800"}))
    register.empfangen(_command("hinweis", "c2", {"key": "k", "stufe": "info", "text": "gut"}))
    assert [(r["command_id"], r["ok"]) for r in sent.results] == [("c1", False), ("c2", True)]
    assert sent.results[0]["error"]["grund"] == "ungueltige_nutzlast"
    assert hinweise.alle() == ({"key": "k", "stufe": "info", "text": "gut"},)
