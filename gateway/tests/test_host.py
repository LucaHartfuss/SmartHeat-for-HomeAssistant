"""Laufzeit-Host des Gateways ab 0.6.0 (Spec SHG G2 3.1, Spec 5b Plan D2 Task 5): Start aus dem Konfigurationsdokument,
Startfehler mit stabilem Schluessel, Hebelsatz, Abmelden, Manifest nach der ersten Abfrage, Batterien, Anlagen-Kennung."""
import time

import pytest
from configs import SENSOR, THERMOSTAT, dokumente_schreiben, konfiguration
from fake_vicare.server import GOOD_CODE, FakeVicare
from fakes import FakeBus
from geraet_fakes import geraet_fuer
from world import GatewayWorld

from smartheat_core import rastung
from smartheat_gateway import config as gateway_config
from smartheat_gateway.drivers import registry
from smartheat_gateway.drivers.base import DriverError
from smartheat_gateway.drivers.registry import create
from smartheat_gateway.drivers.vicare_cloud.driver import ViCareCloudDriver
from smartheat_gateway.host import SHG_TEXTS, GatewayHost, build_manifest
from smartheat_gateway.paths import Paths
from smartheat_gateway.zigbee import ZigbeeMirror
from smartheat_runtime import aufraeumen
from smartheat_runtime.app import IDLE_NOT_CONFIGURED, IdleBridge, RestoreParts, StartFailure
from smartheat_runtime.roles import ManifestError
from smartheat_runtime.runtime import Runtime
from smartheat_runtime.runtime_config import BATTERY_LOW_FLAG, BATTERY_PERCENT, BatteryRef

VICARE = {"id": "vicare_cloud", "parameter": {"installation_id": 2012345, "gateway_serial": "7637415000000001",
                                               "device_id": "0", "heizkreis": 0, "poll_seconds": 300.0}}


@pytest.fixture
def world(data_dir, monkeypatch, clock):
    world = GatewayWorld(data_dir, monkeypatch, clock)
    yield world
    world.close()


def _host(paths, konf, clock, *, bus=None, wait=0.1):
    bus = bus or FakeBus()
    mirror = ZigbeeMirror(bus, clock)
    mirror.start()
    return GatewayHost(paths, bus, mirror, geraet_fuer(paths, mirror, konf), clock=clock, devices_wait_seconds=wait,
                       driver_threads=False)


def test_not_configured_is_idle(world):
    result = world.start_runtime()
    assert isinstance(result, IdleBridge) and result.reason == IDLE_NOT_CONFIGURED and world.status() is None


def test_configured_start_regulates_with_gateway_texts_and_batteries(world):
    dokumente_schreiben(world.paths, konfiguration())
    result = world.start_runtime()
    assert isinstance(result, Runtime)
    world.connect()
    assert world.status()["status"] == "regelt" and world.status()["setup_id"] == "test-tenant"
    assert result.texts is SHG_TEXTS
    assert set(result.config.battery_refs) == {
        BatteryRef(f"zigbee:{SENSOR}:battery", BATTERY_PERCENT),
        BatteryRef(f"zigbee:{THERMOSTAT}:battery_low", BATTERY_LOW_FLAG),
    }
    assert result.manifest.refs["room_actual"] == "raum:mittel"
    assert world.geraet.zustand.raum["soll"] == 20.0


def test_invalid_room_sensor_is_a_config_error(world):
    dokumente_schreiben(world.paths, konfiguration(raumfuehler=["test-falsch"]))
    result = world.start_runtime()
    assert isinstance(result, IdleBridge) and result.reason == "konfigurationsfehler"


def test_start_failures_carry_a_stable_key_independent_of_the_text(data_dir, clock):
    paths = Paths(data_dir)
    with pytest.raises(StartFailure) as config_error:
        _host(paths, konfiguration(raumfuehler=["zigbee:0xzz"]), clock).load()
    assert config_error.value.key == "konfiguration" and config_error.value.grund
    unknown = {"treiber": {"id": "simulation", "parameter": {"lever_set": "gibt-es-nicht"}}, "thermostat": None}
    with pytest.raises(StartFailure) as driver_error:
        _host(paths, konfiguration(bindung=unknown), clock).load()
    assert driver_error.value.key == "treiber"


