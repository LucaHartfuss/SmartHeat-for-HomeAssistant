"""Hebel-Pipeline, Plan 3b: physische Schreibvorgaenge, globales Tagesbudget (Weishaupt), Lebensdauerzaehler,
Schreibgruppen (Viessmann), zurueckgestellte Serverwerte. Vaillant bleibt unberuehrt (test_core_pipeline.py)."""
import dataclasses
import logging

import pytest
from bindung_beispiele import VAILLANT_MYPYLLANT, VIESSMANN_VICARE_BINDING, WEISHAUPT_MODBUS

from heizungsbruecke.ha_binding import HaPlantBinding
from smartheat_core import write_budget
from smartheat_core.pipeline import (
    BUDGET_KEY,
    LIFETIME_KEY,
    START_LIMIT_MESSAGE,
    DeviceWriteError,
    LeverPipeline,
    WriteBudgetExhausted,
)
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


POINT = {"curve": 0.75, "room_setpoint": 20.0, "heat_limit": 18.0}


@pytest.mark.parametrize(("comfort", "emergency"), [(True, False), (False, True), (True, True)])
def test_a_boost_does_not_start_at_the_daily_limit(make_store, clock, comfort, emergency):
    notes = []
    pipeline, store, ha = _weishaupt(
        make_store, clock, backup={"restore_point": POINT, "write_budget": AT_LIMIT},
        notify=lambda *args: notes.append(args),
    )
    assert pipeline.set_boosts(comfort=comfort, emergency=emergency) == (False, False)
    assert ha.writes == []
    assert write_budget.get(store, write_budget.BOOST_START) is None
    assert write_budget.get(store, write_budget.EMERGENCY_START) is None
    assert _total(store) == 10
    assert {note[:3] for note in notes} == {(BUDGET_KEY, DAY, START_LIMIT_MESSAGE.format(limit=10))}
    assert pipeline.comfort_start_heute_gesperrt is comfort


def test_the_start_refusal_text_covers_both_facts_of_the_shared_daily_notice(make_store, clock):
    # Gleicher Schluessel und Tag wie BUDGET_MESSAGE: kommt der Boost-Hinweis zuerst, darf der Hinweis auf die
    # verschobenen Werte nicht fehlen (und der Text hat wie alle Hinweise das Praefix).
    notes = []
    pipeline, _, _ = _weishaupt(
        make_store, clock, backup={"restore_point": POINT, "write_budget": AT_LIMIT}, notify=lambda *args: notes.append(args))
    pipeline.set_boosts(comfort=True, emergency=False)
    text = notes[0][2]
    assert text.startswith("SmartHeat: Das Tageslimit von 10 Schreibvorgängen")
    assert "morgen geschrieben" in text and text.endswith("Ein Boost startet erst morgen wieder.")


def test_refused_start_reports_of_other_days_are_dropped(make_store, clock):
    pipeline, _, _ = _weishaupt(make_store, clock, backup={"restore_point": POINT, "write_budget": AT_LIMIT})
    pipeline._abgelehnt_gemeldet.add(("comfort", "tageslimit", "2000-01-01"))
    pipeline.set_boosts(comfort=True, emergency=False)
    assert pipeline._abgelehnt_gemeldet
    assert {m[2] for m in pipeline._abgelehnt_gemeldet} == {DAY}


@pytest.mark.parametrize("flags", [{"boost_active": True}, {"emergency_boost_active": True}])
def test_a_running_boost_ends_at_the_daily_limit(make_store, clock, flags):
    pipeline, store, ha = _weishaupt(
        make_store, clock, backup={"restore_point": POINT, "write_budget": AT_LIMIT, **flags},
    )
    ha.states.update({"number.hk": 1.0, "number.normal": 25.0, "number.swu": 23.0})  # die Anlage steht auf dem Boost
    assert pipeline.set_boosts(comfort=False, emergency=False) == (False, False)
    assert ha.writes == [("number.hk", 0.75), ("number.normal", 20.0), ("number.swu", 18.0)]
    assert _total(store) == 13
    assert pipeline.comfort_start_heute_gesperrt is False


def test_a_boost_starts_below_the_limit_and_can_cross_it(make_store, clock):
    pipeline, store, ha = _weishaupt(
        make_store, clock, backup={"restore_point": POINT, "write_budget": {write_budget.TOTAL: {"day": DAY, "count": 9}}},
    )
    assert pipeline.set_boosts(comfort=True, emergency=False) == (True, False)
    assert ha.writes == [("number.hk", 1.0), ("number.normal", 25.0), ("number.swu", 23.0)]
    assert _total(store) == 12
    assert pipeline.comfort_start_heute_gesperrt is False


