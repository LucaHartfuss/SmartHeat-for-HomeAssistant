"""Bindung, Laufzeit-Konfiguration, Treiberpruefung, Start-Soll und Abmelde-Markierung aus dem
Konfigurationsdokument."""
from pathlib import Path

import pytest
from configs import SENSOR, THERMOSTAT, konfiguration, leer

from smartheat_core import rastung
from smartheat_device import wire
from smartheat_device.documents import DocumentStore
from smartheat_gateway import config
from smartheat_gateway.paths import Paths
from smartheat_gateway.version import GATEWAY_VERSION
from smartheat_runtime import dokument_config, entitlement
from smartheat_runtime.dokument_config import ConfigError

PARAMETER = {"lever_set": "viessmann_vicare", "heizkreis": 0}


def test_binding_from_the_document():
    bindung = config.bindung(konfiguration())
    assert bindung == config.GatewayBindung("simulation", PARAMETER, (f"zigbee:{SENSOR}:temperature",), THERMOSTAT)
    assert bindung.driver_spec() == {"id": "simulation", "parameter": PARAMETER}


@pytest.mark.parametrize("change", [
    {"bindung": {"treiber": {"id": "erfunden", "parameter": {}}, "thermostat": None}}, {"bindung": None},
    {"raumfuehler": ["sensor.wohnzimmer"]}, {"raumfuehler": []},
    {"bindung": {"treiber": {"id": "simulation", "parameter": {}}, "thermostat": "wohnzimmer"}},
])
def test_unusable_bindings_are_config_errors(change):
    with pytest.raises(ConfigError):
        config.bindung(konfiguration(**change))


def test_an_ieee_address_in_capitals_is_refused_as_thermostat_and_as_sensor():
    """Zigbee2MQTT meldet Kleinbuchstaben (`0x00124b...`); die Regex kennt nur diese. Eine Adresse mit Grossbuchstaben
    waere ein anderer Schluessel im Spiegel und fiele dort nie auf ein Geraet (Festhalten)."""
    gross = THERMOSTAT.upper().replace("0X", "0x")  # 0x00124B0000000002
    assert gross != THERMOSTAT and gross.startswith("0x")
    klein_ok = config.bindung(konfiguration())  # Gegenprobe: die Kleinbuchstaben-Form wird angenommen
    assert klein_ok.thermostat == THERMOSTAT and klein_ok.room_sensors == (f"zigbee:{SENSOR}:temperature",)
    treiber = {"id": "simulation", "parameter": PARAMETER}
    with pytest.raises(ConfigError, match="keine IEEE-Adresse"):
        config.bindung(konfiguration(bindung={"treiber": treiber, "thermostat": gross}))
    sensor_gross = SENSOR.upper().replace("0X", "0x")
    for ref in (f"zigbee:{sensor_gross}:temperature", f"zigbee:{sensor_gross}:local_temperature"):
        with pytest.raises(ConfigError, match="Raumf"):
            config.bindung(konfiguration(raumfuehler=[ref]))
    with pytest.raises(ConfigError, match="Raumf"):  # ein einziger grossgeschriebener unter gueltigen genuegt
        config.bindung(konfiguration(raumfuehler=[f"zigbee:{SENSOR}:temperature", f"zigbee:{sensor_gross}:temperature"]))


def test_parse_gives_the_runtime_config_without_transport(tmp_path):
    paths = Paths(tmp_path)
    runtime, bindung = config.parse_dokument(konfiguration(), paths, lambda: entitlement.ACTIVE)
    assert runtime.entitlement_path == paths.entitlement and runtime.abo_source() == entitlement.ACTIVE
    assert (runtime.tenant_id, runtime.lever_set_id, bindung.driver_id) == ("test-tenant", "viessmann_vicare",
                                                                            "simulation")


def test_the_driver_check_asks_the_driver_without_creating_a_plant(tmp_path):
    paths = Paths(tmp_path)
    assert config.treiber_pruefen(paths, konfiguration()["bindung"]) is None
    assert config.treiber_pruefen(paths, {"treiber": {"id": "x", "parameter": {}}}) == {"feld": "id", "regel": "unbekannt"}
    assert config.treiber_pruefen(paths, {"treiber": {"id": "vicare_cloud", "parameter": {"installation_id": "x"}}}) == {
        "feld": "parameter", "regel": "ungueltig"}
    assert config.treiber_pruefen(paths, {"treiber": {"id": "simulation", "parameter": {"lever_set": "erfunden"}}}) == {
        "feld": "parameter", "regel": "ungueltig"}
    assert config.treiber_pruefen(paths, {"treiber": "kaputt"}) == {"feld": "id", "regel": "unbekannt"}
    assert not paths.sim_dir.exists()


