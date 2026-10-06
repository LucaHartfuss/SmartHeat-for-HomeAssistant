import math

import pytest

from heizungsbruecke.ha_binding import MANUAL_HVAC_MODE, ROOM_SETPOINT_READ_MAX, HaPlantBinding, entity_of, is_climate
from smartheat_core.binding import VAILLANT_MYPYLLANT
from smartheat_core.clamping import target_value
from smartheat_runtime.roles import ChannelManifest

MANIFEST = ChannelManifest(refs={
    "curve_current": "number.curve", "shift_current": "climate.zone::temperature",
    "heat_limit": "number.g", "min_flow": "number.mf",
})
ZONE = "climate.zone::temperature"


class FakeHa:
    def __init__(self, states=None):
        self.states = {"climate.zone": MANUAL_HVAC_MODE, **(states or {})}
        self.calls = []

    def get_state(self, ref):
        value = self.states[ref]
        if isinstance(value, Exception):
            raise value
        return value

    def get_raw_state(self, entity_id):
        value = self.states.get(entity_id, "on")  # nicht hinterlegt = verfuegbar
        if isinstance(value, Exception):
            raise value
        if value in ("unavailable", "unknown", ""):
            raise ValueError(f"{entity_id} nicht verfuegbar: {value!r}")
        return str(value)

    def set_number_value(self, entity_id, value):
        self.calls.append(("number", entity_id, value))

    def set_climate_temperature(self, entity_id, value):
        self.calls.append(("climate", entity_id, value))

    def set_hvac_mode(self, entity_id, mode):
        self.calls.append(("mode", entity_id, mode))
        self.states[entity_id] = mode


def test_has_and_ref_follow_the_manifest_roles():
    binding = HaPlantBinding(FakeHa(), MANIFEST)
    assert binding.ref("curve") == "number.curve"
    assert binding.ref("room_setpoint") == "climate.zone::temperature"
    assert binding.has("min_flow") and not binding.has("level")
    assert not HaPlantBinding(FakeHa(), ChannelManifest(refs={"curve_current": "number.curve"})).has("heat_limit")


@pytest.mark.parametrize(("raw", "expected"), [(21.5, 21.5), (0.0, None), (4.9, None), (math.nan, None)])
def test_room_setpoint_below_five_means_zone_inactive(raw, expected):
    binding = HaPlantBinding(FakeHa({"climate.zone::temperature": raw}), MANIFEST)
    assert binding.read("room_setpoint") == expected


def test_room_setpoint_above_the_plausible_maximum_is_a_read_error():
    binding = HaPlantBinding(FakeHa({"climate.zone::temperature": 36.0}), MANIFEST)
    with pytest.raises(ValueError, match="35"):
        binding.read("room_setpoint")


def test_room_setpoint_boundary_values_are_plausible():
    # Ersetzt test_plant.test_read_shift_above_max_raises (Grenzwert gilt noch) und Zonen 20.5 -> 20.5.
    with pytest.raises(ValueError):
        HaPlantBinding(FakeHa({ZONE: 35.5}), MANIFEST).read("room_setpoint")
    assert HaPlantBinding(FakeHa({ZONE: ROOM_SETPOINT_READ_MAX}), MANIFEST).read("room_setpoint") == ROOM_SETPOINT_READ_MAX
    assert HaPlantBinding(FakeHa({ZONE: 20.5}), MANIFEST).read("room_setpoint") == 20.5


def test_other_levers_are_read_unfiltered():
    binding = HaPlantBinding(FakeHa({"number.curve": 1.05, "number.g": 0.0}), MANIFEST)
    assert binding.read("curve") == 1.05
    assert binding.read("heat_limit") == 0.0


def test_read_or_falls_back_on_inactive_zone_and_read_errors():
    assert HaPlantBinding(FakeHa({"climate.zone::temperature": 0.0}), MANIFEST).read_or("room_setpoint", 18.0) == 18.0
    failing = FakeHa({"climate.zone::temperature": RuntimeError("weg")})
    assert HaPlantBinding(failing, MANIFEST).read_or("room_setpoint", 18.0) == 18.0
    assert HaPlantBinding(FakeHa({"climate.zone::temperature": 20.0}), MANIFEST).read_or("room_setpoint", 18.0) == 20.0


def test_read_or_falls_back_when_value_above_max_and_keeps_none_fallback():
    assert HaPlantBinding(FakeHa({ZONE: 40.0}), MANIFEST).read_or("room_setpoint", 21.0) == 21.0
    assert HaPlantBinding(FakeHa({ZONE: 0.0}), MANIFEST).read_or("room_setpoint", None) is None
    assert HaPlantBinding(FakeHa({ZONE: 22.5}), MANIFEST).read_or("room_setpoint", 21.0) == 22.5


def test_write_checks_availability_and_uses_the_entity_service():
    ha = FakeHa()
    binding = HaPlantBinding(ha, MANIFEST)
    binding.write("curve", 1.05)
    binding.write("room_setpoint", 21.5)
    assert ha.calls == [("number", "number.curve", 1.05), ("climate", "climate.zone", 21.5)]


