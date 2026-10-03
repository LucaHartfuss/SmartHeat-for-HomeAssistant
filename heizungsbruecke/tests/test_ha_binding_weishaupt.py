"""HA-Binding Weishaupt (Plan 3b, weishaupt_modbus): Betriebsart "Normal" als Vorbereitung, Reihenfolge
Komfort/Normal/Absenk, Hilfs-Ursprungswerte, physische Schreibvorgaenge; mit der Pipeline: Fehlschlag mitten in der
Reihenfolge und Wiederherstellung."""
import pytest

from heizungsbruecke.ha_binding import WEISHAUPT_NORMAL_MODE, WeishauptHaBinding
from heizungsbruecke.manifest import ChannelManifest
from smartheat_core import write_budget
from smartheat_core.binding import WEISHAUPT_MODBUS
from smartheat_core.pipeline import DeviceWriteError, LeverPipeline, WriteBudgetExhausted
from smartheat_core.safety import resolve_local_safety

MANIFEST = ChannelManifest(entity_ids={
    "curve_current": "number.hk", "shift_current": "number.normal", "heat_limit": "number.swu",
    "mode_select": "select.betriebsart", "setpoint_comfort": "number.komfort", "setpoint_setback": "number.absenk",
})


class FakeHa:
    def __init__(self, **states):
        self.states = {
            "number.hk": 0.75, "number.normal": 20.0, "number.swu": 18.0, "select.betriebsart": "Automatik",
            "number.komfort": 22.0, "number.absenk": 18.0, **states,
        }
        self.calls = []
        self.failing = set()

    def get_state(self, entity_id):
        value = self.states[entity_id]
        if isinstance(value, Exception):
            raise value
        return value

    def get_raw_state(self, entity_id):
        value = self.states.get(entity_id, "on")
        if value in ("unavailable", "unknown", ""):
            raise ValueError(f"{entity_id} nicht verfuegbar: {value!r}")
        return str(value)

    def set_number_value(self, entity_id, value):
        if entity_id in self.failing:
            raise RuntimeError("Modbus-Fehler")
        self.calls.append(("number", entity_id, value))
        self.states[entity_id] = value

    def select_option(self, entity_id, option):
        if entity_id in self.failing:
            raise RuntimeError("Modbus-Fehler")
        self.calls.append(("select", entity_id, option))
        self.states[entity_id] = option


def _binding(ha):
    return WeishauptHaBinding(ha, MANIFEST, WEISHAUPT_MODBUS)


@pytest.mark.parametrize(("value", "calls"), [
    (23.0, [("number", "number.komfort", 23.0), ("number", "number.normal", 23.0)]),  # anheben ueber Komfort
    (22.0, [("number", "number.normal", 22.0)]),  # Grenzfall Normal == Komfort: kein Hilfsschreiben
    (21.0, [("number", "number.normal", 21.0)]),
    (18.0, [("number", "number.normal", 18.0)]),  # Grenzfall Normal == Absenk
    (16.5, [("number", "number.absenk", 16.5), ("number", "number.normal", 16.5)]),  # senken unter Absenk
])
def test_normal_setpoint_keeps_setback_below_and_comfort_above(value, calls):
    ha = FakeHa()
    binding = _binding(ha)
    binding.write("room_setpoint", value)
    assert ha.calls == calls
    assert binding.physical_writes == len(calls)
    assert ha.states["number.absenk"] <= ha.states["number.normal"] <= ha.states["number.komfort"]


def test_other_levers_are_plain_number_writes():
    ha = FakeHa()
    binding = _binding(ha)
    binding.write("curve", 0.8)
    binding.write("heat_limit", 17.5)
    assert ha.calls == [("number", "number.hk", 0.8), ("number", "number.swu", 17.5)]


def test_a_failing_comfort_write_leaves_the_normal_setpoint_untouched():
    ha = FakeHa()
    ha.failing.add("number.komfort")
    binding = _binding(ha)
    with pytest.raises(RuntimeError):
        binding.write("room_setpoint", 23.0)
    assert ha.calls == [] and binding.physical_writes == 0
    assert ha.states["number.normal"] == 20.0


def test_an_unreadable_comfort_setpoint_is_a_write_error():
    ha = FakeHa(**{"number.komfort": float("nan")})
    with pytest.raises(ValueError, match="setpoint_comfort"):
        _binding(ha).write("room_setpoint", 23.0)
    assert ha.calls == []


def test_preparation_switches_the_operating_mode_to_normal_once():
    ha = FakeHa()
    binding = _binding(ha)
    assert binding.needs_preparation() and not binding.is_prepared()
    assert binding.prepare() is True
    assert binding.prepare() is False
    assert ha.calls == [("select", "select.betriebsart", WEISHAUPT_NORMAL_MODE)]
    assert binding.physical_writes == 1