NICHT_ENDLICH = [pytest.param(float("nan"), id="nan"), pytest.param(float("inf"), id="inf"),
                 pytest.param(float("-inf"), id="minus-inf"), pytest.param(10**400, id="gross-positiv"),
                 pytest.param(-10**400, id="gross-negativ")]


@pytest.mark.parametrize("wert", NICHT_ENDLICH)
@pytest.mark.parametrize(("treiber", "schluessel"), [
    ("vicare_cloud", "poll_seconds"),
    *[("simulation", key) for key in ("poll_seconds", "settle_delay", "aussen_mittel", "aussen_amplitude", "rauschen",
                                      "raum_start")],
])
def test_a_driver_parameter_that_is_not_a_finite_number_is_refused(tmp_path, treiber, schluessel, wert):
    # JSON liest NaN, Infinity und beliebig grosse Ganzzahlen. NaN liess die Abfrage ohne Pause laufen (Event.wait(NaN)
    # kehrt sofort zurueck, Cloud-Kontingent leer), Infinity den Abfrage-Thread sterben und nie den Cache altern.
    paths = Paths(tmp_path)
    ablehnung = config.treiber_pruefen(paths, {"treiber": {"id": treiber, "parameter": {schluessel: wert}}})
    assert ablehnung == {"feld": "parameter", "regel": "ungueltig"}
    assert not paths.sim_dir.exists()


def test_sign_off_marker_belongs_to_one_plant(tmp_path):
    paths = Paths(tmp_path)
    assert not config.abgemeldet(paths, konfiguration())
    assert config.abmelden(paths, "test-tenant") is True
    assert config.abmelden(paths, "test-tenant") is False  # erneute Zustellung: nichts Neues
    assert config.abgemeldet(paths, konfiguration())
    assert not config.abgemeldet(paths, konfiguration(anlage="andere")) and not config.abgemeldet(paths, None)


def test_new_paths():
    paths = Paths(Path("/data"))
    assert (str(paths.inventur), str(paths.abgemeldet), str(paths.aufraeumen)) == (
        "/data/inventur.json", "/data/runtime/abgemeldet.json", "/data/aufraeumen.json")
    assert str(paths.rastung) == "/data/device/sicherheitssatz.json"
    assert [str(p) for p in (paths.software_soll, paths.software_meldungen, paths.software_quittung)] == [
        "/data/software_soll.json", "/data/software_meldungen.json", "/data/software_quittung.json"]


def test_the_empty_configuration_and_the_test_configuration_are_accepted_by_the_document_store(tmp_path):
    """Die Testdokumente passen zum Dokumentspeicher des Geraetekerns (Pflichtfelder, Mindestversion 0.6.0)."""
    paths = Paths(tmp_path)
    store = DocumentStore(paths.device_dir, host="gateway", software_version=GATEWAY_VERSION,
                          treiber_pruefen=lambda bindung: config.treiber_pruefen(paths, bindung))
    ergebnis = store.empfangen("konfiguration", {"schema": wire.SCHEMA, "version": 1, **konfiguration()})
    assert (ergebnis.ok, ergebnis.uebernommen) == (True, True) and dokument_config.eingerichtet(
        store.inhalt("konfiguration"))
    ergebnis = store.empfangen("konfiguration", {"schema": wire.SCHEMA, "version": 2, **leer()})
    assert (ergebnis.ok, ergebnis.uebernommen) == (True, True), ergebnis.details
    assert not store.eingerichtet() and not dokument_config.eingerichtet(store.inhalt("konfiguration"))


def test_rastung_path_is_where_the_document_store_writes_the_lock(tmp_path):
    """Die zweite Linie liest die Datei, die der Dokumentspeicher des Geraetekerns schreibt."""
    paths = Paths(tmp_path)
    assert paths.rastung == paths.device_dir / rastung.DATEI
    store = DocumentStore(paths.device_dir, host="gateway", software_version=GATEWAY_VERSION,
                          treiber_pruefen=lambda bindung: config.treiber_pruefen(paths, bindung))
    ergebnis = store.empfangen("konfiguration", {"schema": wire.SCHEMA, "version": 1, **konfiguration()})
    assert ergebnis.ok and paths.rastung.is_file()
    assert rastung.lesen(paths.rastung) == rastung.Rastung("viessmann_vicare", "Heizkoerper")
    config.parse_dokument(konfiguration(), paths, lambda: entitlement.ACTIVE)
    with pytest.raises(ConfigError, match="festgelegt"):
        config.parse_dokument(konfiguration(verteilsystem="Fussbodenheizung"), paths, lambda: entitlement.ACTIVE)
