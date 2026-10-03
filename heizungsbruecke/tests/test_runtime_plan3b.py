"""Betrieb mit den Bindings aus Plan 3b ueber _start_bridge (Harness aus test_runtime.py): Option lever_set waehlt
Hebelsatz, Binding und lokale Sicherheit; fehlende Pflicht-Entities sind ein Konfigurationsfehler; Vaillant ohne
lever_set bleibt unveraendert (alle Tests in test_runtime.py)."""
import logging

from test_runtime import (
    _backup,
    _last_event,
    _mqtt,
    _start,
    _start_bridge,
    _trigger,
    env,  # noqa: F401  (registriert die Fixture `env`)
)

from heizungsbruecke import __main__ as main_module
from smartheat_core import write_budget
from smartheat_core.pipeline import BUDGET_KEY, BUDGET_MESSAGE, LeverPipeline, WriteBudgetExhausted

WEISHAUPT = {
    "lever_set": "weishaupt_wwp", "entity_curve_current": "number.hk", "entity_shift_current": "number.normal",
    "entity_heat_limit": "number.swu", "entity_mode_select": "select.betriebsart",
    "entity_setpoint_comfort": "number.komfort", "entity_setpoint_setback": "number.absenk", "entity_min_flow": "",
}
WEISHAUPT_STATES = {
    "number.hk": 0.75, "number.normal": 20.0, "number.swu": 18.0, "select.betriebsart": "Automatik",
    "number.komfort": 22.0, "number.absenk": 18.0,
}
VIESSMANN = {
    "lever_set": "viessmann_vicare", "entity_curve_current": "number.slope", "entity_level_current": "number.shift",
    "entity_shift_current": "number.normal_temperature", "entity_mode_select": "climate.heizkreis",
    "entity_heat_limit": "", "entity_min_flow": "",
}
VIESSMANN_STATES = {
    "number.slope": 1.0, "number.shift": 0.0, "number.normal_temperature": 20.0, "climate.heizkreis::preset_mode": "eco",
}


def _first_snapshot(env, bridge):
    _trigger(env, bridge, "sensor.room_target")
    return _mqtt(env).snapshots[-1]


def _answer_levers(env, bridge, seq, levers):
    _mqtt(env).answer(seq, levers=levers)
    bridge.worker.run_pending()


def test_weishaupt_start_remembers_originals_prepares_and_reports_its_levers(env):
    env.ha.states.update(WEISHAUPT_STATES)
    bridge = _start(env, **WEISHAUPT)

    assert env.ha.writes == [("select.betriebsart", "Normal")]  # Normal-Soll 20 bleibt (Register, kein Startwert)
    backup = _backup(env)
    assert backup["aux_originals"] == {"mode_select": "Automatik", "setpoint_comfort": 22.0, "setpoint_setback": 18.0}
    assert backup["originals"] == {"curve": 0.75, "room_setpoint": 20.0, "heat_limit": 18.0}
    snapshot = _first_snapshot(env, bridge)
    assert snapshot["levers"] == {"curve": 0.75, "room_setpoint": 20.0, "heat_limit": 18.0}
    assert snapshot["readonly"] == []

    _answer_levers(env, bridge, snapshot["seq"], {"curve": 0.8, "room_setpoint": 23.0, "heat_limit": 17.0})

    assert env.ha.writes[1:] == [
        ("number.hk", 0.8), ("number.komfort", 23.0), ("number.normal", 23.0), ("number.swu", 17.0),
    ]
    assert _backup(env)["lifetime_writes"] == 5
    assert _backup(env)["write_budget"]["writes:total"]["count"] == 5


def test_weishaupt_basis_reports_the_curve_readonly_and_writes_only_the_room_setpoint(env):
    env.ha.states.update(WEISHAUPT_STATES)
    bridge = _start(env, **{**WEISHAUPT, "lever_set": "weishaupt_wwp_basis", "entity_heat_limit": ""})

    snapshot = _first_snapshot(env, bridge)
    assert snapshot["levers"] == {"room_setpoint": 20.0, "curve": 0.75}
    assert snapshot["readonly"] == ["curve"]

    _answer_levers(env, bridge, snapshot["seq"], {"room_setpoint": 21.5})
    assert env.ha.writes[1:] == [("number.normal", 21.5)]
    assert _last_event(env)["datenfehler"] is None


def test_weishaupt_without_a_comfort_setpoint_is_a_configuration_error(env):
    env.ha.states.update(WEISHAUPT_STATES)
    # Eigenes Passwort: das Harness-Passwort "p" wuerde im Grund unkenntlich gemacht (_without_credentials).
    bridge = _start_bridge(env, **{**WEISHAUPT, "entity_setpoint_comfort": ""}, mqtt_password="x9y8z7")

    assert bridge.reason == "konfigurationsfehler"
    assert "entity_setpoint_comfort" in _last_event(env)["grund"]
    assert "weishaupt_wwp" in _last_event(env)["grund"]
    assert env.ha.writes == []


