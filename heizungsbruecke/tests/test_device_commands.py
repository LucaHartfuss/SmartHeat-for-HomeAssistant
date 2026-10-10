"""Spec 5b 3.4: Befehle hoechstens einmal ausgefuehrt, Ergebnis vor dem Senden gespeichert, Gruende aus dem Vertrag;
Befehl hinweis (jedes Geraet)."""
import json

import pytest

from smartheat_device import commands as cmd


class Sent:
    def __init__(self, ok: bool = True) -> None:
        self.results: list[dict] = []
        self.ok = ok

    def __call__(self, body: dict) -> bool:
        self.results.append(body)
        return self.ok


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _command(kind="diagnostics", command_id="c1", payload=None):
    return {"schema": 1, "command_id": command_id, "kind": kind, "payload": {} if payload is None else payload,
            "expires_at": "2027-01-15T09:00:00+00:00"}


def _ok(command_id="c1", result=None):
    return {"schema": 1, "command_id": command_id, "ok": True, "result": result or {}, "error": None}


def _fail(grund, text, command_id="c1"):
    return {"schema": 1, "command_id": command_id, "ok": False, "result": None, "error": {"grund": grund, "text": text}}


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
    register.empfangen(_command("zigbee_devices"))  # nicht eingetragen
    register.empfangen({**_command(command_id="c2"), "payload": [1]})
    assert sent.results == [_fail("ungueltige_nutzlast", cmd.UNKNOWN_TEXT),
                            _fail("ungueltige_nutzlast", cmd.UNKNOWN_TEXT, "c2")]


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
    register.register("diagnostics", lambda payload: cmd.Failed("erfunden", "x"))
    register.empfangen(_command())
    assert sent.results == [_fail("intern", cmd.INTERN_TEXT)]


def test_exceptions_become_reasons(tmp_path):
    class DriverDown(Exception):
        pass

    def uebersetzen(error):
        return cmd.Failed("anlage_nicht_erreichbar", "Anlage weg.") if isinstance(error, DriverDown) else None

    def invalid(payload):
        raise cmd.InvalidPayload("seconds fehlt.")

    def down(payload):
        raise DriverDown()

    def broken(payload):
        raise RuntimeError("kaputt")

    sent = Sent()
    register = cmd.CommandRegister(tmp_path, sent, uebersetzen=uebersetzen)
    register.register("zigbee_permit_join", invalid)
    register.register("driver_probe", down)
    register.register("diagnostics", broken)
    for number, kind in enumerate(("zigbee_permit_join", "driver_probe", "diagnostics"), start=1):
        register.empfangen(_command(kind, f"c{number}"))
    assert [result["error"] for result in sent.results] == [
        {"grund": "ungueltige_nutzlast", "text": "seconds fehlt."},
        {"grund": "anlage_nicht_erreichbar", "text": "Anlage weg."},
        {"grund": "intern", "text": cmd.INTERN_TEXT},
    ]


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
    assert sent.results == [_fail(cmd.TIMEOUT_GRUND, cmd.TIMEOUT_TEXT, "c1"), _fail("intern", cmd.INTERN_TEXT, "c2")]


def test_inventory_data_goes_out_before_the_result(tmp_path):
    order = []
    register = cmd.CommandRegister(
        tmp_path, lambda body: order.append(("result", body["command_id"])) or True,
        send_inventory=lambda command_id, daten: order.append(("inventur", command_id, daten)) or True)
    register.register("inventory", lambda payload: cmd.Done({}, inventur={"k": [1]}))
    register.empfangen(_command("inventory", payload={"stunden": 1}))
    assert order == [("inventur", "c1", {"k": [1]}), ("result", "c1")]


def test_an_inventory_waits_while_offline(tmp_path):
    sent, online, parts = Sent(), {"ok": False}, []
    register = cmd.CommandRegister(tmp_path, sent,
                                   send_inventory=lambda command_id, daten: parts.append(command_id) or online["ok"])
    register.register("inventory", lambda payload: cmd.Done({}, inventur={"k": [1]}))
    register.empfangen(_command("inventory", payload={"stunden": 1}))
    assert sent.results == [] and parts == ["c1"]
    online["ok"] = True
    register.pruefen()
    assert parts == ["c1", "c1"] and sent.results == [_ok()]


def test_a_too_large_inventory_fails_intern(tmp_path):
    def too_large(command_id, daten):
        raise ValueError("zu gross")

    sent = Sent()
    register = cmd.CommandRegister(tmp_path, sent, send_inventory=too_large)
    register.register("inventory", lambda payload: cmd.Done({}, inventur={"k": [1]}))
    register.empfangen(_command("inventory", payload={"stunden": 1}))
    assert sent.results == [_fail("intern", cmd.INVENTUR_TEXT)]


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
    with pytest.raises(cmd.InvalidPayload):
        cmd.Hinweise(tmp_path).handler(payload)


def test_the_hint_command_through_the_register(tmp_path):
    sent = Sent()
    register = cmd.CommandRegister(tmp_path, sent)
    register.register("hinweis", cmd.Hinweise(tmp_path).handler)
    register.empfangen(_command("hinweis", payload={"key": "k", "stufe": "laut", "text": "x"}))
    assert sent.results[0]["error"]["grund"] == "ungueltige_nutzlast"
