"""Spec Stufe 3 S4b 4.2: Pruefung der Bindungsparameter; jede Regel knapp gueltig und knapp ungueltig."""
import math

import pytest
from bindung_beispiele import PARAMETER, WEISHAUPT_SCHEMATA, parameter

from smartheat_core.binding import ENERGY_DAILY
from smartheat_core.bindungsparameter import (
    EINHEITEN,
    ENERGIE_KANAELE,
    FELDER,
    SCHREIBSCHUTZ_FELDER,
    SCHRITT_MAX,
    WARTEZEIT_S,
    ZAEHLER,
    beschreibung,
)
from smartheat_device import wire


def test_konstanten_gleich_dem_vertrag():
    assert FELDER == wire.BINDUNGSPARAMETER_FIELDS
    assert SCHREIBSCHUTZ_FELDER == wire.SCHREIBSCHUTZ_FIELDS
    assert ZAEHLER == wire.ZAEHLER
    assert tuple(EINHEITEN) == wire.ENERGIE_EINHEITEN
    assert set(ENERGIE_KANAELE) == {rolle.removeprefix("energy_") for rolle in wire.HA_ROLLEN
                                    if rolle.startswith("energy_")}


@pytest.mark.parametrize("hebelsatz", sorted(PARAMETER))
def test_die_heutigen_parameter_werden_angenommen(hebelsatz):
    d = beschreibung(hebelsatz, PARAMETER[hebelsatz])
    assert d.lever_set.id == hebelsatz and d.energy_counters == ENERGY_DAILY


def test_vaillant_beschreibung_wie_bis_1_0_0():
    d = beschreibung("vaillant_vrc720", PARAMETER["vaillant_vrc720"])
    assert d.steps == {"curve": 0.05, "room_setpoint": 0.5, "heat_limit": 0.1, "min_flow": 0.1}
    assert d.settle_seconds == 2100 and d.enforce_per_day == 6
    assert (d.restore_originals, d.optional_restore, d.prepared_lever) == (("heat_limit",), ("heat_limit",), "room_setpoint")
    assert (d.daily_write_limit, d.lifetime_hint_at, d.write_groups, d.aux_originals) == (None, None, (), ())
    assert d.preparation_resets_setpoint is True and d.preparation_label == "Betriebsart der Zone"


def test_weishaupt_schemata_als_tupel():
    d = beschreibung("weishaupt_wwp", PARAMETER["weishaupt_wwp"])
    assert d.schemata == {"praefixe": ("hz_operationmode_", "heating_circuit_operation_mode_"),
                          "normal_bereich": {"hz_operationmode_": (16.0, 28.0),
                                             "heating_circuit_operation_mode_": (18.0, 25.0)}}
    assert (d.daily_write_limit, d.lifetime_hint_at) == (10, 50000)


def _abgelehnt(hebelsatz, werte) -> str:
    with pytest.raises(ValueError) as info:
        beschreibung(hebelsatz, werte)
    return str(info.value)


@pytest.mark.parametrize("werte", [None, [], "x", {}])
def test_fehlende_parameter_werden_abgelehnt(werte):
    assert _abgelehnt("vaillant_vrc720", werte)


def test_fremdes_oder_fehlendes_feld_wird_abgelehnt():
    werte = parameter("vaillant_vrc720")
    del werte["energie"]
    assert _abgelehnt("vaillant_vrc720", werte)
    assert _abgelehnt("vaillant_vrc720", parameter("vaillant_vrc720", fremd=1))


def test_unbekannter_hebelsatz_wird_abgelehnt():
    assert "Schreibverfahren" in _abgelehnt("gibt_es_nicht", PARAMETER["vaillant_vrc720"])


@pytest.mark.parametrize(("schritt", "gut"), [
    (SCHRITT_MAX, True), (SCHRITT_MAX + 0.01, False), (1e-6, True), (0, False), (-0.05, False),
    (math.nan, False), (math.inf, False), (True, False), ("0.05", False),
])
def test_schrittweite(schritt, gut):
    werte = parameter("vaillant_vrc720")
    werte["schritte"]["curve"] = schritt
    werte["toleranzen"]["curve"] = 1e-6
    if gut:
        assert beschreibung("vaillant_vrc720", werte).steps["curve"] == schritt
    else:
        assert _abgelehnt("vaillant_vrc720", werte)


@pytest.mark.parametrize(("toleranz", "gut"), [(0.05, True), (0.0500001, False), (1e-9, True), (0, False)])
def test_toleranz_hoechstens_ein_schritt(toleranz, gut):
    werte = parameter("vaillant_vrc720")
    werte["toleranzen"]["curve"] = toleranz
    assert gut == (_try(werte) is None)


