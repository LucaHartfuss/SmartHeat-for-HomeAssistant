import pytest

from heizungsbruecke import plant


class Ha:
    def __init__(self, states):
        self.states = dict(states)
        self.calls = []

    def get_state(self, ref):
        value = self.states[ref]
        if isinstance(value, Exception):
            raise value
        return value

    def get_raw_state(self, entity_id):
        return self.states[entity_id]

    def set_hvac_mode(self, entity_id, mode):
        self.calls.append(("hvac", entity_id, mode))
        self.states[entity_id] = mode

    def set_climate_temperature(self, entity_id, value):
        self.calls.append(("climate", entity_id, value))

    def set_number_value(self, entity_id, value):
        self.calls.append(("number", entity_id, value))


ZONE = "climate.zone::temperature"


def test_write_climate_switches_to_manual_first_and_rounds():
    ha = Ha({"climate.zone": "auto"})
    assert plant.write(ha, "shift_current", ZONE, 20.37, 15.0, 25.0) == 20.5
    assert ha.calls == [("hvac", "climate.zone", "heat_cool"), ("climate", "climate.zone", 20.5)]


def test_write_climate_already_manual_only_sets_temperature():
    ha = Ha({"climate.zone": "heat_cool"})
    plant.write(ha, "shift_current", ZONE, 26.0, 15.0, 25.0)
    assert ha.calls == [("climate", "climate.zone", 25.0)]


def test_write_number_rounds_to_role_step():
    ha = Ha({})
    assert plant.write(ha, "curve_current", "number.c", 1.0147, 0.4, 1.5) == 1.0
    assert plant.write(ha, "min_flow", "number.mf", 20.46, 20.0, 30.0) == 20.5
    assert ha.calls == [("number", "number.c", 1.0), ("number", "number.mf", 20.5)]


def test_write_climate_ensure_mode_false_skips_mode_check():
    ha = Ha({"climate.zone": "auto"})
    assert plant.write(ha, "shift_current", ZONE, 20.37, 15.0, 25.0, ensure_mode=False) == 20.5
    assert ha.calls == [("climate", "climate.zone", 20.5)]


def test_read_shift_zone_zero_is_inactive():
    assert plant.read_shift(Ha({ZONE: 0.0}), ZONE) is None
    assert plant.read_shift(Ha({ZONE: 20.5}), ZONE) == 20.5


def test_read_shift_above_max_raises():
    with pytest.raises(ValueError):
        plant.read_shift(Ha({ZONE: 35.5}), ZONE)
    assert plant.read_shift(Ha({ZONE: plant.SHIFT_READ_MAX}), ZONE) == plant.SHIFT_READ_MAX


def test_current_shift_falls_back_when_shift_above_max():
    assert plant.current_shift(Ha({ZONE: 40.0}), ZONE, fallback=21.0) == 21.0


def test_current_shift_falls_back_when_zone_reports_zero():
    assert plant.current_shift(Ha({ZONE: 0.0}), ZONE, fallback=21.0) == 21.0
    assert plant.current_shift(Ha({ZONE: RuntimeError("unavailable")}), ZONE, fallback=21.0) == 21.0
    assert plant.current_shift(Ha({ZONE: 0.0}), ZONE, fallback=None) is None
    assert plant.current_shift(Ha({ZONE: 22.5}), ZONE, fallback=21.0) == 22.5


def test_ensure_manual_mode_ignores_numbers():
    ha = Ha({})
    assert plant.ensure_manual_mode(ha, "number.shift") is False
    assert ha.calls == []


@pytest.mark.parametrize(("ref", "entity"), [(ZONE, "climate.zone"), ("number.x", "number.x")])
def test_entity_of(ref, entity):
    assert plant.entity_of(ref) == entity