def test_without_mode_select_there_is_nothing_to_prepare():
    manifest = ChannelManifest(entity_ids={k: v for k, v in MANIFEST.entity_ids.items() if k != "mode_select"})
    binding = WeishauptHaBinding(FakeHa(), manifest, WEISHAUPT_MODBUS)
    assert binding.needs_preparation() is False and binding.prepare() is False


def test_read_aux_returns_mode_and_both_auxiliary_setpoints():
    assert _binding(FakeHa()).read_aux() == {
        "mode_select": "Automatik", "setpoint_comfort": 22.0, "setpoint_setback": 18.0,
    }


def test_restore_aux_writes_only_differences_in_a_safe_order():
    ha = FakeHa(**{"number.komfort": 24.0, "number.absenk": 17.0, "select.betriebsart": "Normal"})
    binding = _binding(ha)
    binding.restore_aux({"mode_select": "Automatik", "setpoint_comfort": 22.0, "setpoint_setback": 18.0})
    assert ha.calls == [
        ("number", "number.komfort", 22.0), ("number", "number.absenk", 18.0),
        ("select", "select.betriebsart", "Automatik"),
    ]
    assert binding.physical_writes == 3
    ha.calls.clear()
    binding.restore_aux({"mode_select": "Automatik", "setpoint_comfort": 22.0, "setpoint_setback": 18.0})
    assert ha.calls == []


def test_restore_aux_never_violates_the_order_when_normal_stayed_high():
    # Ursprungswert des Normal-Solls unbekannt: Normal bleibt 23, Komfort darf nicht auf 22 zurueck.
    ha = FakeHa(**{"number.normal": 23.0, "number.komfort": 23.0})
    _binding(ha).restore_aux({"setpoint_comfort": 22.0, "setpoint_setback": 18.0, "mode_select": "Automatik"})
    assert ("number", "number.komfort", 22.0) not in ha.calls
    assert ha.states["number.komfort"] == 23.0


# --- mit der Pipeline ---

def _pipeline(make_store, clock, ha, backup=None):
    store = make_store(backup=backup)
    pipeline = LeverPipeline(store, _binding(ha), resolve_local_safety("weishaupt_wwp", "Heizkoerper"), clock=clock)
    clock.advance(WEISHAUPT_MODBUS.settle_seconds + 1)
    return pipeline, store


def test_first_server_answer_remembers_originals_prepares_and_writes(make_store, clock):
    ha = FakeHa()
    pipeline, store = _pipeline(make_store, clock, ha)
    pipeline.apply_server_values({"curve": 0.8, "room_setpoint": 23.0, "heat_limit": 17.0})
    assert store.state.aux_originals == {"mode_select": "Automatik", "setpoint_comfort": 22.0, "setpoint_setback": 18.0}
    assert store.state.originals == {"curve": 0.75, "room_setpoint": 20.0, "heat_limit": 18.0}
    assert ha.calls == [
        ("select", "select.betriebsart", "Normal"), ("number", "number.hk", 0.8),
        ("number", "number.komfort", 23.0), ("number", "number.normal", 23.0), ("number", "number.swu", 17.0),
    ]
    assert store.state.lifetime_writes == 5
    assert store.state.write_budget["writes:total"]["count"] == 5


def test_a_failure_in_the_middle_of_the_order_keeps_the_state_consistent_and_retries(make_store, clock):
    ha = FakeHa(**{"select.betriebsart": "Normal"})
    pipeline, store = _pipeline(make_store, clock, ha)
    ha.failing.add("number.normal")
    with pytest.raises(DeviceWriteError) as error:
        pipeline.apply_server_values({"curve": 0.75, "room_setpoint": 23.0, "heat_limit": 18.0})
    assert error.value.lever == "room_setpoint"
    assert ha.calls == [("number", "number.komfort", 23.0)]  # Komfort schon oben, Normal unveraendert
    assert store.state.restore_point["room_setpoint"] == 23.0
    assert store.state.write_budget["writes:total"]["count"] == 1
    assert pipeline.last_written("room_setpoint") is None
    ha.failing.clear()
    pipeline.write_levers(("room_setpoint",))
    assert ha.calls[1:] == [("number", "number.normal", 23.0)]  # Komfort steht schon, nur Normal


def test_sign_off_restores_levers_then_auxiliary_values(make_store, clock):
    ha = FakeHa()
    pipeline, store = _pipeline(make_store, clock, ha)
    pipeline.apply_server_values({"curve": 0.8, "room_setpoint": 23.0, "heat_limit": 17.0})
    ha.calls.clear()
    assert pipeline.restore_and_clear(always_restore=False) is True
    assert ha.calls == [
        ("number", "number.hk", 0.75), ("number", "number.normal", 20.0), ("number", "number.swu", 18.0),
        ("number", "number.komfort", 22.0), ("select", "select.betriebsart", "Automatik"),
    ]
    assert ha.states["number.absenk"] <= ha.states["number.normal"] <= ha.states["number.komfort"]


