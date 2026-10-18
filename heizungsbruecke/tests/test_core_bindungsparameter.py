"""Spec Stufe 3 S4b 4.2: Pruefung der Bindungsparameter; jede Regel knapp gueltig und knapp ungueltig."""
import dataclasses
import math

import pytest
from bindung_beispiele import PARAMETER, VAILLANT_MYPYLLANT, WEISHAUPT_SCHEMATA, parameter

from smartheat_core.binding import ENERGY_DAILY
from smartheat_core.bindungsparameter import (
    ENERGIE_KANAELE,
    SCHRITT_MAX,
    WARTEZEIT_S,
    Fehler,
    beschreibung,
)
from smartheat_device import wire


def test_energiekanaele_gleich_den_energierollen():
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
    assert d.enforce_tolerance == {"curve": 0.025, "room_setpoint": 0.25, "heat_limit": 0.05, "min_flow": 0.05}
    assert d.labels == {"curve": "Heizkurve", "room_setpoint": "Wunschtemperatur der Zone",
                        "heat_limit": "Heizgrenze", "min_flow": "Mindestvorlauftemperatur"}
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
    ("weishaupt_wwp", {"praefixe": ["hz_operationmode_"], "normal_bereich": {"hz_operationmode_": [16.0, 28.0]}}, True),
    ("weishaupt_wwp", {"praefixe": ["hz_operationmode_"], "normal_bereich": {"b_": [16.0, 28.0]}}, False),
    ("weishaupt_wwp", {"praefixe": ["hz_operationmode_"], "normal_bereich": {"hz_operationmode_": [28.0, 16.0]}}, False),
    ("weishaupt_wwp", {"praefixe": ["a_"], "normal_bereich": {"a_": [16.0, 28.0]}}, False),  # fremder Praefix
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


H = "hz_operationmode_"
V2 = "heating_circuit_operation_mode_"


def _schemata_mit(praefixe, bereich):
    return {"praefixe": praefixe, "normal_bereich": bereich}