def test_driver_lever_set_must_match_the_configured_lever_set(world):
    dokumente_schreiben(world.paths, konfiguration(hebelsatz="vaillant_vrc720"))  # Treiber simuliert viessmann_vicare
    result = world.start_runtime()
    assert isinstance(result, IdleBridge) and result.reason == "konfigurationsfehler"
    assert "Hebelsatz" in world.status()["grund"]
    assert world.host.driver is None


def test_sign_off_without_matching_lever_set_does_not_restore(data_dir, clock):
    paths = Paths(data_dir)
    gateway_config.abmelden(paths, "test-tenant")
    host = _host(paths, konfiguration(hebelsatz="vaillant_vrc720"), clock)
    assert host.boot_info().signed_off and host.sign_off_parts() is None and host.driver is None


def test_sign_off_with_a_matching_plant_restores_with_the_driver(data_dir, clock):
    paths = Paths(data_dir)
    gateway_config.abmelden(paths, "test-tenant")
    host = _host(paths, konfiguration(), clock)
    parts = host.sign_off_parts()
    assert isinstance(parts, RestoreParts) and host.driver is not None and parts.binding is host.driver


def test_empty_configuration_with_pending_reset_boots_signed_off_with_the_old_one(data_dir, clock):
    """Spec Stufe 3 S3 Abschnitt 4: die Laufzeit setzt mit der Konfiguration der Aufraeum-Markierung zurueck."""
    paths = Paths(data_dir)
    aufraeumen.markieren(paths.aufraeumen, konfiguration())
    host = _host(paths, {**konfiguration(), "anlage": None}, clock)
    boot = host.boot_info()
    assert boot.signed_off and not boot.configured and boot.tenant_id == "test-tenant"
    assert boot.aufraeumen_path == paths.aufraeumen
    assert isinstance(host.sign_off_parts(), RestoreParts) and host.driver is not None


def test_load_refuses_a_configuration_that_does_not_fit_the_lock(data_dir, clock):
    paths = Paths(data_dir)
    rastung.schreiben(paths.rastung, rastung.Rastung("viessmann_vicare", "Fussbodenheizung"), "2026-10-14T00:00:00+00:00")
    with pytest.raises(StartFailure, match="festgelegt") as error:
        _host(paths, konfiguration(), clock).load()
    assert error.value.key == "konfiguration"


def test_sign_off_parts_are_none_with_a_broken_lock(data_dir, clock):
    paths = Paths(data_dir)
    gateway_config.abmelden(paths, "test-tenant")
    paths.rastung.parent.mkdir(parents=True, exist_ok=True)
    paths.rastung.write_text("{")
    host = _host(paths, konfiguration(), clock)
    assert host.sign_off_parts() is None and host.driver is None


def test_manifest_error_is_a_start_failure_with_the_manifest_key(data_dir, clock, monkeypatch):
    host = _host(Paths(data_dir), konfiguration(), clock)
    create_driver = host._create_driver
    created = []

    def partial_driver(spec):
        driver = create_driver(spec)
        created.append(driver)
        driver.signals = lambda: {"outdoor_temp": "treiber:outdoor_temp"}  # Pflicht-Rollen des Hebelsatzes fehlen
        return driver

    monkeypatch.setattr(host, "_create_driver", partial_driver)
    with pytest.raises(StartFailure) as manifest_error:
        host.load()
    assert manifest_error.value.key == "manifest" and "Pflicht-Rollen" in manifest_error.value.grund
    assert host.driver is None and created[0]._stop.is_set()


def test_manifest_is_built_after_the_first_poll_so_optional_signals_are_in_it(data_dir, clock, monkeypatch):
    server = FakeVicare()
    base = server.start()
    try:
        for key, value in {"SHG_TEST_ENDPOINTS": "1", "SHG_VICARE_IAM_BASE": base, "SHG_VICARE_API_BASE": base}.items():
            monkeypatch.setenv(key, value)
        paths = Paths(data_dir)
        login = ViCareCloudDriver(VICARE["parameter"], paths, clock=clock)
        login.login_begin({"client_id": "c", "redirect_uri": "r"})
        login.login_finish({"code": GOOD_CODE, "redirect_uri": "r"})
        host = _host(paths, konfiguration(bindung={"treiber": VICARE, "thermostat": THERMOSTAT}), clock)
        loaded = host.load()
        assert loaded.manifest.refs["flow_temperature"] == "treiber:flow_temperature"
        assert host.driver is loaded.binding
    finally:
        server.stop()


