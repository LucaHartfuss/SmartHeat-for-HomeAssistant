import time

import pytest
from configs import SENSOR, THERMOSTAT, apply_config, write_runtime_files
from fakes import FakeBus
from world import GatewayWorld

from smartheat_gateway.config import GatewayConfig
from smartheat_gateway.drivers.registry import create
from smartheat_gateway.host import SHG_TEXTS, GatewayHost, build_manifest
from smartheat_gateway.paths import Paths
from smartheat_runtime.app import IDLE_NOT_CONFIGURED, IdleBridge, RestoreParts, StartFailure
from smartheat_runtime.roles import ManifestError
from smartheat_runtime.runtime import Runtime
from smartheat_runtime.runtime_config import BATTERY_LOW_FLAG, BATTERY_PERCENT, BatteryRef


@pytest.fixture
def world(data_dir, monkeypatch, clock):
    return GatewayWorld(data_dir, monkeypatch, clock)


def test_not_configured_is_idle_and_clears_retained_status(world):
    world.bus.publish("shg/status", {"status": "abgemeldet"}, retain=True)
    result = world.start_runtime()
    assert isinstance(result, IdleBridge) and result.reason == IDLE_NOT_CONFIGURED
    assert world.status() is None


def test_configured_start_regulates_with_gateway_texts_and_batteries(world):
    write_runtime_files(world.paths, apply_config())
    result = world.start_runtime()
    assert isinstance(result, Runtime)
    world.connect()
    assert world.status()["status"] == "regelt" and world.status()["setup_id"] == "setup-1"
    assert result.texts is SHG_TEXTS
    assert set(result.config.battery_refs) == {
        BatteryRef(f"zigbee:{SENSOR}:battery", BATTERY_PERCENT),
        BatteryRef(f"zigbee:{THERMOSTAT}:battery_low", BATTERY_LOW_FLAG),
    }
    assert result.manifest.refs["room_actual"] == "raum:mittel"
    assert world.bus.retained["shg/raum"]["soll"] == 20.0


def test_invalid_config_is_a_config_error_without_secrets(world):
    write_runtime_files(world.paths, apply_config(room_sensors=["test-geheim"], mqtt_password="test-geheim"))
    result = world.start_runtime()
    assert isinstance(result, IdleBridge) and result.reason == "konfigurationsfehler"
    assert "test-geheim" not in str(world.status())
    assert "test-geheim" not in str(world.bus.decoded())


def test_start_failures_carry_a_stable_key_independent_of_the_text(world):
    write_runtime_files(world.paths, apply_config(room_sensors=["zigbee:0xzz"]))
    host = GatewayHost(world.paths, world.bus, clock=world.clock, driver_threads=False)
    with pytest.raises(StartFailure) as config_error:
        host.load()
    assert config_error.value.key == "konfiguration" and config_error.value.grund

    unknown = {"id": "simulation", "parameter": {"lever_set": "gibt-es-nicht"}}
    write_runtime_files(world.paths, apply_config(driver=unknown))
    with pytest.raises(StartFailure) as driver_error:
        GatewayHost(world.paths, world.bus, clock=world.clock, driver_threads=False).load()
    assert driver_error.value.key == "treiber"


def test_driver_lever_set_must_match_the_configured_lever_set(world):
    write_runtime_files(world.paths, apply_config(lever_set="vaillant_vrc720"))  # Treiber simuliert viessmann_vicare
    result = world.start_runtime()
    assert isinstance(result, IdleBridge) and result.reason == "konfigurationsfehler"
    assert "Hebelsatz" in world.status()["grund"]
    assert world.host.driver is None  # nie gestartet, nie abgefragt


def test_sign_off_without_matching_lever_set_does_not_restore(world):
    write_runtime_files(world.paths, apply_config(lever_set="vaillant_vrc720", abgemeldet=True))
    host = GatewayHost(world.paths, world.bus, clock=world.clock, driver_threads=False)
    assert host.sign_off_parts() is None
    assert host.driver is None


def test_sign_off_with_a_valid_signed_off_config_restores_with_the_driver(world):
    write_runtime_files(world.paths, apply_config(abgemeldet=True))  # Treiber und Hebelsatz passen (viessmann_vicare)
    host = GatewayHost(world.paths, world.bus, clock=world.clock, driver_threads=False)
    parts = host.sign_off_parts()
    assert isinstance(parts, RestoreParts)
    assert host.driver is not None and parts.binding is host.driver


def test_manifest_error_is_a_start_failure_with_the_manifest_key(world, monkeypatch):
    write_runtime_files(world.paths, apply_config())
    host = GatewayHost(world.paths, world.bus, clock=world.clock, driver_threads=False)
    create_driver = host._create_driver

    def partial_driver(spec):
        driver = create_driver(spec)
        driver.signals = lambda: {"outdoor_temp": "treiber:outdoor_temp"}  # Pflicht-Rollen des Hebelsatzes fehlen
        return driver

    monkeypatch.setattr(host, "_create_driver", partial_driver)
    with pytest.raises(StartFailure) as manifest_error:
        host.load()
    assert manifest_error.value.key == "manifest" and "Pflicht-Rollen" in manifest_error.value.grund
    assert host.driver is None  # nie aktiviert, nie abgefragt


def test_waiting_for_devices_is_bounded_by_the_real_clock_not_the_injected_one(data_dir, clock):
    write_runtime_files(Paths(data_dir), apply_config())
    host = GatewayHost(Paths(data_dir), FakeBus(), clock=clock, devices_wait_seconds=0.3, driver_threads=False)
    started = time.monotonic()
    loaded = host.load()  # leeres bridge/devices, die injizierte Uhr steht still
    assert loaded.config.battery_refs == ()
    assert 0.25 <= time.monotonic() - started < 5


def test_manifest_requires_the_roles_of_the_lever_set(data_dir, clock):
    driver = create("simulation", {"lever_set": "viessmann_vicare"}, Paths(data_dir), clock=clock, writer=True)
    gateway = GatewayConfig("simulation", {}, (f"zigbee:{SENSOR}:temperature",), None, 20.0, None)
    assert build_manifest(driver, gateway).refs["level_current"] == "treiber:level_current"

    class Partial:
        description = driver.description

        def signals(self):
            return {"outdoor_temp": "treiber:outdoor_temp"}

    with pytest.raises(ManifestError):
        build_manifest(Partial(), gateway)


def test_gateway_texts_never_name_home_assistant():
    for value in vars(SHG_TEXTS).values():
        assert "Home Assistant" not in value and "Heizungsbrücke" not in value and "Integration" not in value