@pytest.mark.parametrize(("schemata", "gut"), [
    (_schemata_mit(["p" * 64], {"p" * 64: [16.0, 28.0]}), False),  # nicht in der lokalen Positivliste
    (_schemata_mit(["p" * 65], {"p" * 65: [16.0, 28.0]}), False),
    (_schemata_mit([""], {"": [16.0, 28.0]}), False),
    (_schemata_mit([H], {H: [20.0, 20.0]}), True),
    (_schemata_mit([H], {H: [math.nan, 28.0]}), False),
    (_schemata_mit([H], {H: [16.0, 20.0, 28.0]}), False),
    (_schemata_mit([H, H], {H: [16.0, 28.0]}), False),
    (_schemata_mit(["a_"], {"a_": [16.0, 28.0]}), False),  # fremder Praefix
    (_schemata_mit([H, "a_"], {H: [16.0, 28.0], "a_": [16.0, 28.0]}), False),  # eine Teilmenge ist gueltig, mehr nicht
    (_schemata_mit([V2], {V2: [18.0, 25.0]}), True),  # Teilmenge: nur das zweite Schema
    (_schemata_mit([V2, H], {V2: [18.0, 25.0], H: [16.0, 28.0]}), True),
    (_schemata_mit(["hz_", "hz_operationmode_"], {"hz_": [16.0, 28.0], "hz_operationmode_": [16.0, 28.0]}), False),
    (_schemata_mit(["hz_operationmode_", "hz_"], {"hz_": [16.0, 28.0], "hz_operationmode_": [16.0, 28.0]}), False),
    ({"praefixe": [H]}, False),
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


def _vaillant_durchsetzen(durchsetzen_tag):
    return beschreibung("vaillant_vrc720", parameter("vaillant_vrc720", schreibschutz={
        "tag": None, "lebensdauer_hinweis": None, "durchsetzen_tag": durchsetzen_tag}))


def test_vaillant_durchsetzen_tag_6_auf_der_lokalen_obergrenze_wird_angenommen():
    """Lokale Obergrenze des Vaillant-Schreibschutzes: 6 Durchsetzungen je Tag (Spec Stufe 3 S4b 4.2)."""
    assert _vaillant_durchsetzen(6).enforce_per_day == 6


def test_vaillant_durchsetzen_tag_7_knapp_ueber_der_lokalen_obergrenze_wird_abgelehnt():
    with pytest.raises(Fehler) as info:
        _vaillant_durchsetzen(7)
    assert info.value.details == {"feld": "durchsetzen_tag", "regel": "bereich", "min": 1, "max": 6}


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


@pytest.mark.parametrize(("change", "match"), [
    ({"steps": {"curve": 0.05}}, "Schrittweite"),
    ({"enforce_tolerance": {"curve": 0.025}}, "Toleranz"),
    ({"labels": {}}, "Anzeigename"),
    ({"restore_originals": ("level",)}, "restore_originals"),
    ({"optional_restore": ("min_flow",)}, "optional_restore"),
    ({"prepared_lever": "level"}, "prepared_lever"),
    ({"settle_seconds": 0}, "Wartezeit"),
    ({"daily_write_limit": 0}, "daily_write_limit"),
    ({"lifetime_hint_at": 0}, "lifetime_hint_at"),
    ({"enforce_per_day": 0}, "enforce_per_day"),
    ({"write_groups": (("curve",),)}, "Schreibgruppe"),
    ({"write_groups": (("curve", "level"),)}, "Schreibgruppe"),
    ({"write_groups": (("curve", "room_setpoint"), ("room_setpoint", "heat_limit"))}, "Schreibgruppe"),
    ({"energy_counters": "monthly"}, "energy_counters"),
    ({"readonly_levers": ("curve",)}, "readonly_levers"),
    ({"readonly_levers": ("slope",)}, "readonly_levers"),
])
def test_description_is_validated(change, match):
    """Die Invarianten von BindingDescription (vorher in test_core_binding.py)."""
    with pytest.raises(ValueError, match=match):
        dataclasses.replace(VAILLANT_MYPYLLANT, **change)


# --- Mutationsprobe Stufe 3 S4b (A5-22 Rest): Grenzen als feste Zahlen, Fehlertexte je Pruefung ---

@pytest.mark.parametrize(("schritt", "gut"), [(5.0, True), (5.0001, False), (5.25, False)])
def test_schrittweite_hoechstens_5_als_zahl(schritt, gut):
    werte = parameter("vaillant_vrc720")
    werte["schritte"]["curve"] = schritt
    werte["toleranzen"]["curve"] = 1e-6
    text = _try(werte)
    assert gut == (text is None)
    if not gut:
        assert "höchstens 5 sein" in text


def test_schrittweite_0_scheitert_an_der_schrittweite_nicht_erst_an_der_toleranz():
    werte = parameter("vaillant_vrc720")
    werte["schritte"]["curve"] = 0
    werte["toleranzen"]["curve"] = 1e-6
    assert "Schrittweite für curve" in _try(werte)


@pytest.mark.parametrize(("wartezeit", "gut"), [(60, True), (59.9, False), (7200, True), (7200.5, False), (7201, False)])
def test_wartezeit_60_bis_7200_als_zahl(wartezeit, gut):
    text = _try(parameter("vaillant_vrc720", wartezeit_s=wartezeit))
    assert gut == (text is None)
    if not gut:
        assert "zwischen 60 und 7200 Sekunden" in text


def test_ohne_schemata_im_verfahren_ist_schemata_ein_leeres_mapping():
    assert beschreibung("vaillant_vrc720", PARAMETER["vaillant_vrc720"]).schemata == {}


@pytest.mark.parametrize(("praefix", "text"), [
    ("p", "nicht kennt"),  # Form gueltig (1 Zeichen), dann die Positivliste
    ("p" * 64, "nicht kennt"),  # Form gueltig (64 Zeichen), dann die Positivliste
    ("p" * 65, "praefixe der Bindungsparameter ist ungültig"),
    ("", "praefixe der Bindungsparameter ist ungültig"),
    (5, "praefixe der Bindungsparameter ist ungültig"),
])
def test_praefix_form_wird_vor_der_positivliste_geprueft(praefix, text):
    schemata = _schemata_mit([praefix], {praefix: [16.0, 28.0]})
    assert text in _try(parameter("weishaupt_wwp_basis", schemata=schemata), "weishaupt_wwp_basis")


def _mit(hebelsatz, **aenderungen):
    return hebelsatz, parameter(hebelsatz, **aenderungen)


def _ohne_min_flow():
    werte = parameter("vaillant_vrc720")
    del werte["schritte"]["min_flow"]
    return "vaillant_vrc720", werte


def _kurve(schritt, toleranz):
    werte = parameter("vaillant_vrc720")
    werte["schritte"]["curve"], werte["toleranzen"]["curve"] = schritt, toleranz
    return "vaillant_vrc720", werte


_W = "weishaupt_wwp_basis"
# Je Pruefung in beschreibung ein Verstoss (Abschlussreview Minor 1): jeder Text ist fuer sich verstaendlich und nennt
# die Bindungsparameter genau einmal (config_check setzt kein Praefix mehr davor).
_VERSTOESSE = {
    "hebel": _mit("vaillant_vrc720", zurueck=["min_flow"]),
    "zahlen_je_hebel": _ohne_min_flow(),
    "schreibschutz_unvollstaendig": _mit("vaillant_vrc720", schreibschutz={"tag": None, "lebensdauer_hinweis": None}),
    "durchsetzen": _mit("vaillant_vrc720", schreibschutz={"tag": None, "lebensdauer_hinweis": None,
                                                           "durchsetzen_tag": 0}),
    "schreibschutz_locker": _mit("weishaupt_wwp", schreibschutz={"tag": 11, "lebensdauer_hinweis": 50000,
                                                                  "durchsetzen_tag": 6}),
    "schemata": _mit("vaillant_vrc720", schemata=WEISHAUPT_SCHEMATA),
    "praefixe_form": _mit(_W, schemata=_schemata_mit([""], {"": [16.0, 28.0]})),
    "praefixe_fremd": _mit(_W, schemata=_schemata_mit(["a_"], {"a_": [16.0, 28.0]})),
    "normal_bereich": _mit(_W, schemata=_schemata_mit([H], {H: [28.0, 16.0]})),
    "energie": _mit("vaillant_vrc720", energie={"gas": "kWh"}),
    "hebelsatz": ("gibt_es_nicht", PARAMETER["vaillant_vrc720"]),
    "fehlen": ("vaillant_vrc720", None),
    "felder": ("vaillant_vrc720", {}),
    "schrittweite": _kurve(0, 1e-6),
    "toleranz": _kurve(0.05, 0.06),
    "wartezeit": _mit("vaillant_vrc720", wartezeit_s=59),
    "schreibgruppen_form": _mit("vaillant_vrc720", schreibgruppen={"a": ["curve", "heat_limit"]}),
    "schreibgruppen_regel": _mit("vaillant_vrc720", schreibgruppen=[["curve"]]),
    "zaehler": _mit("vaillant_vrc720", zaehler="monthly"),
    "zurueck_optional": _mit("vaillant_vrc720", zurueck=[], zurueck_optional=["heat_limit"]),
}


@pytest.mark.parametrize("fall", sorted(_VERSTOESSE))
def test_jeder_fehlertext_nennt_die_bindungsparameter_genau_einmal(fall):
    hebelsatz, werte = _VERSTOESSE[fall]
    text = _try(werte, hebelsatz)
    assert text and text.count("Bindungsparameter") == 1, text
    assert not text.startswith("Bindungsparameter") and "fuer" not in text, text


def test_die_faelle_treffen_verschiedene_pruefungen():
    texte = {fall: _try(werte, hebelsatz) for fall, (hebelsatz, werte) in _VERSTOESSE.items()}
    assert len(set(texte.values())) == len(texte), texte


def test_jeder_fehler_traegt_details_des_vertrags():
    from smartheat_core.bindungsparameter import Fehler
    from smartheat_device import fehler
    kaputt = [None, {**parameter("vaillant_vrc720"), "wartezeit_s": 1},
              {**parameter("vaillant_vrc720"), "zaehler": "x"}, {**parameter("vaillant_vrc720"), "energie": 5}]
    for werte in kaputt:
        with pytest.raises(Fehler) as info:
            beschreibung("vaillant_vrc720", werte)
        assert fehler.gueltig(info.value.details) and info.value.details["feld"] in wire.PRUEF_FELDER
    with pytest.raises(Fehler) as info:
        beschreibung("erfunden", parameter("vaillant_vrc720"))
    assert info.value.details == {"feld": "hebelsatz", "regel": "unbekannt"}
