"""Hebel-Pipeline, Plan 3b: physische Schreibvorgaenge, globales Tagesbudget (Weishaupt), Lebensdauerzaehler,
Schreibgruppen (Viessmann), zurueckgestellte Serverwerte. Vaillant bleibt unberuehrt (test_core_pipeline.py)."""
import dataclasses

import pytest

from heizungsbruecke.ha_binding import HaPlantBinding
from smartheat_core import write_budget
from smartheat_core.binding import VAILLANT_MYPYLLANT, VIESSMANN_VICARE_BINDING, WEISHAUPT_MODBUS
from smartheat_core.pipeline import LIFETIME_KEY, DeviceWriteError, LeverPipeline, WriteBudgetExhausted
from smartheat_core.safety import resolve_local_safety
from smartheat_runtime.backup_store import load_backup
from smartheat_runtime.roles import ChannelManifest

DAY = "2026-10-03"
NEXT_DAY = "2026-10-04"
# Weishaupt ohne Vorbereitung (Betriebsart nicht gemappt): reines Budget-Verhalten; die Reihenfolge Komfort/Normal/
# Absenk testet test_ha_binding_weishaupt.py.
WH_MANIFEST = ChannelManifest(refs={
    "curve_current": "number.hk", "shift_current": "number.normal", "heat_limit": "number.swu",
})
VI_MANIFEST = ChannelManifest(refs={
    "curve_current": "number.slope", "level_current": "number.shift", "shift_current": "number.normal",
})
WH_SAFETY = resolve_local_safety("weishaupt_wwp", "Heizkoerper")
# Ohne Hilfs-Ursprungswerte (Betriebsart, Komfort/Absenk): die testet test_core_pipeline_originals.py.
WEISHAUPT = dataclasses.replace(WEISHAUPT_MODBUS, aux_originals=())
VIESSMANN = dataclasses.replace(VIESSMANN_VICARE_BINDING, aux_originals=())
VI_SAFETY = resolve_local_safety("viessmann_vicare", "Heizkoerper")


class Ha:
    """Spiegelt Schreibvorgaenge sofort (Modbus lokal); `failing` = Entity-IDs, deren Schreiben scheitert."""

    def __init__(self, states):
        self.states = dict(states)
        self.writes = []
        self.failing = set()

    def get_state(self, entity_id):
        return self.states[entity_id]

    def get_raw_state(self, entity_id):
        return str(self.states.get(entity_id, "on"))

    def set_number_value(self, entity_id, value):
        if entity_id in self.failing:
            raise RuntimeError("Modbus-Fehler")
        self.states[entity_id] = value
        self.writes.append((entity_id, value))


@pytest.fixture(autouse=True)
def _day(monkeypatch):
    monkeypatch.setattr(write_budget, "today", lambda: DAY)


def _weishaupt(make_store, clock, backup=None, description=WEISHAUPT, notify=None):
    store = make_store(backup=backup)
    ha = Ha({"number.hk": 0.75, "number.normal": 20.0, "number.swu": 18.0})
    pipeline = LeverPipeline(
        store, HaPlantBinding(ha, WH_MANIFEST, description), WH_SAFETY, clock=clock, notify=notify,
    )
    clock.advance(description.settle_seconds + 1)
    return pipeline, store, ha


def _total(store):
    return write_budget.count_today(write_budget.get(store, write_budget.TOTAL), DAY)


def test_every_physical_write_counts_into_the_daily_total_and_the_lifetime(make_store, clock, tmp_path):
    pipeline, store, ha = _weishaupt(make_store, clock)
    pipeline.apply_server_values({"curve": 0.8, "room_setpoint": 21.0, "heat_limit": 17.0})
    assert len(ha.writes) == 3
    assert _total(store) == 3
    assert store.state.lifetime_writes == 3
    backup = load_backup(tmp_path / "backup.json")
    assert backup["write_budget"][write_budget.TOTAL]["count"] == 3
    assert backup["lifetime_writes"] == 3


def test_an_unchanged_value_costs_nothing(make_store, clock):
    pipeline, store, ha = _weishaupt(make_store, clock)
    pipeline.apply_server_values({"curve": 0.75, "room_setpoint": 20.0, "heat_limit": 18.0})
    assert ha.writes == [] and _total(store) == 0 and store.state.lifetime_writes == 0


def test_server_values_stop_at_the_daily_limit_but_the_restore_point_is_saved(make_store, clock):
    notes = []
    pipeline, store, ha = _weishaupt(
        make_store, clock, backup={"write_budget": {write_budget.TOTAL: {"day": DAY, "count": 10}}},
        notify=lambda *args: notes.append(args),
    )
    pipeline.apply_server_values({"curve": 0.8, "room_setpoint": 21.0, "heat_limit": 17.0})
    assert ha.writes == []
    assert store.state.restore_point == {"curve": 0.8, "room_setpoint": 21.0, "heat_limit": 17.0}
    assert store.state.deferred_levers == ("curve", "room_setpoint", "heat_limit")
    assert [note[:2] for note in notes] == [("schreibbudget", DAY)]


