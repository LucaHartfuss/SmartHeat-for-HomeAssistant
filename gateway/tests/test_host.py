"""Laufzeit-Host des Gateways ab 0.6.0 (Spec SHG G2 3.1, Spec 5b Plan D2 Task 5): Start aus dem Konfigurationsdokument,
Startfehler mit stabilem Schluessel, Hebelsatz, Abmelden, Manifest nach der ersten Abfrage, Batterien, Anlagen-Kennung."""
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace

import bindung_beispiele
import pytest
from bindung_beispiele import VIESSMANN_VICARE_BINDING
from configs import SENSOR, THERMOSTAT, bedienung, dokumente_schreiben, konfiguration
from fake_vicare.server import GOOD_CODE, FakeVicare
from fakes import FakeBus
from geraet_fakes import geraet_fuer
from world import GatewayWorld

from smartheat_core import dateien, rastung
from smartheat_gateway import config as gateway_config
from smartheat_gateway import host as host_modul
from smartheat_gateway.drivers import registry
from smartheat_gateway.drivers.base import DriverError
from smartheat_gateway.drivers.registry import create
from smartheat_gateway.drivers.vicare_cloud.driver import ViCareCloudDriver
from smartheat_gateway.host import SHG_TEXTS, GatewayHost, build_manifest
from smartheat_gateway.paths import Paths
from smartheat_gateway.zigbee import ZigbeeMirror
from smartheat_runtime import aufraeumen
from smartheat_runtime.app import IDLE_NOT_CONFIGURED, IdleBridge, RestoreParts, StartFailure
from smartheat_runtime.roles import ManifestError, lever_refs_identity
from smartheat_runtime.runtime import Runtime
from smartheat_runtime.runtime_config import BATTERY_LOW_FLAG, BATTERY_PERCENT, BatteryRef
from smartheat_runtime.state import StateStore, bind_to_tenant

VICARE = {"id": "vicare_cloud", "parameter": {"installation_id": 2012345, "gateway_serial": "7637415000000001",
                                               "device_id": "0", "heizkreis": 0, "poll_seconds": 300.0}}


@pytest.fixture
def world(data_dir, monkeypatch, clock):
    world = GatewayWorld(data_dir, monkeypatch, clock)
    yield world
    world.close()


def _host(paths, konf, clock, *, bus=None, wait=0.1, start_markierung=None):
    bus = bus or FakeBus()
    mirror = ZigbeeMirror(bus, clock)
    mirror.start()
    return GatewayHost(paths, bus, mirror, geraet_fuer(paths, mirror, konf, start_markierung=start_markierung),
                       clock=clock, devices_wait_seconds=wait, driver_threads=False)


def _offene_markierung(paths, alt):
    """Wie der Start: die Markierung nach einer leeren Konfiguration schreiben und einmal lesen."""
    aufraeumen.markieren(paths.aufraeumen, alt)
    return aufraeumen.entscheiden(paths.aufraeumen, {"anlage": None})[1]


def test_not_configured_is_idle(world):
    result = world.start_runtime()
    assert isinstance(result, IdleBridge) and result.reason == IDLE_NOT_CONFIGURED and world.status() is None


def test_configured_start_regulates_with_gateway_texts_and_batteries(world):
    dokumente_schreiben(world.paths, konfiguration())
    result = world.start_runtime()
    assert isinstance(result, Runtime)
    world.connect()
    assert world.status()["status"] == "regelt" and world.status()["tenant_id"] == "test-tenant"
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


def test_load_refuses_missing_binding_parameters_with_the_config_key(data_dir, clock):
    host = _host(Paths(data_dir), konfiguration(bindungsparameter=None), clock)
    with pytest.raises(StartFailure, match="Bindungsparameter fehlen") as error:
        host.load()
    assert error.value.key == "konfiguration" and error.value.grund.startswith("Konfiguration: ")
    assert host.driver is None


def test_sign_off_parts_are_none_without_binding_parameters(data_dir, clock, caplog):
    paths = Paths(data_dir)
    gateway_config.abmelden(paths, "test-tenant")
    host = _host(paths, konfiguration(bindungsparameter=None), clock)
    assert host.sign_off_parts() is None and host.driver is None
    assert "Abmelden ohne Zuruecksetzen: Die Bindungsparameter fehlen." in caplog.text