def _try(werte, hebelsatz="vaillant_vrc720"):
    try:
        beschreibung(hebelsatz, werte)
    except ValueError as error:
        return str(error)
    return None


def test_schritte_und_toleranzen_genau_fuer_die_hebel_des_hebelsatzes():
    werte = parameter("vaillant_vrc720")
    del werte["schritte"]["min_flow"]
    assert _try(werte)
    werte = parameter("vaillant_vrc720")
    werte["toleranzen"]["level"] = 0.5
    assert _try(werte)


@pytest.mark.parametrize(("wartezeit", "gut"), [
    (WARTEZEIT_S[0], True), (WARTEZEIT_S[0] - 1, False), (WARTEZEIT_S[1], True), (WARTEZEIT_S[1] + 1, False),
    (None, False), (math.nan, False),
])
def test_wartezeit(wartezeit, gut):
    assert gut == (_try(parameter("vaillant_vrc720", wartezeit_s=wartezeit)) is None)


@pytest.mark.parametrize(("feld", "wert", "gut"), [
    ("zurueck", ["heat_limit"], True), ("zurueck", ["min_flow"], False), ("zurueck", ["level"], False),
    ("zurueck", ["heat_limit", "heat_limit"], False), ("zurueck", "heat_limit", False),
    ("zurueck_optional", [], True), ("zurueck_optional", ["curve"], False), ("zurueck_optional", ["x"], False),
    ("nur_lesen", ["x"], False), ("schreibgruppen", {"a": ["curve", "heat_limit"]}, False),
    ("nur_lesen", ["level"], True), ("nur_lesen", ["curve"], False),
    ("schreibgruppen", [["curve", "room_setpoint"]], True), ("schreibgruppen", [["curve"]], False),
    ("schreibgruppen", [["curve", "heat_limit"], ["curve", "room_setpoint"]], False),
    ("zaehler", "total", True), ("zaehler", "monthly", False),
])
def test_listen_und_zaehlerart(feld, wert, gut):
    assert gut == (_try(parameter("vaillant_vrc720", **{feld: wert})) is None)


@pytest.mark.parametrize(("hebelsatz", "schutz", "gut"), [
    ("weishaupt_wwp", {"tag": 10, "lebensdauer_hinweis": 50000, "durchsetzen_tag": 6}, True),
    ("weishaupt_wwp", {"tag": 9, "lebensdauer_hinweis": 40000, "durchsetzen_tag": 5}, True),  # strenger
    ("weishaupt_wwp", {"tag": 11, "lebensdauer_hinweis": 50000, "durchsetzen_tag": 6}, False),
    ("weishaupt_wwp", {"tag": None, "lebensdauer_hinweis": 50000, "durchsetzen_tag": 6}, False),
    ("weishaupt_wwp", {"tag": 10, "lebensdauer_hinweis": 50001, "durchsetzen_tag": 6}, False),
    ("weishaupt_wwp", {"tag": 10, "lebensdauer_hinweis": None, "durchsetzen_tag": 6}, False),
    ("weishaupt_wwp", {"tag": 10, "lebensdauer_hinweis": 50000, "durchsetzen_tag": 7}, False),
    ("weishaupt_wwp", {"tag": 0, "lebensdauer_hinweis": 50000, "durchsetzen_tag": 6}, False),
    ("viessmann_vicare", {"tag": None, "lebensdauer_hinweis": None, "durchsetzen_tag": 4}, True),
    ("viessmann_vicare", {"tag": None, "lebensdauer_hinweis": None, "durchsetzen_tag": 5}, False),
    ("viessmann_vicare", {"tag": 3, "lebensdauer_hinweis": None, "durchsetzen_tag": 1}, True),  # strenger: Budget
    ("vaillant_vrc720", {"tag": None, "lebensdauer_hinweis": None, "durchsetzen_tag": 0}, False),
    ("vaillant_vrc720", {"tag": None, "lebensdauer_hinweis": None, "durchsetzen_tag": True}, False),
    ("vaillant_vrc720", {"tag": None, "lebensdauer_hinweis": None}, False),
])
def test_schreibschutz_nur_gleich_oder_strenger(hebelsatz, schutz, gut):
    assert gut == (_try(parameter(hebelsatz, schreibschutz=schutz), hebelsatz) is None)