def test_the_limit_can_be_reached_in_the_middle_of_an_answer(make_store, clock):
    pipeline, store, ha = _weishaupt(make_store, clock, backup={"write_budget": {write_budget.TOTAL: {"day": DAY, "count": 9}}})
    pipeline.apply_server_values({"curve": 0.8, "room_setpoint": 21.0, "heat_limit": 17.0})
    assert ha.writes == [("number.hk", 0.8)]
    assert _total(store) == 10
    assert store.state.deferred_levers == ("curve", "room_setpoint", "heat_limit")


def test_deferred_values_are_written_after_the_day_change_and_cleared(make_store, clock, monkeypatch):
    pipeline, store, ha = _weishaupt(make_store, clock, backup={"write_budget": {write_budget.TOTAL: {"day": DAY, "count": 10}}})
    pipeline.apply_server_values({"curve": 0.8, "room_setpoint": 21.0, "heat_limit": 17.0})
    pipeline.write_deferred()  # gleicher Tag: nichts
    assert ha.writes == []
    monkeypatch.setattr(write_budget, "today", lambda: NEXT_DAY)
    pipeline.write_deferred()
    assert ha.writes == [("number.hk", 0.8), ("number.normal", 21.0), ("number.swu", 17.0)]
    assert store.state.deferred_levers == ()
    assert write_budget.get(store, write_budget.TOTAL)["day"] == NEXT_DAY and _total_next(store) == 3


def _total_next(store):
    return write_budget.count_today(write_budget.get(store, write_budget.TOTAL), NEXT_DAY)


def test_deferred_levers_survive_a_restart(make_store, clock, tmp_path):
    pipeline, store, ha = _weishaupt(make_store, clock, backup={"write_budget": {write_budget.TOTAL: {"day": DAY, "count": 10}}})
    pipeline.apply_server_values({"curve": 0.8, "room_setpoint": 21.0, "heat_limit": 17.0})
    reloaded = make_store()
    assert reloaded.state.deferred_levers == ("curve", "room_setpoint", "heat_limit")
    assert _total(reloaded) == 10  # Tagesstand uebersteht den Neustart


@pytest.mark.parametrize("flags", [{"boost_active": True}, {"emergency_boost_active": True}])
def test_boost_and_restore_ignore_the_limit_but_are_counted(make_store, clock, flags):
    point = {"curve": 0.75, "room_setpoint": 20.0, "heat_limit": 18.0}
    pipeline, store, ha = _weishaupt(
        make_store, clock,
        backup={"restore_point": point, "write_budget": {write_budget.TOTAL: {"day": DAY, "count": 10}}},
    )
    pipeline.set_boosts(comfort="boost_active" in flags, emergency="emergency_boost_active" in flags)
    assert ha.writes == [("number.hk", 1.0), ("number.normal", 25.0), ("number.swu", 23.0)]
    assert _total(store) == 13
    pipeline.set_boosts(comfort=False, emergency=False)
    assert ha.writes[-3:] == [("number.hk", 0.75), ("number.normal", 20.0), ("number.swu", 18.0)]
    assert _total(store) == 16


def test_restore_and_clear_ignores_the_limit(make_store, clock):
    pipeline, store, ha = _weishaupt(
        make_store, clock,
        backup={
            "restore_point": {"curve": 0.8, "room_setpoint": 21.0, "heat_limit": 17.0}, "boost_active": True,
            "write_budget": {write_budget.TOTAL: {"day": DAY, "count": 10}},
        },
    )
    assert pipeline.restore_and_clear(always_restore=False) is True
    assert ha.writes == [("number.hk", 0.8), ("number.normal", 21.0), ("number.swu", 17.0)]


def test_enforce_writes_raise_at_the_limit(make_store, clock):
    pipeline, store, ha = _weishaupt(
        make_store, clock,
        backup={"restore_point": {"curve": 0.8}, "write_budget": {write_budget.TOTAL: {"day": DAY, "count": 10}}},
    )
    assert pipeline.daily_budget_reached() is True
    with pytest.raises(WriteBudgetExhausted):
        pipeline.write_levers(("curve",))
    assert ha.writes == []


def test_the_counter_starts_fresh_on_the_next_day(make_store, clock, monkeypatch):
    pipeline, store, ha = _weishaupt(make_store, clock, backup={"write_budget": {write_budget.TOTAL: {"day": DAY, "count": 10}}})
    monkeypatch.setattr(write_budget, "today", lambda: NEXT_DAY)
    assert pipeline.daily_budget_reached() is False
    pipeline.apply_server_values({"curve": 0.8, "room_setpoint": 20.0, "heat_limit": 18.0})
    assert ha.writes == [("number.hk", 0.8)] and _total_next(store) == 1


def test_a_failed_write_is_not_counted(make_store, clock):
    pipeline, store, ha = _weishaupt(make_store, clock)
    ha.failing.add("number.hk")
    with pytest.raises(DeviceWriteError):
        pipeline.apply_server_values({"curve": 0.8, "room_setpoint": 20.0, "heat_limit": 18.0})
    assert _total(store) == 0 and store.state.lifetime_writes == 0


