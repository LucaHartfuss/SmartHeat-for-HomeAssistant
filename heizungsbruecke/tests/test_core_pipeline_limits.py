"""Hebel-Pipeline, Plan Client2-Bereitschaft: geschrieben wird in der Schnittmenge aus lokalen Grenzen (safety.py) und
dem Wertebereich der Anlage (binding.limits) -- nur enger, nie weiter."""
import logging

from heizungsbruecke.ha_binding import HaPlantBinding
from smartheat_core.binding import WEISHAUPT_MODBUS
from smartheat_core.pipeline import LeverPipeline
from smartheat_core.safety import resolve_local_safety
from smartheat_runtime.roles import ChannelManifest

MANIFEST = ChannelManifest(refs={"curve_current": "number.hk", "shift_current": "number.normal", "heat_limit": "number.swu"})
SAFETY = resolve_local_safety("weishaupt_wwp", "Heizkoerper")  # room_setpoint 16-25


class Ha:
    def __init__(self):
        self.states = {"number.hk": 0.75, "number.normal": 20.0, "number.swu": 18.0}
        self.writes = []

    def get_state(self, entity_id):
        return self.states[entity_id]

    def get_raw_state(self, entity_id):
        return "on"

    def set_number_value(self, entity_id, value):
        self.states[entity_id] = value
        self.writes.append((entity_id, value))


class LimitedBinding(HaPlantBinding):
    def __init__(self, ha, device):
        super().__init__(ha, MANIFEST, WEISHAUPT_MODBUS)
        self.device = device

    def limits(self, lever):
        value = self.device.get(lever)
        if isinstance(value, Exception):
            raise value
        return value

    def read_aux(self):
        # Wie AuxBinding in test_core_pipeline_originals.py: Weishaupt verlangt die Hilfswerte vor dem ersten Schreiben.
        return {"mode_select": "hz_operationmode_automatic", "setpoint_comfort": 22.0, "setpoint_setback": 18.0}

    def restore_aux(self, values, levers=None):
        return None


def _pipeline(make_store, clock, device):
    ha = Ha()
    pipeline = LeverPipeline(make_store(), LimitedBinding(ha, device), SAFETY, clock=clock)
    clock.advance(WEISHAUPT_MODBUS.settle_seconds + 1)
    return pipeline, ha


def test_the_device_range_narrows_the_written_value(make_store, clock):
    pipeline, ha = _pipeline(make_store, clock, {"room_setpoint": (18.0, 25.0)})
    pipeline.apply_server_values({"room_setpoint": 16.5})
    assert ("number.normal", 18.0) in ha.writes


def test_a_wider_device_range_never_widens_the_local_limits(make_store, clock):
    pipeline, ha = _pipeline(make_store, clock, {"room_setpoint": (5.0, 35.0)})
    pipeline.apply_server_values({"room_setpoint": 30.0})
    assert ("number.normal", 25.0) in ha.writes


def test_without_overlap_the_local_limits_apply(make_store, clock, caplog):
    pipeline, ha = _pipeline(make_store, clock, {"room_setpoint": (26.0, 28.0)})
    with caplog.at_level(logging.WARNING):
        pipeline.apply_server_values({"room_setpoint": 16.5})
    assert ("number.normal", 16.5) in ha.writes
    assert "ausserhalb der lokalen Grenzen" in caplog.text


def test_an_unreadable_device_range_falls_back_to_the_local_limits(make_store, clock):
    pipeline, ha = _pipeline(make_store, clock, {"room_setpoint": RuntimeError("weg")})
    pipeline.apply_server_values({"room_setpoint": 16.5})
    assert ("number.normal", 16.5) in ha.writes


def test_expected_values_and_the_emergency_row_use_the_narrowed_range(make_store, clock):
    pipeline, _ = _pipeline(make_store, clock, {"room_setpoint": (18.0, 24.0)})
    pipeline.apply_server_values({"room_setpoint": 16.5})
    assert pipeline.expected_values()["room_setpoint"] == 18.0
    assert pipeline._row_values("emergency")["room_setpoint"] == 24.0
