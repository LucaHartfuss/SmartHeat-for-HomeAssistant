"""Durchsetzen je Binding (Plan 3b): Viessmann 4 Versuche am Tag und eine Schreibgruppe curve+level, Weishaupt
Tagesbudget und zurueckgestellte Serverwerte. Vaillant: test_core_enforce.py (unveraendert)."""
import dataclasses
from datetime import date
from unittest.mock import MagicMock

import pytest
from fakes import runtime_config

from heizungsbruecke.ha_binding import HaPlantBinding
from smartheat_core import enforce, write_budget
from smartheat_core.binding import VIESSMANN_VICARE_BINDING, WEISHAUPT_MODBUS
from smartheat_core.pipeline import LeverPipeline
from smartheat_core.safety import resolve_local_safety
from smartheat_runtime.notifier import Notifier
from smartheat_runtime.roles import ChannelManifest
from smartheat_runtime.runtime import Runtime

TODAY = date(2026, 10, 3)
VIESSMANN = dataclasses.replace(VIESSMANN_VICARE_BINDING, aux_originals=())
WEISHAUPT = dataclasses.replace(WEISHAUPT_MODBUS, aux_originals=())
VI_MANIFEST = ChannelManifest(entity_ids={
    "curve_current": "number.slope", "level_current": "number.shift", "shift_current": "number.normal",
})
WH_MANIFEST = ChannelManifest(entity_ids={
    "curve_current": "number.hk", "shift_current": "number.normal", "heat_limit": "number.swu",
})


class Ha:
    def __init__(self, states):
        self.states = dict(states)
        self.writes = []

    def get_state(self, entity_id):
        return self.states[entity_id]

    def get_raw_state(self, entity_id):
        return "on"

    def set_number_value(self, entity_id, value):
        self.states[entity_id] = value
        self.writes.append((entity_id, value))


@pytest.fixture(autouse=True)
def _fixed_day(monkeypatch):
    monkeypatch.setattr(enforce, "_today", lambda: TODAY)
    monkeypatch.setattr(write_budget, "today", lambda: TODAY.isoformat())


def _rt(make_store, clock, description, manifest, states, point, backup=None):
    store = make_store(backup={"restore_point": point, **(backup or {})})
    ha = Ha(states)
    safety = resolve_local_safety(description.lever_set.id, "Heizkoerper")
    notifier = MagicMock(spec=Notifier)
    notifier.notify.return_value = True
    override = LeverPipeline(store, HaPlantBinding(ha, manifest, description), safety, clock=clock)
    clock.advance(description.settle_seconds + 1)
    return Runtime(manifest=manifest, signals=ha, config=runtime_config(), worker=MagicMock(), store=store, override=override,
                   notifier=notifier, clock=clock)


def _rounds(rt):
    for _ in range(enforce.DETECTION_ROUNDS):
        enforce.check_manual_override(rt)


VI_POINT = {"curve": 1.0, "level": -2.0, "room_setpoint": 20.0}
VI_STATES = {"number.slope": 1.0, "number.shift": -2.0, "number.normal": 20.0}


def test_viessmann_rewrites_the_whole_curve_group_under_one_budget_key(make_store, clock):
    rt = _rt(make_store, clock, VIESSMANN, VI_MANIFEST, {**VI_STATES, "number.shift": 1.0}, VI_POINT)
    _rounds(rt)
    assert rt.signals.writes == [("number.slope", 1.0), ("number.shift", -2.0)]
    assert set(rt.store.state.write_budget) == {"enforce:curve+level"}
    message = rt.notifier.notify.call_args.args[2]
    assert "Niveau der Heizkurve" in message and "Neigung" not in message


def test_viessmann_both_group_members_deviating_write_once(make_store, clock):
    rt = _rt(make_store, clock, VIESSMANN, VI_MANIFEST, {**VI_STATES, "number.slope": 1.3, "number.shift": 1.0}, VI_POINT)
    _rounds(rt)
    assert rt.signals.writes == [("number.slope", 1.0), ("number.shift", -2.0)]
    assert rt.store.state.write_budget["enforce:curve+level"]["count"] == 1


def test_viessmann_enforces_at_most_four_times_a_day(make_store, clock):
    rt = _rt(make_store, clock, VIESSMANN, VI_MANIFEST, VI_STATES, VI_POINT)
    for _ in range(8):
        rt.signals.states["number.normal"] = 23.0
        clock.advance(VIESSMANN.settle_seconds + write_budget.INTERVAL_SECONDS + 1)
        _rounds(rt)
    assert [w for w in rt.signals.writes if w[0] == "number.normal"] == [("number.normal", 20.0)] * 4


WH_POINT = {"curve": 0.75, "room_setpoint": 20.0, "heat_limit": 18.0}
WH_STATES = {"number.hk": 0.75, "number.normal": 20.0, "number.swu": 18.0}


def test_weishaupt_at_the_daily_limit_neither_writes_nor_counts(make_store, clock):
    rt = _rt(make_store, clock, WEISHAUPT, WH_MANIFEST, {**WH_STATES, "number.hk": 0.9}, WH_POINT,
             backup={"write_budget": {write_budget.TOTAL: {"day": TODAY.isoformat(), "count": 10}}})
    _rounds(rt)
    assert rt.signals.writes == []
    assert "enforce:curve" not in rt.store.state.write_budget


def test_deferred_server_values_are_no_manual_override_and_are_written_silently(make_store, clock, monkeypatch):
    rt = _rt(make_store, clock, WEISHAUPT, WH_MANIFEST, WH_STATES, WH_POINT,
             backup={"write_budget": {write_budget.TOTAL: {"day": TODAY.isoformat(), "count": 10}}})
    rt.override.apply_server_values({"curve": 0.8, "room_setpoint": 21.0, "heat_limit": 17.0})
    assert rt.store.state.deferred_levers == ("curve", "room_setpoint", "heat_limit")
    _rounds(rt)  # gleicher Tag: offen, kein Eingriff
    assert rt.signals.writes == [] and rt.store.state.manual_override is None
    next_day = date(2026, 10, 4)
    monkeypatch.setattr(enforce, "_today", lambda: next_day)
    monkeypatch.setattr(write_budget, "today", lambda: next_day.isoformat())
    _rounds(rt)
    assert rt.signals.writes == [("number.hk", 0.8), ("number.normal", 21.0), ("number.swu", 17.0)]
    assert rt.store.state.deferred_levers == ()
    assert rt.store.state.manual_override is None
    assert all(call.args[1] == "ok" for call in rt.notifier.notify.call_args_list)


def test_weishaupt_budget_used_up_within_the_tick_stops_without_counting_or_error(make_store, clock, caplog):
    """Der erste Hebel verbraucht den letzten Schreibvorgang des Tages: der zweite wird weder geschrieben noch gezaehlt,
    und das ist kein gescheitertes Zuruecksetzen (kein logger.exception)."""
    rt = _rt(make_store, clock, WEISHAUPT, WH_MANIFEST, {**WH_STATES, "number.hk": 0.9, "number.swu": 15.0}, WH_POINT,
             backup={"write_budget": {write_budget.TOTAL: {"day": TODAY.isoformat(), "count": 9}}})
    with caplog.at_level("INFO"):
        _rounds(rt)
    assert rt.signals.writes == [("number.hk", 0.75)]
    assert "enforce:curve" in rt.store.state.write_budget
    assert "enforce:heat_limit" not in rt.store.state.write_budget
    assert not [record for record in caplog.records if record.levelname == "ERROR"]