def test_the_driver_gets_the_description_from_the_configuration(data_dir, clock):
    parameter = bindung_beispiele.parameter("viessmann_vicare", wartezeit_s=240)
    loaded = _host(Paths(data_dir), konfiguration(bindungsparameter=parameter), clock).load()
    assert loaded.binding.description.settle_seconds == 240.0


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
    host = _host(paths, {**konfiguration(), "anlage": None}, clock,
                 start_markierung=_offene_markierung(paths, konfiguration()))
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

    def partial_driver(spec, description):
        driver = create_driver(spec, description)
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


def _erzeugte_treiber(monkeypatch):
    erzeugt = []
    echt = registry.create

    def zaehlend(*args, **kwargs):
        erzeugt.append(args[0])
        return echt(*args, **kwargs)

    monkeypatch.setattr(registry, "create", zaehlend)
    return erzeugt


def _host_mit_z2m(data_dir, clock, konf, *, wait=0.1):
    from fake_z2m import FakeZigbee2Mqtt

    bus = FakeBus()
    host = _host(Paths(data_dir), konf, clock, bus=bus, wait=wait)
    z2m = FakeZigbee2Mqtt(bus)
    z2m.add_sensor(SENSOR)
    z2m.add_thermostat(THERMOSTAT)
    return host, z2m


def test_a_sensor_as_the_bound_thermostat_stops_the_start_before_any_driver_exists(data_dir, clock, monkeypatch, caplog):
    erzeugt = _erzeugte_treiber(monkeypatch)
    konf = konfiguration(bindung={**konfiguration()["bindung"], "thermostat": SENSOR})
    host, _ = _host_mit_z2m(data_dir, clock, konf)
    with pytest.raises(StartFailure) as fehler:
        host.load()
    assert fehler.value.key == "konfiguration" and "Thermostat" in fehler.value.grund
    assert SENSOR not in fehler.value.grund and SENSOR not in caplog.text
    assert erzeugt == [] and host.driver is None


def test_the_counter_probe_starts_with_a_real_thermostat_and_creates_one_driver(data_dir, clock, monkeypatch):
    erzeugt = _erzeugte_treiber(monkeypatch)
    host, _ = _host_mit_z2m(data_dir, clock, konfiguration())
    loaded = host.load()
    assert erzeugt == ["simulation"] and host.driver is loaded.binding
    assert {ref.ref for ref in loaded.config.battery_refs} == {
        f"zigbee:{SENSOR}:battery", f"zigbee:{THERMOSTAT}:battery_low"}


@pytest.mark.parametrize("ohne", ["unbekannt", "ohne_definition"])
def test_a_thermostat_the_mirror_does_not_know_as_a_defined_device_starts_as_before(data_dir, clock, monkeypatch, ohne):
    erzeugt = _erzeugte_treiber(monkeypatch)
    fern = "0x00124b0000000077"
    konf = konfiguration(bindung={**konfiguration()["bindung"], "thermostat": fern})
    host, z2m = _host_mit_z2m(data_dir, clock, konf)
    if ohne == "ohne_definition":
        z2m.add_ohne_definition(fern)
    host.load()
    assert erzeugt == ["simulation"]


def test_a_start_without_a_bound_thermostat_does_not_check_one(data_dir, clock, monkeypatch):
    erzeugt = _erzeugte_treiber(monkeypatch)
    konf = konfiguration(bindung={**konfiguration()["bindung"], "thermostat": None})
    host, _ = _host_mit_z2m(data_dir, clock, konf)
    host.load()
    assert erzeugt == ["simulation"]


def _fake_uhr(monkeypatch):
    """Ersetzt `time` im Host durch eine Uhr, die nur `sleep` vorstellt: die Pausen stehen in der Liste, die Wartezeit
    laeuft nicht in Wanduhrzeit."""
    jetzt = [0.0]
    pausen = []

    def sleep(sekunden):
        pausen.append(sekunden)
        jetzt[0] += sekunden

    monkeypatch.setattr(host_modul, "time", SimpleNamespace(monotonic=lambda: jetzt[0], sleep=sleep))
    return pausen


def test_the_device_list_is_awaited_once_for_the_thermostat_check_and_the_batteries(data_dir, clock, monkeypatch):
    pausen = _fake_uhr(monkeypatch)
    host = _host(Paths(data_dir), konfiguration(), clock, wait=0.3)
    host.load()  # leeres bridge/devices: eine Wartezeit (0.3 s in Pausen zu 0.2 s), nicht zwei
    assert pausen == [host_modul.BATTERY_POLL_SECONDS] * 2 == [0.2, 0.2]


