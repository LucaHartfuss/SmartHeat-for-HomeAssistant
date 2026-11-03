"""Kennung der Gateway-Kreise: die Treiber nennen dieselben Anlagenparameter wie die Tabelle des Geraets."""
import pytest

from smartheat_core import kennung
from smartheat_gateway.drivers import registry


def test_jeder_treiber_hat_einen_eintrag_und_seine_identitaetsparameter_sind_anlage_plus_heizkreis():
    assert set(registry.DRIVERS) == set(kennung.TREIBER)
    for treiber_id, klasse in registry.DRIVERS.items():
        erwartet = (*kennung.TREIBER[treiber_id][1], "heizkreis")
        assert tuple(klasse.IDENTITY_PARAMETERS) == erwartet, treiber_id


def test_die_kennung_des_treibers_enthaelt_weder_treiber_noch_weg():
    text = registry.plant_id({"id": "vicare_cloud", "parameter": {
        "installation_id": 5, "gateway_serial": "G", "device_id": "0", "heizkreis": 1}})
    assert text == '{"anlage":"5-G-0","hersteller":"viessmann","nummer":1}'


def test_fehlt_ein_anlagenparameter_scheitert_die_kennung_laut():
    with pytest.raises(kennung.KennungFehler):
        registry.plant_id({"id": "simulation", "parameter": {"heizkreis": 0}})