def test_write_never_switches_the_mode_itself():
    # Ersetzt test_plant.test_write_climate_ensure_mode_false_skips_mode_check und
    # ..._already_manual_only_sets_temperature: der Modus gehoert zu prepare(), write schreibt nur.
    ha = FakeHa({"climate.zone": "auto"})
    HaPlantBinding(ha, MANIFEST).write("room_setpoint", 25.0)
    assert ha.calls == [("climate", "climate.zone", 25.0)]


def test_write_value_matches_the_old_plant_rounding_with_the_binding_steps():
    # Ersetzt die Rundungs-Erwartungen von test_plant.test_write_climate_switches_to_manual_first_and_rounds und
    # test_write_number_rounds_to_role_step: die Rundung liegt jetzt in target_value mit description.steps.
    steps = VAILLANT_MYPYLLANT.steps
    assert target_value(20.37, 15.0, 25.0, steps["room_setpoint"]) == 20.5
    assert target_value(26.0, 15.0, 25.0, steps["room_setpoint"]) == 25.0
    assert target_value(1.0147, 0.4, 1.5, steps["curve"]) == 1.0
    assert target_value(20.46, 20.0, 30.0, steps["min_flow"]) == 20.5
    assert steps["heat_limit"] == 0.1


def test_number_and_climate_write_calls_use_the_right_services():
    # Ersetzt test_plant.test_write_number_rounds_to_role_step (Aufrufe) und ..._switches_to_manual_first (Climate).
    ha = FakeHa()
    binding = HaPlantBinding(ha, MANIFEST)
    binding.write("curve", 1.0)
    binding.write("min_flow", 20.5)
    binding.write("room_setpoint", 20.5)
    assert ha.calls == [("number", "number.curve", 1.0), ("number", "number.mf", 20.5), ("climate", "climate.zone", 20.5)]


def test_write_to_an_unavailable_entity_raises_and_writes_nothing():
    ha = FakeHa({"number.curve": "unavailable"})
    with pytest.raises(ValueError, match="nicht verfuegbar"):
        HaPlantBinding(ha, MANIFEST).write("curve", 1.05)
    assert ha.calls == []


@pytest.mark.parametrize("state", ["unavailable", "unknown"])
def test_write_refuses_an_unavailable_number_in_either_state(state):
    ha = FakeHa({"number.curve": state})
    with pytest.raises(ValueError, match=state):
        HaPlantBinding(ha, MANIFEST).write("curve", 1.0)
    assert ha.calls == []


def test_write_refuses_an_unavailable_zone():
    ha = FakeHa({"climate.zone": "unavailable"})
    with pytest.raises(ValueError):
        HaPlantBinding(ha, MANIFEST).write("room_setpoint", 20.0)
    assert ha.calls == []


def test_prepare_switches_the_zone_to_manual_once():
    ha = FakeHa({"climate.zone": "auto"})
    binding = HaPlantBinding(ha, MANIFEST)
    assert binding.needs_preparation() and not binding.is_prepared()
    assert binding.prepare() is True
    assert binding.prepare() is False
    assert binding.is_prepared()
    assert ha.calls == [("mode", "climate.zone", MANUAL_HVAC_MODE)]


def test_prepare_on_an_already_manual_zone_sends_nothing():
    ha = FakeHa({"climate.zone": MANUAL_HVAC_MODE})
    assert HaPlantBinding(ha, MANIFEST).prepare() is False
    assert ha.calls == []


def test_number_entity_as_room_setpoint_needs_no_preparation():
    manifest = ChannelManifest(refs={**MANIFEST.refs, "shift_current": "number.zone"})
    ha = FakeHa()
    binding = HaPlantBinding(ha, manifest)
    assert not binding.needs_preparation()
    assert binding.prepare() is False
    assert ha.calls == []


@pytest.mark.parametrize(("ref", "entity"), [(ZONE, "climate.zone"), ("number.x", "number.x")])
def test_entity_of(ref, entity):
    assert entity_of(ref) == entity


def test_is_climate():
    assert is_climate(ZONE) and not is_climate("number.x")


# --- Plan 3b: Binding je Hebelsatz ---

@pytest.mark.parametrize(("lever_set", "cls"), [
    (None, "HaPlantBinding"), ("weishaupt_wwp", "WeishauptHaBinding"), ("weishaupt_wwp_basis", "WeishauptHaBinding"),
    ("viessmann_vicare", "ViessmannHaBinding"),
])
def test_binding_for_picks_the_class_and_description_of_the_lever_set(lever_set, cls):
    from heizungsbruecke.ha_binding import binding_for

    options = {} if lever_set is None else {"lever_set": lever_set}
    binding = binding_for(options, FakeHa(), MANIFEST)
    assert type(binding).__name__ == cls
    assert binding.description.lever_set.id == (lever_set or "vaillant_vrc720")


def test_binding_roles_for_sign_off():
    from heizungsbruecke.ha_binding import binding_roles
    from smartheat_core.binding import VIESSMANN_VICARE_BINDING, WEISHAUPT_MODBUS

    assert binding_roles(VAILLANT_MYPYLLANT) == ("curve_current", "shift_current", "heat_limit")
    assert binding_roles(WEISHAUPT_MODBUS) == (
        "curve_current", "shift_current", "heat_limit", "mode_select", "setpoint_comfort", "setpoint_setback",
    )
    assert binding_roles(VIESSMANN_VICARE_BINDING) == ("curve_current", "level_current", "shift_current", "mode_select")