def test_load_reports_the_plant_identity_of_the_driver(data_dir, clock):
    loaded = _host(Paths(data_dir), konfiguration(), clock).load()
    assert loaded.plant_id == registry.plant_id(
        {"id": "simulation", "parameter": {"lever_set": "viessmann_vicare", "heizkreis": 0}})


def test_die_uebernahme_der_frueheren_ha_kennung_greift_beim_gateway_nie(data_dir, clock):
    """A6-36: app.start reicht frueher=lever_refs_identity(manifest) auch beim Gateway weiter. Seine Kennung traegt den
    Schluessel "id", den kein Hebel hat: frueher ist nie gleich, ein Bestand einer anderen Anlage wird weiter
    verworfen."""
    loaded = _host(Paths(data_dir), konfiguration(), clock).load()
    frueher = lever_refs_identity(loaded.manifest)
    assert frueher != loaded.plant_id and "id" in json.loads(loaded.plant_id) and "id" not in json.loads(frueher)
    store = StateStore(Path(data_dir) / "backup.json", Path(data_dir) / "failsafe_state.json")
    store.update(tenant_id="t1", plant_id=registry.plant_id({"id": "simulation", "parameter": {"heizkreis": 1}}),
                 originals={"curve": 1.4})
    assert bind_to_tenant(store, "t1", loaded.plant_id, frueher=frueher) == "andere_anlage"
    assert store.state.originals == {}


def test_manifest_requires_the_roles_of_the_lever_set(data_dir, clock):
    driver = create("simulation", {"lever_set": "viessmann_vicare"}, Paths(data_dir), clock=clock, writer=True,
                    description=VIESSMANN_VICARE_BINDING)
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
    host = _host(paths, {**alt, "anlage": None, "ursprungswerte": {}}, clock,
                 start_markierung=_offene_markierung(paths, alt))
    assert host.ursprungswerte_server() == {"heat_limit": 16.0}


def test_ursprungswerte_server_ignores_a_new_setup_while_the_reset_is_pending(data_dir, clock):
    paths = Paths(data_dir)
    alt = {**konfiguration(), "ursprungswerte": {"heat_limit": 16.0}}
    host = _host(paths, {**alt, "anlage": None, "ursprungswerte": {}}, clock,
                 start_markierung=_offene_markierung(paths, alt))
    host._geraet.device.konf = {**alt, "anlage": "andere-anlage", "ursprungswerte": {"heat_limit": 12.0}}
    assert host.ursprungswerte_server() == {"heat_limit": 16.0}


class _SpaeterBus(FakeBus):
    """Der lokale Bus ist erst nach `fehlversuche` Wartefristen da (Mosquitto startet noch)."""

    def __init__(self, fehlversuche: int, runtime_dir=None) -> None:
        super().__init__()
        self.fehlversuche = fehlversuche
        self.versuche = 0
        self.runtime_dir = runtime_dir
        self.ordner_beim_warten: list[bool] = []  # stand der Laufzeitordner schon, als der Host auf den Bus wartete?

    def wait_connected(self, timeout=None) -> bool:
        self.versuche += 1
        if self.runtime_dir is not None:
            self.ordner_beim_warten.append(self.runtime_dir.exists())
        return self.versuche > self.fehlversuche


def test_wait_until_ready_waits_for_the_bus_and_logs_each_wait(data_dir, clock, caplog):
    caplog.set_level(logging.INFO, logger="smartheat_gateway.host")
    paths = Paths(data_dir)
    bus = _SpaeterBus(fehlversuche=2, runtime_dir=paths.runtime_dir)
    host = _host(paths, konfiguration(), clock, bus=bus)
    assert not paths.runtime_dir.exists()
    host.wait_until_ready()
    assert bus.versuche == 3  # zwei Fristen ohne Bus, die dritte mit
    assert bus.ordner_beim_warten == [False, False, False]  # bei jeder Frist fehlte der Ordner noch
    assert caplog.text.count("Lokaler Bus noch nicht erreichbar, warte") == 2
    assert paths.runtime_dir.is_dir()  # erst nach dem Bus angelegt


def test_wait_until_ready_with_the_bus_already_there_logs_nothing_and_prepares_the_runtime_folder(data_dir, clock, caplog):
    caplog.set_level(logging.INFO, logger="smartheat_gateway.host")
    paths = Paths(data_dir)
    host = _host(paths, konfiguration(), clock, bus=_SpaeterBus(fehlversuche=0))
    host.wait_until_ready()
    assert "noch nicht erreichbar" not in caplog.text and paths.runtime_dir.is_dir()


