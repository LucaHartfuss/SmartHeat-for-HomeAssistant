"""Spec Stufe 3 S4b: Gateway-Treiber nutzen die Beschreibung aus der Konfiguration; ohne Konfiguration (Probe,
Inventur, Vergessen) haben sie keine."""
import pytest
from bindung_beispiele import PARAMETER, VIESSMANN_VICARE_BINDING

from smartheat_gateway.drivers import registry
from smartheat_gateway.paths import Paths


@pytest.mark.parametrize("driver_id", ["simulation", "vicare_cloud"])
def test_ohne_beschreibung_wirft_der_zugriff(driver_id, data_dir):
    driver = registry.create(driver_id, {}, Paths(data_dir))
    with pytest.raises(RuntimeError, match="ohne Konfiguration"):
        _ = driver.description


@pytest.mark.parametrize("hebelsatz", sorted(PARAMETER))
def test_simulation_probe_ohne_beschreibung(hebelsatz, data_dir):
    driver = registry.create("simulation", {"lever_set": hebelsatz}, Paths(data_dir))
    hebel = driver.probe()["kandidaten"][0]["hebel"]
    assert hebel
    for name, wert in hebel.items():
        assert wert["schritt"] == PARAMETER[hebelsatz]["schritte"][name]


@pytest.mark.parametrize("hebelsatz", sorted(PARAMETER))
def test_simulation_erkennung_ohne_beschreibung(hebelsatz, data_dir):
    driver = registry.create("simulation", {"lever_set": hebelsatz}, Paths(data_dir))
    hebel = driver.erkennung()["features"][0]["properties"]["hebel"]["value"]
    assert hebel
    for name, wert in hebel.items():
        assert wert["schritt"] == PARAMETER[hebelsatz]["schritte"][name]


def test_simulation_signale_und_inventur_ohne_beschreibung(data_dir):
    driver = registry.create("simulation", {"lever_set": "viessmann_vicare"}, Paths(data_dir))
    assert "outdoor_temp" in driver.signals()
    assert driver.inventory(1, [])["driver_id"] == "simulation"
    driver.forget_credentials()


def test_beschreibung_aus_der_konfiguration(data_dir):
    driver = registry.create("simulation", {"lever_set": "viessmann_vicare"}, Paths(data_dir),
                             description=VIESSMANN_VICARE_BINDING)
    assert driver.description is VIESSMANN_VICARE_BINDING


def test_vicare_beschreibung_aus_der_konfiguration(data_dir):
    driver = registry.create("vicare_cloud", {}, Paths(data_dir), description=VIESSMANN_VICARE_BINDING)
    assert driver.description is VIESSMANN_VICARE_BINDING
