"""Lokaler Verfahrensteil je Weg und Hebelsatz."""
import pytest

from heizungsbruecke import dokument, geraet_host
from smartheat_core import config_check
from smartheat_core.levers import LEVER_SETS
from smartheat_core.safety import LOCAL_SAFETY
from smartheat_core.verfahren import VERFAHREN, WEG_UNBEKANNT, WegUnbekannt, verfahren_fuer

#: Die Zeilen der Tabelle, woertlich: heute gibt es vier Wege; die Simulation traegt jeden Hebelsatz.
ZEILEN = {
    ("vaillant_myvaillant", "vaillant_vrc720"),
    ("weishaupt_modbus", "weishaupt_wwp"), ("weishaupt_modbus", "weishaupt_wwp_basis"),
    ("viessmann_vicare", "viessmann_vicare"),
    ("simulation", "vaillant_vrc720"), ("simulation", "weishaupt_wwp"), ("simulation", "weishaupt_wwp_basis"),
    ("simulation", "viessmann_vicare"),
}


def test_tabelle_hat_genau_die_heutigen_zeilen():
    assert set(VERFAHREN) == ZEILEN
    assert all(v.lever_set is LEVER_SETS[hebelsatz] for (_, hebelsatz), v in VERFAHREN.items())


def test_ein_hebelsatz_hat_auf_jedem_weg_dieselbe_zeile():
    for (_, hebelsatz), zeile in VERFAHREN.items():
        assert zeile is VERFAHREN[next(k for k in VERFAHREN if k[1] == hebelsatz)]


def test_jeder_hebelsatz_hat_eine_zeile():
    assert {hebelsatz for _, hebelsatz in VERFAHREN} == set(LEVER_SETS)


def test_wege_der_tabelle_sind_die_wege_der_konfigurationspruefung():
    assert {weg for weg, _ in VERFAHREN} == set(config_check.WEGE)


def test_jede_schreibplattform_fuehrt_zu_einer_zeile_ihres_hebelsatzes():
    for hebelsatz, plattform in dokument.SCHREIB_PLATTFORM.items():
        assert (geraet_host.WEG_JE_PLATTFORM[plattform], hebelsatz) in VERFAHREN


def test_auswahl_nach_weg_und_hebelsatz():
    assert verfahren_fuer("weishaupt_modbus", "weishaupt_wwp") is VERFAHREN[("weishaupt_modbus", "weishaupt_wwp")]
    assert verfahren_fuer("simulation", "viessmann_vicare") is VERFAHREN[("simulation", "viessmann_vicare")]


@pytest.mark.parametrize(("weg", "hebelsatz"), [
    ("vaillant_myvaillant", "viessmann_vicare"), ("viessmann_vicare", "vaillant_vrc720"),
    ("weishaupt_modbus", "vaillant_vrc720"), ("vaillant_myvaillant", "weishaupt_wwp_basis"),
    ("vaillant_myvaillant", "erfunden"),
])
def test_hebelsatz_ohne_zeile_am_weg_gibt_keine_zeile(weg, hebelsatz):
    assert verfahren_fuer(weg, hebelsatz) is None


@pytest.mark.parametrize("weg", ["vaillant_ebus", "", "Simulation", "vaillant_myvaillant ", None, 3])
def test_unbekannter_weg_scheitert_laut(weg):
    with pytest.raises(WegUnbekannt) as fehler:
        verfahren_fuer(weg, "vaillant_vrc720")
    assert fehler.value.code == WEG_UNBEKANNT == config_check.WEG_UNBEKANNT == "weg_unbekannt"


def test_jede_zeile_hat_lokale_sicherheitswerte():
    assert {hebelsatz for _, hebelsatz in VERFAHREN} <= {hebelsatz for hebelsatz, _ in LOCAL_SAFETY}


def test_verfahrensfelder_wie_bis_1_0_0():
    v, w, b, s = (VERFAHREN[k] for k in (
        ("vaillant_myvaillant", "vaillant_vrc720"), ("weishaupt_modbus", "weishaupt_wwp"),
        ("weishaupt_modbus", "weishaupt_wwp_basis"), ("viessmann_vicare", "viessmann_vicare")))
    assert (v.prepared_lever, v.preparation_resets_setpoint, v.aux_originals, v.schemata) == (
        "room_setpoint", True, ("mode_select",), ())
    for x in (w, b):
        assert (x.prepared_lever, x.preparation_resets_setpoint) == ("room_setpoint", False)
        assert x.aux_originals == ("mode_select", "setpoint_comfort", "setpoint_setback")
        assert x.schemata == ("praefixe", "normal_bereich")
    assert (s.prepared_lever, s.preparation_resets_setpoint, s.aux_originals, s.schemata) == (
        "room_setpoint", False, ("mode_select",), ())
    assert v.labels["min_flow"] == "Mindestvorlauftemperatur" and s.preparation_label == "Heizprogramm"