def _thermostat_writes(world):
    return [body["occupied_heating_setpoint"] for topic, body in world.bus.decoded()
            if topic == f"zigbee2mqtt/{THERMOSTAT}/set"]


def _thermostat_an(world, **zustand):
    """Das Thermostat meldet vor dem Start (retained wie Zigbee2MQTT): sein letzter bekannter Zustand."""
    world.z2m.report(THERMOSTAT, **{"system_mode": "heat", "occupied_heating_setpoint": 20.0,
                                    "local_temperature": 20.0, **zustand})


@pytest.mark.parametrize("gespeichert", [20.0, 22.5], ids=["Bedienung beim Start", "Angleichen beim Start"])
@pytest.mark.parametrize("zustand", [{"system_mode": "off"}, {"occupied_heating_setpoint": 5.0}],
                         ids=["aus", "Frostschutz"])
def test_the_start_writes_nothing_to_a_thermostat_whose_last_known_state_is_off(world, gespeichert, zustand):
    """A6-14 (FH-3): Bedienung 22,5 ohne offene lokale Aenderung; das Thermostat hat vor dem Neustart aus bzw. die
    Frostschutz-Stellung gemeldet. Weder die Bedienung des Servers beim Start noch das Angleichen erreicht es; die
    Bedienung gilt im Speicher."""
    _thermostat_an(world, **zustand)
    dateien.schreiben_json(world.paths.room_target, {"value": gespeichert, "source": "portal", "ts": "t"},
                           modus=dateien.LESBAR)
    dokumente_schreiben(world.paths, konfiguration())
    dateien.schreiben_json(world.paths.device_dir / "bedienung.json",
                           {"version": 1, "inhalt": bedienung(22.5), "dirty": False}, modus=dateien.PRIVAT)
    world.start_runtime()
    world.run()
    assert world.host._store.value == 22.5 and _thermostat_writes(world) == []


@pytest.mark.parametrize(("dirty", "soll", "schreibt"), [(False, 22.5, [22.5]), (True, 20.0, [])],
                         ids=["Bedienung gilt und gleicht das Thermostat an", "offene lokale Aenderung gilt vor"])
def test_an_open_local_change_keeps_the_start_from_aligning_the_thermostat(world, dirty, soll, schreibt):
    """Die Bedienung des Servers (22,5) weicht vom gespeicherten Soll (20,0) ab. Ohne offene lokale Aenderung gilt sie
    und das Thermostat wird beim Start angeglichen; mit offener Aenderung (dirty) bleibt das lokale Soll, nichts geht
    ans Thermostat. Das Thermostat hat vor dem Start gemeldet (bekannter Zustand, an; A6-14)."""
    _thermostat_an(world)
    dateien.schreiben_json(world.paths.room_target, {"value": 20.0, "source": "portal", "ts": "t"}, modus=dateien.LESBAR)
    dokumente_schreiben(world.paths, konfiguration())
    dateien.schreiben_json(world.paths.device_dir / "bedienung.json",
                           {"version": 1, "inhalt": bedienung(22.5), "dirty": dirty}, modus=dateien.PRIVAT)
    world.start_runtime()
    world.run()
    assert world.host._store.value == soll and _thermostat_writes(world) == schreibt


@pytest.mark.parametrize("wert", [float("nan"), float("inf"), float("-inf"), 1e308], ids=["nan", "inf", "-inf", "1e308"])
def test_a_sensor_value_that_is_not_plausible_never_becomes_the_room_temperature(world, wert):
    """F-gateway-host-26: die Signalquelle reicht den Zigbee-Wert unveraendert weiter (wie der HA-Host `float(state)`);
    die gemeinsame Laufzeit (room_mean) zaehlt nur plausible endliche Werte. Der Raum im Status zeigt dann keinen Ist-Wert,
    und sobald wieder ein gueltiger Wert kommt, den gueltigen."""
    dokumente_schreiben(world.paths, konfiguration())
    world.start_runtime()
    world.connect()
    world.advance(30)
    assert world.geraet.zustand.raum["ist"] == 20.0  # vorher anders
    world.z2m.report(SENSOR, temperature=wert)
    world.advance(30)
    assert world.geraet.zustand.raum["ist"] is None
    world.z2m.report(SENSOR, temperature=21.0)
    world.advance(30)
    assert world.geraet.zustand.raum["ist"] == 21.0


