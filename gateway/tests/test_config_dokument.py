"""Plan 5b D2 Task 1: Bindung, Laufzeit-Konfiguration, Treiberpruefung, Start-Soll und Abmelde-Markierung aus dem
Konfigurationsdokument."""
from pathlib import Path

import pytest
from configs import SENSOR, THERMOSTAT, bedienung, konfiguration, leer

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


@pytest.mark.parametrize("inhalt, start", [
    (bedienung(22.5), 22.5), (None, config.DEFAULT_RAUM_SOLL), (bedienung(30.0), config.DEFAULT_RAUM_SOLL),
    ({"modus": "manuell"}, config.DEFAULT_RAUM_SOLL),
])
def test_start_target_comes_from_the_operation(inhalt, start):
    assert config.raum_soll_start(inhalt) == start


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
    """Die zweite Linie (Spec Stufe 3 S3 3.3) liest die Datei, die der Dokumentspeicher des Geraetekerns schreibt."""
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