@pytest.mark.parametrize(("hebelsatz", "schemata", "gut"), [
    ("weishaupt_wwp", WEISHAUPT_SCHEMATA, True),
    ("weishaupt_wwp", {}, False),
    ("weishaupt_wwp", {"praefixe": ["a_"], "normal_bereich": {"a_": [16.0, 28.0]}}, True),
    ("weishaupt_wwp", {"praefixe": ["a_"], "normal_bereich": {"b_": [16.0, 28.0]}}, False),
    ("weishaupt_wwp", {"praefixe": ["a_"], "normal_bereich": {"a_": [28.0, 16.0]}}, False),
    ("weishaupt_wwp", {"praefixe": [], "normal_bereich": {}}, False),
    ("vaillant_vrc720", {}, True),
    ("vaillant_vrc720", WEISHAUPT_SCHEMATA, False),
])
def test_schemata_passen_zum_hebelsatz(hebelsatz, schemata, gut):
    assert gut == (_try(parameter(hebelsatz, schemata=schemata), hebelsatz) is None)


@pytest.mark.parametrize(("energie", "gut"), [
    ({}, True), ({"primary_heating": "Wh"}, True), ({"primary_heating": "m³"}, True),
    ({"primary_heating": "MJ"}, False), ({"gas": "kWh"}, False), ([], False),
])
def test_energie(energie, gut):
    assert gut == (_try(parameter("vaillant_vrc720", energie=energie)) is None)


def test_riesenzahlen_sind_ein_wertefehler():
    werte = parameter("vaillant_vrc720")
    werte["schritte"]["curve"] = 10**400
    assert _try(werte)
    assert _try(parameter("vaillant_vrc720", wartezeit_s=10**400))
    werte = parameter("weishaupt_wwp")
    werte["schemata"]["normal_bereich"]["hz_operationmode_"] = [16.0, 10**400]
    assert _try(werte, "weishaupt_wwp")
    # ohne lokale Obergrenze ist jeder Wert >= 1 gueltig
    schutz = {"tag": 10**400, "lebensdauer_hinweis": 10**400, "durchsetzen_tag": 6}
    assert _try(parameter("vaillant_vrc720", schreibschutz=schutz)) is None


def test_zurueck_optional_nur_aus_zurueck():
    assert _try(parameter("vaillant_vrc720", zurueck=[], zurueck_optional=["heat_limit"]))
    assert _try(parameter("vaillant_vrc720", zurueck=[], zurueck_optional=[])) is None


def _schemata_mit(praefixe, bereich):
    return {"praefixe": praefixe, "normal_bereich": bereich}


@pytest.mark.parametrize(("schemata", "gut"), [
    (_schemata_mit(["p" * 64], {"p" * 64: [16.0, 28.0]}), True),
    (_schemata_mit(["p" * 65], {"p" * 65: [16.0, 28.0]}), False),
    (_schemata_mit([""], {"": [16.0, 28.0]}), False),
    (_schemata_mit(["a_"], {"a_": [20.0, 20.0]}), True),
    (_schemata_mit(["a_"], {"a_": [math.nan, 28.0]}), False),
    (_schemata_mit(["a_"], {"a_": [16.0, 20.0, 28.0]}), False),
    (_schemata_mit(["a_", "a_"], {"a_": [16.0, 28.0]}), False),
    ({"praefixe": ["a_"]}, False),
])
def test_schemata_randfaelle(schemata, gut):
    assert gut == (_try(parameter("weishaupt_wwp_basis", schemata=schemata), "weishaupt_wwp_basis") is None)


@pytest.mark.parametrize(("schutz", "gut"), [
    ({"tag": 1, "lebensdauer_hinweis": 1, "durchsetzen_tag": 6}, True),
    ({"tag": 0, "lebensdauer_hinweis": None, "durchsetzen_tag": 6}, False),
    ({"tag": None, "lebensdauer_hinweis": 0, "durchsetzen_tag": 6}, False),
    ({"tag": None, "lebensdauer_hinweis": None, "durchsetzen_tag": 6, "extra": 1}, False),
])
def test_schreibschutz_vaillant_randfaelle(schutz, gut):
    assert gut == (_try(parameter("vaillant_vrc720", schreibschutz=schutz)) is None)


def test_energie_einheit_kein_text():
    assert _try(parameter("vaillant_vrc720", energie={"primary_heating": 5}))
    assert _try(parameter("vaillant_vrc720", energie={"primary_heating": None}))


def test_basis_lehnt_fremden_hebel_ab():
    assert _try(parameter("weishaupt_wwp_basis", zurueck=["curve"]), "weishaupt_wwp_basis")
    assert _try(parameter("weishaupt_wwp_basis", nur_lesen=["room_setpoint"]), "weishaupt_wwp_basis")


def test_fehlertexte_nicht_doppelt_verpackt():
    for feld, wert in (("zurueck", ["min_flow"]), ("nur_lesen", ["curve"]), ("energie", {"gas": "kWh"}),
                       ("schreibgruppen", [["curve"]]), ("zurueck_optional", ["curve"])):
        text = _try(parameter("vaillant_vrc720", **{feld: wert}))
        assert text and "ungültig (" not in text, (feld, text)