def test_flapping_operations_are_bounded_weishaupt(make_store, clock):
    # FK-1: Tageslimit 10, ein Boost schreibt 3 und sein Ende 3 Werte; nach zwei Starts ist das Limit erreicht.
    pipeline, store, ha = _weishaupt(make_store, clock, backup={"restore_point": POINT})
    for _ in range(50):
        pipeline.set_boosts(comfort=True, emergency=False)
        pipeline.set_boosts(comfort=False, emergency=False)
        clock.advance(1800)
    assert ha.writes.count(("number.hk", 1.0)) == 2
    assert ha.writes.count(("number.hk", 0.75)) == 2
    assert _total(store) == 12
    assert pipeline.set_boosts(comfort=True, emergency=False) == (False, False)
    assert pipeline.comfort_start_heute_gesperrt is True


def _refusals(caplog):
    return [r for r in caplog.records if r.levelno == logging.INFO and "abgelehnt" in r.getMessage()]


def test_refusal_reason_is_logged_once_a_day(make_store, clock, monkeypatch, caplog):
    pipeline, store, ha = _weishaupt(make_store, clock, backup={"restore_point": POINT, "write_budget": AT_LIMIT})
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            assert pipeline.set_boosts(comfort=True, emergency=False) == (False, False)
        assert len(_refusals(caplog)) == 1
        caplog.clear()
        # Gegenprobe: am naechsten Tag (wieder am Limit) kommt die Zeile erneut, danach nicht mehr.
        monkeypatch.setattr(write_budget, "today", lambda: NEXT_DAY)
        write_budget.put(store, write_budget.TOTAL, {"day": NEXT_DAY, "count": 10, "last": 0.0})
        for _ in range(3):
            pipeline.set_boosts(comfort=True, emergency=False)
        assert len(_refusals(caplog)) == 1


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
    pipeline = LeverPipeline(store, HaPlantBinding(ha, manifest, VAILLANT_MYPYLLANT), resolve_local_safety("vaillant_vrc720", "Heizkoerper"),
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


def test_a_group_without_write_group_counts_each_physical_write(make_store, clock):
    # A6-09: ohne write_group sind es zwei physische Schreibvorgaenge, und beide zaehlen (nicht min(diff, 1))
    counted = dataclasses.replace(VIESSMANN, daily_write_limit=10, lifetime_hint_at=50000)
    pipeline, store, ha = _viessmann(make_store, clock, description=counted)
    pipeline.apply_server_values({"curve": 1.2, "level": -2.0, "room_setpoint": 20.0})
    assert len(ha.writes) == 2
    assert _total(store) == 2 and store.state.lifetime_writes == 2


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
    assert _total(store) == 3


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


class GroupBinding(HaPlantBinding):
    """Binding mit optionaler Gruppenschreibung (Audit 4, A4-31): ein physischer Aufruf fuer alle Mitglieder."""

    def __init__(self, ha, manifest, description):
        super().__init__(ha, manifest, description)
        self.group_calls = []
        self.single_calls = []
        self.group_error = None

    def write(self, lever, value):
        self.single_calls.append((lever, value))
        super().write(lever, value)

    def write_group(self, values):
        if self.group_error is not None:
            raise self.group_error
        self.group_calls.append(dict(values))
        self.physical_writes += 1


def _group_pipeline(make_store, clock, description=VIESSMANN):
    store = make_store()
    ha = Ha({"number.slope": 1.0, "number.shift": 0.0, "number.normal": 20.0})
    binding = GroupBinding(ha, VI_MANIFEST, description)
    pipeline = LeverPipeline(store, binding, VI_SAFETY, clock=clock)
    clock.advance(description.settle_seconds + 1)
    return pipeline, binding, store


def test_a_binding_with_write_group_gets_one_call_with_all_targets(make_store, clock):
    # Audit 4, A4-31: Gruppenschreibung statt zwei Einzelschreibvorgaengen
    counted = dataclasses.replace(VIESSMANN, daily_write_limit=10, lifetime_hint_at=50000)
    pipeline, binding, store = _group_pipeline(make_store, clock, counted)
    pipeline.apply_server_values({"curve": 1.2, "level": -2.0, "room_setpoint": 20.0})
    assert binding.group_calls == [{"curve": 1.2, "level": -2.0}] and binding.single_calls == []
    assert _total(store) == 1 and store.state.lifetime_writes == 1
    # je Mitglied gilt der Hebel als geschrieben: der naechste Tick mit denselben Zielwerten schreibt nichts mehr
    assert pipeline._last_written["curve"] == 1.2 and pipeline._last_written["level"] == -2.0


def test_a_failing_group_write_names_the_first_member_and_counts_nothing(make_store, clock):
    counted = dataclasses.replace(VIESSMANN, daily_write_limit=10)
    pipeline, binding, store = _group_pipeline(make_store, clock, counted)
    binding.group_error = RuntimeError("abgelehnt")
    with pytest.raises(DeviceWriteError) as error:
        pipeline.apply_server_values({"curve": 1.2, "level": -2.0, "room_setpoint": 20.0})
    assert error.value.lever == "curve"
    assert "curve" not in pipeline._last_written and "level" not in pipeline._last_written
    assert _total(store) == 0


# --- Mutationsprobe Stufe 3 S4b (A5-22 Rest): Tagesbudget, Zaehlung, Lebensdauer-Hinweis ---

AT_LIMIT = {write_budget.TOTAL: {"day": DAY, "count": 10}}


def test_only_the_levers_of_this_answer_are_deferred(make_store, clock):
    pipeline, store, ha = _weishaupt(make_store, clock, backup={"write_budget": AT_LIMIT})
    pipeline.apply_server_values({"curve": 0.8})
    assert ha.writes == []
    assert store.state.deferred_levers == ("curve",)


@pytest.mark.parametrize("flags", [{"boost_active": True}, {"emergency_boost_active": True}])
def test_deferred_values_are_not_written_during_a_boost(make_store, clock, flags):
    pipeline, store, ha = _weishaupt(
        make_store, clock, backup={"restore_point": {"curve": 0.8, "room_setpoint": 20.0, "heat_limit": 18.0}, **flags},
    )
    store.update(deferred_levers=("curve",))
    assert pipeline.daily_budget_reached() is False
    pipeline.write_deferred()
    assert ha.writes == []  # das Boost-Ende uebernimmt
    assert store.state.deferred_levers == ("curve",)


def test_a_write_group_and_a_boost_start_stop_at_the_daily_limit(make_store, clock):
    counted = dataclasses.replace(VIESSMANN, daily_write_limit=10)
    pipeline, store, ha = _viessmann(make_store, clock, description=counted)
    store.update(write_budget=AT_LIMIT)
    pipeline.apply_server_values({"curve": 1.2, "level": -2.0, "room_setpoint": 20.0})
    assert ha.writes == []
    assert store.state.deferred_levers == ("curve", "level", "room_setpoint")
    with pytest.raises(WriteBudgetExhausted) as error:
        pipeline.write_levers(("level",))
    assert error.value.lever == "curve"  # die Gruppe meldet ihr erstes Mitglied
    assert pipeline.set_boosts(comfort=True, emergency=False) == (False, False)  # am Limit startet kein Boost
    assert ha.writes == []
    assert _total(store) == 10


def test_a_failing_group_write_names_the_entity_of_the_first_member(make_store, clock):
    pipeline, binding, store = _group_pipeline(make_store, clock)
    binding.group_error = RuntimeError("abgelehnt")
    with pytest.raises(DeviceWriteError) as error:
        pipeline.apply_server_values({"curve": 1.2, "level": -2.0, "room_setpoint": 20.0})
    assert (error.value.lever, error.value.entity_id) == ("curve", "number.slope")


def test_a_failing_group_write_after_an_earlier_write_counts_nothing(make_store, clock):
    counted = dataclasses.replace(VIESSMANN, daily_write_limit=10, lifetime_hint_at=50000)
    pipeline, binding, store = _group_pipeline(make_store, clock, counted)
    pipeline.apply_server_values({"room_setpoint": 21.0})
    assert binding.physical_writes == 1 and _total(store) == 1
    binding.group_error = RuntimeError("abgelehnt")
    with pytest.raises(DeviceWriteError):
        pipeline.apply_server_values({"curve": 1.2, "level": -2.0})
    assert _total(store) == 1 and store.state.lifetime_writes == 1


def test_a_failed_write_neither_creates_the_counter_nor_repeats_the_lifetime_hint(make_store, clock, tmp_path):
    notes = []
    pipeline, store, ha = _weishaupt(make_store, clock, backup={"lifetime_writes": 50000}, notify=lambda *a: notes.append(a))
    ha.failing.add("number.hk")
    with pytest.raises(DeviceWriteError):
        pipeline.apply_server_values({"curve": 0.8})
    assert notes == []
    assert write_budget.TOTAL not in load_backup(tmp_path / "backup.json").get("write_budget", {})
    assert store.state.lifetime_writes == 50000


def test_no_lifetime_hint_below_the_threshold(make_store, clock):
    notes = []
    pipeline, store, ha = _weishaupt(make_store, clock, notify=lambda *a: notes.append(a))
    pipeline.apply_server_values({"curve": 0.8, "room_setpoint": 21.0, "heat_limit": 17.0})
    assert store.state.lifetime_writes == 3
    assert notes == []


def test_write_lever_is_not_exempt_from_the_daily_limit(make_store, clock):
    # Mindestvorlauf (write_lever) und der Startwert in prepare_start zaehlen gegen das Tagesbudget.
    store = make_store()
    ha = Ha({"number.curve": 0.7, "number.min_flow": 20.0})
    manifest = ChannelManifest(refs={"curve_current": "number.curve", "min_flow": "number.min_flow"})
    limited = dataclasses.replace(VAILLANT_MYPYLLANT, daily_write_limit=1)
    pipeline = LeverPipeline(store, HaPlantBinding(ha, manifest, limited),
                             resolve_local_safety("vaillant_vrc720", "Heizkoerper"), clock=clock)
    assert pipeline.write_lever("min_flow", 22.0) == 22.0
    with pytest.raises(WriteBudgetExhausted):
        pipeline.write_lever("min_flow", 25.0)
    assert ha.writes == [("number.min_flow", 22.0)]
