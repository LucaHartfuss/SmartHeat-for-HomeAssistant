"""HA-Binding Viessmann (Plan 3b, HA-Core vicare): Heizprogramm als Vorbereitung, Hilfs-Ursprungswert, mit der Pipeline
Neigung und Niveau als eine Schreibgruppe."""
import pytest

from heizungsbruecke.ha_binding import VIESSMANN_NORMAL_PRESET, ViessmannHaBinding
from heizungsbruecke.manifest import ChannelManifest
from smartheat_core.binding import VIESSMANN_VICARE_BINDING
from smartheat_core.pipeline import LeverPipeline
from smartheat_core.safety import resolve_local_safety

MANIFEST = ChannelManifest(entity_ids={
    "curve_current": "number.slope", "level_current": "number.shift", "shift_current": "number.normal_temperature",
    "mode_select": "climate.heizkreis",
})


class FakeHa:
    def __init__(self, preset="home", **states):
        self.states = {
            "number.slope": 1.0, "number.shift": 0.0, "number.normal_temperature": 20.0,
            "climate.heizkreis::preset_mode": preset, **states,
        }
        self.calls = []

    def get_state(self, entity_id):
        return self.states[entity_id]

    def get_raw_state(self, entity_id):
        return "on"

    def get_attribute(self, entity_id, attribute):
        return str(self.states[f"{entity_id}::{attribute}"])

    def set_number_value(self, entity_id, value):
        self.calls.append(("number", entity_id, value))
        self.states[entity_id] = value

    def set_preset_mode(self, entity_id, preset):
        self.calls.append(("preset", entity_id, preset))
        self.states[f"{entity_id}::preset_mode"] = preset


def _binding(ha):
    return ViessmannHaBinding(ha, MANIFEST, VIESSMANN_VICARE_BINDING)


@pytest.mark.parametrize(("preset", "prepared"), [("home", True), ("sleep", True), ("comfort", False), ("eco", False)])
def test_only_comfort_and_eco_programs_count_as_not_prepared(preset, prepared):
    assert _binding(FakeHa(preset)).is_prepared() is prepared


def test_prepare_leaves_comfort_once():
    ha = FakeHa("comfort")
    binding = _binding(ha)
    assert binding.prepare() is True and binding.prepare() is False
    assert ha.calls == [("preset", "climate.heizkreis", VIESSMANN_NORMAL_PRESET)]
    assert binding.physical_writes == 1


def test_reduced_program_of_the_schedule_is_left_alone():
    ha = FakeHa("sleep")
    assert _binding(ha).prepare() is False and ha.calls == []


def test_level_is_a_plain_number_write():
    ha = FakeHa()
    binding = _binding(ha)
    binding.write("level", -3.0)
    assert ha.calls == [("number", "number.shift", -3.0)] and binding.ref("level") == "number.shift"


@pytest.mark.parametrize(("original", "current", "calls"), [
    ("eco", "home", [("preset", "climate.heizkreis", "eco")]),
    ("comfort", "comfort", []),
    ("home", "home", []),
    ("sleep", "home", []),
])
def test_restore_aux_reactivates_only_a_user_program(original, current, calls):
    ha = FakeHa(current)
    _binding(ha).restore_aux({"mode_select": original})
    assert ha.calls == calls


def test_with_the_pipeline_curve_and_level_go_together_after_leaving_eco(make_store, clock):
    ha = FakeHa("eco")
    store = make_store()
    pipeline = LeverPipeline(store, _binding(ha), resolve_local_safety("viessmann_vicare", "Heizkoerper"), clock=clock)
    clock.advance(VIESSMANN_VICARE_BINDING.settle_seconds + 1)
    pipeline.apply_server_values({"curve": 1.1, "level": 0.0, "room_setpoint": 21.0})
    assert store.state.aux_originals == {"mode_select": "eco"}
    assert store.state.originals == {"curve": 1.0, "level": 0.0, "room_setpoint": 20.0}
    assert ha.calls == [
        ("preset", "climate.heizkreis", "home"), ("number", "number.slope", 1.1), ("number", "number.shift", 0.0),
        ("number", "number.normal_temperature", 21.0),
    ]
    ha.calls.clear()
    assert pipeline.restore_and_clear(always_restore=False) is True
    assert ha.calls == [
        ("number", "number.slope", 1.0), ("number", "number.shift", 0.0),
        ("number", "number.normal_temperature", 20.0), ("preset", "climate.heizkreis", "eco"),
    ]


def test_a_restore_without_auxiliary_originals_leaves_the_users_program(make_store, clock):
    # Hilfswert nie gemerkt (Backup ohne aux_originals), der Nutzer hat inzwischen Komfort aktiv und die Neigung weicht
    # vom Ursprungswert ab: die Neigungsgruppe wird zurueckgeschrieben, das Heizprogramm bleibt beim Nutzer (Review Task 8).
    ha = FakeHa("comfort", **{"number.slope": 1.3})
    store = make_store(backup={
        "restore_point": {"curve": 1.3, "level": 0.0, "room_setpoint": 20.0},
        "originals": {"curve": 1.0},
    })
    pipeline = LeverPipeline(store, _binding(ha), resolve_local_safety("viessmann_vicare", "Heizkoerper"), clock=clock)
    clock.advance(VIESSMANN_VICARE_BINDING.settle_seconds + 1)
    assert store.state.aux_originals == {}
    assert pipeline.restore_and_clear(always_restore=True) is True
    assert all(call[0] != "preset" for call in ha.calls)
    assert ("number", "number.slope", 1.0) in ha.calls
    assert ha.states["climate.heizkreis::preset_mode"] == "comfort"