@pytest.mark.parametrize(("dirty", "schreibt"), [(False, [22.5]), (True, [])],
                         ids=["Thermostat wird angeglichen", "offene lokale Aenderung: nichts ans Thermostat"])
def test_the_operation_equal_to_the_stored_target_aligns_the_thermostat_only_without_an_open_local_change(
        world, dirty, schreibt):
    """Der zweite Pfad des Angleichens: die Bedienung des Servers entspricht schon dem gespeicherten Soll (kein
    Portal-Ereignis), das Thermostat bekommt es direkt, ausser eine lokale Aenderung ist offen. Bekannter Zustand: an."""
    _thermostat_an(world)
    dateien.schreiben_json(world.paths.room_target, {"value": 22.5, "source": "portal", "ts": "t"}, modus=dateien.LESBAR)
    dokumente_schreiben(world.paths, konfiguration())
    dateien.schreiben_json(world.paths.device_dir / "bedienung.json",
                           {"version": 1, "inhalt": bedienung(22.5), "dirty": dirty}, modus=dateien.PRIVAT)
    world.start_runtime()
    world.run()
    assert world.host._store.value == 22.5 and _thermostat_writes(world) == schreibt


def test_an_operation_at_start_reaches_a_bound_w100_only_after_it_reports_on(world):
    """Fix-Runde 1 (I-1, L4-30): gespeichert 20, Bedienung 22,5; das W100 hat retained thermostat_mode OFF gemeldet. Die
    Bedienung gilt sofort im Speicher, ans Geraet geht sie erst nach der Meldung ON, genau einmal gerundet, ohne heat."""
    w100 = "0x54ef44100140eb34"  # Testwert
    world.z2m.add_w100(w100)
    world.z2m.report(w100, thermostat_mode="OFF", system_mode="heat", occupied_heating_setpoint=20,
                     local_temperature=20.0)
    dateien.schreiben_json(world.paths.room_target, {"value": 20.0, "source": "portal", "ts": "t"}, modus=dateien.LESBAR)
    bindung = {**konfiguration()["bindung"], "thermostat": w100}
    dokumente_schreiben(world.paths, konfiguration(bindung=bindung))
    dateien.schreiben_json(world.paths.device_dir / "bedienung.json",
                           {"version": 1, "inhalt": bedienung(22.5), "dirty": False}, modus=dateien.PRIVAT)
    world.start_runtime()
    world.run()
    modus = [{"thermostat_mode": "ON"}, {"auto_hide_middle_line": False}]
    assert world.host._store.value == 22.5
    assert [body for ieee, body in world.z2m.set_requests if ieee == w100] == modus
    world.z2m.report(w100, thermostat_mode="ON")
    world.run()
    assert [body for ieee, body in world.z2m.set_requests if ieee == w100] == modus + [{"occupied_heating_setpoint": 23.0}]
    assert world.z2m.verworfen == []


def test_the_start_puts_a_bound_w100_into_thermostat_mode_and_aligns_it_rounded(world):
    """Fixrunde A, L4-22: Thermostatmodus und Anzeige vor dem Soll (der Konverter verwirft ein Soll, solange Zigbee2MQTT
    den Modus fuer OFF haelt), das Soll ganzzahlig kaufmaennisch gerundet (22,5 -> 23), kein heat beim Start. Das W100
    hat vor dem Start thermostat_mode ON und heat gemeldet (L4-30, A6-14)."""
    w100 = "0x54ef44100140eb34"  # Testwert
    world.z2m.add_w100(w100)
    world.z2m.report(w100, thermostat_mode="ON", system_mode="heat", occupied_heating_setpoint=20, local_temperature=20.0)
    dateien.schreiben_json(world.paths.room_target, {"value": 22.5, "source": "portal", "ts": "t"}, modus=dateien.LESBAR)
    bindung = {**konfiguration()["bindung"], "thermostat": w100}
    dokumente_schreiben(world.paths, konfiguration(bindung=bindung))
    dateien.schreiben_json(world.paths.device_dir / "bedienung.json",
                           {"version": 1, "inhalt": bedienung(22.5), "dirty": False}, modus=dateien.PRIVAT)
    world.start_runtime()
    world.run()
    assert [body for ieee, body in world.z2m.set_requests if ieee == w100] == [
        {"thermostat_mode": "ON"}, {"auto_hide_middle_line": False}, {"occupied_heating_setpoint": 23.0}]
    assert world.host._store.value == 22.5