def test_an_unknown_lever_set_is_a_configuration_error_not_vaillant(env):
    bridge = _start_bridge(env, lever_set="buderus")
    assert bridge.reason == "konfigurationsfehler"
    assert "lever_set" in _last_event(env)["grund"]
    assert env.ha.writes == []


def test_weishaupt_sign_off_restores_levers_and_operating_mode(env):
    env.ha.states.update(WEISHAUPT_STATES)
    bridge = _start(env, **WEISHAUPT)
    snapshot = _first_snapshot(env, bridge)
    _answer_levers(env, bridge, snapshot["seq"], {"curve": 0.8, "room_setpoint": 23.0, "heat_limit": 17.0})
    env.ha.writes.clear()

    signed_off = _start_bridge(env, **WEISHAUPT, abgemeldet=True)

    assert signed_off.reason == "abgemeldet"
    assert env.ha.writes == [
        ("number.hk", 0.75), ("number.normal", 20.0), ("number.swu", 18.0), ("number.komfort", 22.0),
        ("select.betriebsart", "Automatik"),
    ]


def test_viessmann_start_leaves_eco_and_writes_curve_and_level_together(env):
    env.ha.states.update(VIESSMANN_STATES)
    bridge = _start(env, **VIESSMANN)

    assert env.ha.writes == [("climate.heizkreis::preset_mode", "home")]
    snapshot = _first_snapshot(env, bridge)
    assert snapshot["levers"] == {"curve": 1.0, "level": 0.0, "room_setpoint": 20.0}

    _answer_levers(env, bridge, snapshot["seq"], {"curve": 1.0, "level": -2.0, "room_setpoint": 21.0})
    assert env.ha.writes[1:] == [("number.slope", 1.0), ("number.shift", -2.0), ("number.normal_temperature", 21.0)]
    assert "lifetime_writes" not in _backup(env)


def test_main_has_no_vaillant_constant_left():
    assert not hasattr(main_module, "REQUIRED_ENTITY_OPTIONS")
    assert not hasattr(main_module, "VAILLANT_MYPYLLANT")


# --- Carry-forwards aus den Reviews der Tasks 5/8 ---

def _exhaust_daily_budget(env, bridge) -> int:
    limit = bridge.override.binding.description.daily_write_limit
    write_budget.put(
        bridge.store, write_budget.TOTAL, write_budget.added(None, limit, env.clock(), write_budget.today()),
    )
    return limit


def test_daily_limit_notice_goes_through_the_notifier_once_a_day(env):
    # notify= der Pipeline ist Notifier.notify (Meldezustand je Schluessel): zweimal am Limit, eine Meldung.
    env.ha.states.update(WEISHAUPT_STATES)
    bridge = _start(env, **WEISHAUPT)
    limit = _exhaust_daily_budget(env, bridge)
    pushes, writes = len(env.ha.pushes), len(env.ha.writes)

    for _ in range(2):
        bridge.override.apply_server_values({"curve": 0.8, "room_setpoint": 23.0, "heat_limit": 17.0})

    assert env.ha.writes[writes:] == []
    assert [push for push in env.ha.pushes[pushes:] if "Tageslimit" in push] == [BUDGET_MESSAGE.format(limit=limit)]
    assert bridge.store.state.notify_states[BUDGET_KEY] == write_budget.today()


def test_zone_preparation_at_the_daily_limit_is_a_budget_stop_not_a_failed_attempt(env, monkeypatch, caplog):
    # Weishaupt-Tageslimit: kein Stacktrace "Zone konnte nicht vorbereitet werden", kein Versuch im Kontingent.
    attempts = []

    def _prepare_start(self, start_value):
        attempts.append(env.clock())
        raise WriteBudgetExhausted("room_setpoint", 10)

    monkeypatch.setattr(LeverPipeline, "prepare_start", _prepare_start)
    env.ha.states.update(WEISHAUPT_STATES)
    with caplog.at_level(logging.INFO, logger="heizungsbruecke.__main__"):
        bridge = _start(env, **WEISHAUPT)
        _trigger(env, bridge, "sensor.room_actual")

    assert len(attempts) == 2  # kein Wiederholungs-Kontingent verbraucht: der naechste lokale Check versucht es erneut
    assert bridge.zone_prepared is False
    assert write_budget.get(bridge.store, write_budget.ZONE_PREPARE) is None
    assert not any("Zone konnte nicht vorbereitet werden" in record.getMessage() for record in caplog.records)
    assert any(
        record.levelno == logging.INFO and "Tageslimit" in record.getMessage() for record in caplog.records
    )
