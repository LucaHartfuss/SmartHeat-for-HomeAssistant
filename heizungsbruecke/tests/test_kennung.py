"""Kennung eines Heizkreises auf dem Geraet: `{hersteller, anlage, nummer}` ohne Weg, kanonischer Text wie am Server."""
import json

import pytest

from smartheat_core import kennung


def test_ein_ha_kreis_wird_zur_kanonischen_kennung():
    assert kennung.aus_ha_kreis("mypyllant", "Zuhause", "0") == '{"anlage":"Zuhause","hersteller":"vaillant","nummer":0}'


@pytest.mark.parametrize("plattform,hersteller", [("mypyllant", "vaillant"), ("vicare", "viessmann"),
                                                  ("weishaupt_modbus", "weishaupt")])
def test_der_hersteller_kommt_aus_der_plattform(plattform, hersteller):
    assert json.loads(kennung.aus_ha_kreis(plattform, "A", "2"))["hersteller"] == hersteller


def test_der_weg_gehoert_nicht_zur_kennung():
    assert set(json.loads(kennung.aus_ha_kreis("vicare", "A", "1"))) == {"hersteller", "anlage", "nummer"}


@pytest.mark.parametrize("plattform,anlage,nummer", [
    ("unbekannt", "A", "0"), (None, "A", "0"), ("mypyllant", "", "0"), ("mypyllant", " A", "0"),
    ("mypyllant", None, "0"), ("mypyllant", "A", "x"), ("mypyllant", "A", ""), ("mypyllant", "A", 0),
    ("mypyllant", "A", "-1"), ("mypyllant", "A", "10000"), ("mypyllant", "A", "٣"),
    ("mypyllant", "A" * 101, "0"), ("mypyllant", "A\n", "0")])
def test_eine_unbrauchbare_ha_kennung_scheitert_laut(plattform, anlage, nummer):
    with pytest.raises(kennung.KennungFehler):
        kennung.aus_ha_kreis(plattform, anlage, nummer)


def test_die_grenzen_der_nummer_und_der_anlage_gelten_beidseitig():
    assert json.loads(kennung.aus_ha_kreis("mypyllant", "A" * 100, "9999"))["nummer"] == 9999
    assert json.loads(kennung.aus_ha_kreis("mypyllant", "A", "0"))["nummer"] == 0


def test_ein_treiber_wird_aus_den_anlagenparametern_und_dem_heizkreis_zur_kennung():
    parameter = {"installation_id": 123, "gateway_serial": "G7", "device_id": "0", "heizkreis": 1, "extra": "x"}
    assert kennung.aus_treiber("vicare_cloud", parameter) == (
        '{"anlage":"123-G7-0","hersteller":"viessmann","nummer":1}')


def test_der_simulationstreiber_hat_seinen_eigenen_hersteller():
    assert json.loads(kennung.aus_treiber("simulation", {"lever_set": "heizkoerper", "heizkreis": 2})) == {
        "anlage": "heizkoerper", "hersteller": "simulation", "nummer": 2}


@pytest.mark.parametrize("treiber,parameter", [
    ("unbekannt", {"heizkreis": 1}), (None, {}), ("simulation", None), ("simulation", {"heizkreis": 1}),
    ("simulation", {"lever_set": "", "heizkreis": 1}), ("simulation", {"lever_set": "x"}),
    ("simulation", {"lever_set": "x", "heizkreis": True}), ("simulation", {"lever_set": "x", "heizkreis": "1"}),
    ("simulation", {"lever_set": "x", "heizkreis": -1}), ("simulation", {"lever_set": True, "heizkreis": 1})])
def test_ein_unbrauchbarer_treiber_scheitert_laut(treiber, parameter):
    with pytest.raises(kennung.KennungFehler):
        kennung.aus_treiber(treiber, parameter)


def test_die_tabellen_des_geraets():
    """Der Contract-Check vergleicht sie mit dem Wegekatalog des Servers (gemeinsam/wege.py)."""
    assert kennung.HERSTELLER_DER_PLATTFORM == {"mypyllant": "vaillant", "vicare": "viessmann",
                                                "weishaupt_modbus": "weishaupt"}
    assert kennung.TREIBER == {
        "vicare_cloud": ("viessmann", ("installation_id", "gateway_serial", "device_id")),
        "simulation": ("simulation", ("lever_set",))}