def test_lifetime_hint_at_the_threshold_and_every_further_10000(make_store, clock):
    notes = []
    pipeline, store, ha = _weishaupt(make_store, clock, backup={"lifetime_writes": 49999}, notify=lambda *a: notes.append(a))
    pipeline.apply_server_values({"curve": 0.8, "room_setpoint": 20.0, "heat_limit": 18.0})
    assert store.state.lifetime_writes == 50000
    assert [(key, state) for key, state, _ in notes] == [(LIFETIME_KEY, "50000")]
    assert "50.000" in notes[0][2]
    store.update(lifetime_writes=59999)
    pipeline.apply_server_values({"curve": 0.85, "room_setpoint": 20.0, "heat_limit": 18.0})
    assert notes[-1][:2] == (LIFETIME_KEY, "60000")


def test_vaillant_counts_nothing_and_never_writes_the_new_fields(make_store, clock, tmp_path):
    store = make_store()
    ha = Ha({"number.curve": 0.7, "number.shift": 21.0})
    manifest = ChannelManifest(refs={"curve_current": "number.curve", "shift_current": "number.shift"})
    pipeline = LeverPipeline(store, HaPlantBinding(ha, manifest), resolve_local_safety("vaillant_vrc720", "Heizkoerper"),
                             clock=clock)
    pipeline.apply_server_values({"curve": 0.9, "room_setpoint": 22.0, "heat_limit": 16.0})
    assert len(ha.writes) == 2
    backup = load_backup(tmp_path / "backup.json")
    assert "lifetime_writes" not in backup and "deferred_levers" not in backup
    assert write_budget.TOTAL not in backup.get("write_budget", {})
    assert pipeline.daily_budget_reached() is False


# --- Schreibgruppen (Viessmann setCurve) ---

def _viessmann(make_store, clock, states=None, description=VIESSMANN):
    store = make_store()
    ha = Ha({"number.slope": 1.0, "number.shift": 0.0, "number.normal": 20.0, **(states or {})})
    pipeline = LeverPipeline(store, HaPlantBinding(ha, VI_MANIFEST, description), VI_SAFETY, clock=clock)
    clock.advance(description.settle_seconds + 1)
    return pipeline, store, ha


def test_a_changed_group_member_writes_the_whole_group(make_store, clock):
    pipeline, store, ha = _viessmann(make_store, clock)
    pipeline.apply_server_values({"curve": 1.0, "level": -2.0, "room_setpoint": 20.0})
    assert ha.writes == [("number.slope", 1.0), ("number.shift", -2.0)]


def test_an_unchanged_group_is_not_written(make_store, clock):
    pipeline, store, ha = _viessmann(make_store, clock)
    pipeline.apply_server_values({"curve": 1.0, "level": 0.0, "room_setpoint": 21.0})
    assert ha.writes == [("number.normal", 21.0)]


def test_a_group_counts_as_one_write(make_store, clock):
    counted = dataclasses.replace(VIESSMANN, daily_write_limit=10, lifetime_hint_at=50000)
    pipeline, store, ha = _viessmann(make_store, clock, description=counted)
    pipeline.apply_server_values({"curve": 1.2, "level": -2.0, "room_setpoint": 20.0})
    assert len(ha.writes) == 2
    assert _total(store) == 1 and store.state.lifetime_writes == 1


def test_a_failing_second_half_rewrites_both_next_time(make_store, clock):
    counted = dataclasses.replace(VIESSMANN, daily_write_limit=10)
    pipeline, store, ha = _viessmann(make_store, clock, description=counted)
    ha.failing.add("number.shift")
    with pytest.raises(DeviceWriteError) as error:
        pipeline.apply_server_values({"curve": 1.2, "level": -2.0, "room_setpoint": 20.0})
    assert error.value.lever == "level"
    assert ha.writes == [("number.slope", 1.2)]
    assert _total(store) == 1
    ha.failing.clear()
    pipeline.write_levers(("level",))  # Durchsetzen/naechster Versuch: die ganze Gruppe
    assert ha.writes[1:] == [("number.slope", 1.2), ("number.shift", -2.0)]
    assert _total(store) == 2


def test_write_levers_expands_a_group_member_to_the_group(make_store, clock):
    pipeline, store, ha = _viessmann(make_store, clock)
    store.update(restore_point={"curve": 1.0, "level": -3.0, "room_setpoint": 20.0})
    pipeline.write_levers(("level",))
    assert ha.writes == [("number.slope", 1.0), ("number.shift", -3.0)]


def test_vaillant_binding_counts_physical_writes_too():
    ha = Ha({"number.curve": 0.7})
    binding = HaPlantBinding(ha, ChannelManifest(refs={"curve_current": "number.curve"}), VAILLANT_MYPYLLANT)
    binding.write("curve", 0.9)
    assert binding.physical_writes == 1
