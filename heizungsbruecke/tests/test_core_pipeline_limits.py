"""Hebel-Pipeline, Plan Client2-Bereitschaft: geschrieben wird in der Schnittmenge aus lokalen Grenzen (safety.py) und
dem Wertebereich der Anlage (binding.limits) -- nur enger, nie weiter."""
import logging
from types import SimpleNamespace

from heizungsbruecke.ha_binding import HaPlantBinding
from smartheat_core import derived, enforce
from smartheat_core.binding import VAILLANT_MYPYLLANT, WEISHAUPT_MODBUS
from smartheat_core.pipeline import LIMITS_TTL_SECONDS, LeverPipeline
from smartheat_core.safety import LocalSafety, resolve_local_safety
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


class CountingBinding(LimitedBinding):
    """Zaehlt die Abfragen des Anlagenbereichs; `device` kann pro Test umgestellt werden."""

    def __init__(self, ha, device):
        super().__init__(ha, device)
        self.calls = 0

    def limits(self, lever):
        self.calls += 1
        return super().limits(lever)


def _counting(make_store, clock, device):
    ha = Ha()
    binding = CountingBinding(ha, device)
    pipeline = LeverPipeline(make_store(), binding, SAFETY, clock=clock)
    return pipeline, binding


def test_the_device_range_is_cached_for_the_ttl(make_store, clock):
    pipeline, binding = _counting(make_store, clock, {"room_setpoint": (18.0, 24.0)})
    assert pipeline.range_of("room_setpoint") == (18.0, 24.0)
    assert pipeline.range_of("room_setpoint") == (18.0, 24.0)
    assert binding.calls == 1
    clock.advance(LIMITS_TTL_SECONDS - 1)
    pipeline.range_of("room_setpoint")
    assert binding.calls == 1
    binding.device = {"room_setpoint": (19.0, 24.0)}
    clock.advance(2)
    assert pipeline.range_of("room_setpoint") == (19.0, 24.0)
    assert binding.calls == 2


def test_an_unknown_device_range_is_cached_too(make_store, clock):
    pipeline, binding = _counting(make_store, clock, {})
    assert pipeline.range_of("room_setpoint") == SAFETY.ranges["room_setpoint"]
    pipeline.range_of("room_setpoint")
    assert binding.calls == 1


def test_a_failing_read_after_a_good_one_keeps_the_good_range(make_store, clock, caplog):
    pipeline, binding = _counting(make_store, clock, {"room_setpoint": (18.0, 24.0)})
    assert pipeline.range_of("room_setpoint") == (18.0, 24.0)
    binding.device = {"room_setpoint": RuntimeError("ha weg")}
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            clock.advance(LIMITS_TTL_SECONDS + 1)
            assert pipeline.range_of("room_setpoint") == (18.0, 24.0)
    assert caplog.text.count("nicht lesbar") == 1  # einmal je Hebel, kein Traceback
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    binding.device = {"room_setpoint": (17.0, 24.0)}
    clock.advance(LIMITS_TTL_SECONDS + 1)
    assert pipeline.range_of("room_setpoint") == (17.0, 24.0)


def test_a_failing_first_read_uses_the_local_limits_and_is_not_repeated_within_the_ttl(make_store, clock):
    pipeline, binding = _counting(make_store, clock, {"room_setpoint": RuntimeError("ha weg")})
    assert pipeline.range_of("room_setpoint") == SAFETY.ranges["room_setpoint"]
    pipeline.range_of("room_setpoint")
    assert binding.calls == 1


def test_the_no_overlap_warning_is_logged_once_per_device_range(make_store, clock, caplog):
    pipeline, binding = _counting(make_store, clock, {"room_setpoint": (26.0, 28.0)})
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            pipeline.range_of("room_setpoint")
        assert caplog.text.count("ausserhalb der lokalen Grenzen") == 1
        binding.device = {"room_setpoint": (27.0, 29.0)}
        clock.advance(LIMITS_TTL_SECONDS + 1)
        pipeline.range_of("room_setpoint")
    assert caplog.text.count("ausserhalb der lokalen Grenzen") == 2


def test_an_unmapped_optional_lever_has_no_device_range_and_logs_no_error(make_store, clock, caplog):
    ha = Ha()
    manifest = ChannelManifest(refs={"curve_current": "number.hk", "shift_current": "number.normal"})  # ohne heat_limit
    binding = HaPlantBinding(ha, manifest, WEISHAUPT_MODBUS)
    assert binding.limits("heat_limit") is None
    pipeline = LeverPipeline(make_store(), binding, SAFETY, clock=clock)
    with caplog.at_level(logging.DEBUG):
        assert pipeline.range_of("heat_limit") == SAFETY.ranges["heat_limit"]
        assert pipeline._row_values("emergency")
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


# --- Mindestvorlauf (derived) und Durchsetzen folgen demselben verengten Bereich ---

VAILLANT_SAFETY = LocalSafety(
    ranges={"curve": (0.4, 1.5), "room_setpoint": (15.0, 25.0), "heat_limit": (5.0, 20.0), "min_flow": (20.0, 30.0)},
    comfort_boost={}, emergency_boost_levers=(), arrival_threshold_k=0.5,
)
VAILLANT_MANIFEST = ChannelManifest(refs={
    "curve_current": "number.curve", "shift_current": "climate.zone::temperature", "min_flow": "number.mf",
})


class VaillantHa:
    def __init__(self):
        self.states = {"number.curve": 0.9, "climate.zone::temperature": 21.0, "number.mf": 20.0, "climate.zone": "heat_cool"}

    def get_state(self, ref):
        return self.states[ref]

    def get_raw_state(self, entity_id):
        return self.states.get(entity_id, "on")

    def set_number_value(self, entity_id, value):
        self.states[entity_id] = value

    def set_climate_temperature(self, entity_id, value):
        self.states[entity_id] = value


class MinFlowLimited(HaPlantBinding):
    def limits(self, lever):
        return (20.0, 24.0) if lever == "min_flow" else None


def test_min_flow_expected_and_written_use_the_narrowed_range_and_enforce_sees_no_deviation(make_store, clock):
    store = make_store()
    ha = VaillantHa()
    pipeline = LeverPipeline(store, MinFlowLimited(ha, VAILLANT_MANIFEST, VAILLANT_MYPYLLANT), VAILLANT_SAFETY, clock=clock)
    store.update(stable_target=28.0)
    clock.advance(VAILLANT_MYPYLLANT.settle_seconds + 1)
    runtime = SimpleNamespace(store=store, override=pipeline)
    assert derived.expected(runtime) == 24.0
    derived.sync(runtime)
    assert ha.states["number.mf"] == 24.0
    clock.advance(VAILLANT_MYPYLLANT.settle_seconds + 1)
    expected, deviating, pending = enforce._deviations(runtime)
    assert expected["min_flow"] == 24.0
    assert deviating == {} and pending == set()