def test_a_failing_first_poll_still_loads_and_the_runtime_reports_the_data_error(data_dir, clock, monkeypatch):
    monkeypatch.setenv("SHG_TEST_ENDPOINTS", "1")
    monkeypatch.setenv("SHG_VICARE_IAM_BASE", "http://127.0.0.1:9")
    monkeypatch.setenv("SHG_VICARE_API_BASE", "http://127.0.0.1:9")
    host = _host(Paths(data_dir), konfiguration(bindung={"treiber": VICARE, "thermostat": THERMOSTAT}), clock)
    loaded = host.load()
    assert "flow_temperature" not in loaded.manifest.refs and "curve_current" in loaded.manifest.refs
    with pytest.raises((ValueError, DriverError)):
        loaded.binding.read("curve")


def test_waiting_for_devices_is_bounded_by_the_real_clock_not_the_injected_one(data_dir, clock):
    host = _host(Paths(data_dir), konfiguration(), clock, wait=0.3)
    started = time.monotonic()
    loaded = host.load()  # leeres bridge/devices, die injizierte Uhr steht still
    assert loaded.config.battery_refs == ()
    assert 0.25 <= time.monotonic() - started < 5


def test_load_reports_the_plant_identity_of_the_driver(data_dir, clock):
    loaded = _host(Paths(data_dir), konfiguration(), clock).load()
    assert loaded.plant_id == registry.plant_id(
        {"id": "simulation", "parameter": {"lever_set": "viessmann_vicare", "heizkreis": 0}})


def test_same_plant_compares_driver_and_identity_parameters():
    base = {"id": "simulation", "parameter": {"lever_set": "viessmann_vicare", "heizkreis": 0, "poll_seconds": 60}}
    assert registry.same_plant(base, {**base, "parameter": {**base["parameter"], "poll_seconds": 300}})
    assert not registry.same_plant(base, {**base, "parameter": {**base["parameter"], "heizkreis": 1}})
    assert not registry.same_plant(base, {"id": "vicare_cloud", "parameter": base["parameter"]})
    assert not registry.same_plant(None, base)


def test_manifest_requires_the_roles_of_the_lever_set(data_dir, clock):
    driver = create("simulation", {"lever_set": "viessmann_vicare"}, Paths(data_dir), clock=clock, writer=True)
    assert build_manifest(driver).refs["level_current"] == "treiber:level_current"

    class Partial:
        description = driver.description

        def signals(self):
            return {"outdoor_temp": "treiber:outdoor_temp"}

    with pytest.raises(ManifestError):
        build_manifest(Partial())


def test_abo_comes_from_the_document(data_dir, clock):
    loaded = _host(Paths(data_dir), konfiguration(abo="inaktiv"), clock).load()
    assert loaded.config.abo_source() == "inactive"


def test_gateway_texts_never_name_home_assistant():
    for value in vars(SHG_TEXTS).values():
        assert "Home Assistant" not in value and "Heizungsbrücke" not in value and "Integration" not in value


def test_ursprungswerte_server_reads_the_current_configuration_fresh(data_dir, clock):
    """Spec Stufe 3 S3 5.2: frisch aus dem Dokument; trifft ein, ohne dass die Laufzeit neu startet."""
    host = _host(Paths(data_dir), {**konfiguration(), "ursprungswerte": {"heat_limit": 16.0}}, clock)
    assert host.ursprungswerte_server() == {"heat_limit": 16.0}
    host._geraet.device.konf = {**konfiguration(), "ursprungswerte": {"heat_limit": 15.0}}
    assert host.ursprungswerte_server() == {"heat_limit": 15.0}


def test_ursprungswerte_server_falls_back_to_the_marker_while_the_reset_is_pending(data_dir, clock):
    paths = Paths(data_dir)
    alt = {**konfiguration(), "ursprungswerte": {"heat_limit": 16.0}}
    aufraeumen.markieren(paths.aufraeumen, alt)
    host = _host(paths, {**alt, "anlage": None, "ursprungswerte": {}}, clock)
    assert host.ursprungswerte_server() == {"heat_limit": 16.0}