# --- Vorbereitung am Tageslimit (LeverPipeline.ensure_prepared) und nicht lesbare Hilfswerte ---

DAY = "2026-10-03"
AT_LIMIT = {"write_budget": {write_budget.TOTAL: {"day": DAY, "count": 10}}}


def _total(store):
    return write_budget.count_today(write_budget.get(store, write_budget.TOTAL), DAY)


def test_preparation_at_the_daily_limit_is_refused_when_the_mode_must_change(make_store, clock, monkeypatch):
    monkeypatch.setattr(write_budget, "today", lambda: DAY)
    ha = FakeHa()
    pipeline, store = _pipeline(make_store, clock, ha, backup=AT_LIMIT)
    with pytest.raises(WriteBudgetExhausted):
        pipeline.ensure_prepared()
    assert ha.calls == [] and _total(store) == 10
    # Eine Serverantwort am Limit: nichts geschrieben (die Vorbereitung kommt vor dem ersten Hebel), alles zurueckgestellt.
    pipeline.apply_server_values({"curve": 0.8, "room_setpoint": 23.0, "heat_limit": 17.0})
    assert ha.calls == [] and _total(store) == 10
    assert store.state.deferred_levers == ("curve", "room_setpoint", "heat_limit")
    assert ha.states["select.betriebsart"] == "Automatik"


def test_preparation_at_the_daily_limit_proceeds_when_the_mode_is_already_normal(make_store, clock, monkeypatch):
    monkeypatch.setattr(write_budget, "today", lambda: DAY)
    ha = FakeHa(**{"select.betriebsart": WEISHAUPT_NORMAL_MODE})
    pipeline, store = _pipeline(make_store, clock, ha, backup=AT_LIMIT)
    assert pipeline.ensure_prepared() is False
    assert ha.calls == [] and _total(store) == 10


def test_exempt_preparation_at_the_daily_limit_switches_and_counts(make_store, clock, monkeypatch):
    monkeypatch.setattr(write_budget, "today", lambda: DAY)
    ha = FakeHa()
    pipeline, store = _pipeline(make_store, clock, ha, backup=AT_LIMIT)
    assert pipeline.ensure_prepared(exempt=True) is True
    assert ha.calls == [("select", "select.betriebsart", WEISHAUPT_NORMAL_MODE)]
    assert _total(store) == 11 and store.state.lifetime_writes == 1


def test_a_comfort_boost_at_the_daily_limit_prepares_writes_in_order_and_counts(make_store, clock, monkeypatch):
    monkeypatch.setattr(write_budget, "today", lambda: DAY)
    ha = FakeHa()
    pipeline, store = _pipeline(make_store, clock, ha, backup=AT_LIMIT)
    assert pipeline.set_boosts(comfort=True, emergency=False) == (True, False)
    assert ha.calls == [
        ("select", "select.betriebsart", "Normal"), ("number", "number.hk", 1.0),
        ("number", "number.komfort", 25.0), ("number", "number.normal", 25.0), ("number", "number.swu", 23.0),
    ]
    assert _total(store) == 15


@pytest.mark.parametrize(("comfort", "emergency"), [(True, False), (False, True)])
def test_unreadable_auxiliary_values_block_a_boost_that_writes_the_room_setpoint(make_store, clock, comfort, emergency):
    # Plan-Praezisierung 10: ohne gemerkte Hilfs-Ursprungswerte kein Schreiben des Normal-Solls und keine Umstellung der
    # Betriebsart; weil die Vorbereitung vor dem ersten Hebel kommt, schreibt der Boost gar nichts.
    ha = FakeHa(**{"number.komfort": float("nan")})
    pipeline, store = _pipeline(make_store, clock, ha)
    with pytest.raises(DeviceWriteError) as error:
        pipeline.set_boosts(comfort=comfort, emergency=emergency)
    assert error.value.lever == "room_setpoint"
    assert ha.calls == []
    assert (store.state.boost_active, store.state.emergency_boost_active) == (False, False)
    assert store.state.aux_originals == {}
    # Sobald die Hilfswerte lesbar sind, startet der Boost (nach der Wartezeit des Budgets).
    ha.states["number.komfort"] = 22.0
    clock.advance(3600)
    assert pipeline.set_boosts(comfort=comfort, emergency=emergency) == (comfort, emergency)
    assert store.state.aux_originals == {"mode_select": "Automatik", "setpoint_comfort": 22.0, "setpoint_setback": 18.0}
    assert ha.calls[0] == ("select", "select.betriebsart", "Normal")
