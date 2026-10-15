"""Spec Stufe 3 S4b 3: lokaler Verfahrensteil je Hebelsatz."""
from smartheat_core.levers import LEVER_SETS
from smartheat_core.safety import LOCAL_SAFETY
from smartheat_core.verfahren import VERFAHREN


def test_jeder_hebelsatz_hat_genau_einen_verfahrensteil():
    assert set(VERFAHREN) == set(LEVER_SETS)
    assert all(v.lever_set is LEVER_SETS[k] for k, v in VERFAHREN.items())


def test_jeder_hebelsatz_mit_verfahren_hat_lokale_sicherheitswerte():
    assert set(VERFAHREN) <= {hebelsatz for hebelsatz, _ in LOCAL_SAFETY}


def test_verfahrensfelder_wie_bis_1_0_0():
    v, w, b, s = (VERFAHREN[k] for k in ("vaillant_vrc720", "weishaupt_wwp", "weishaupt_wwp_basis", "viessmann_vicare"))
    assert (v.prepared_lever, v.preparation_resets_setpoint, v.aux_originals, v.schemata) == ("room_setpoint", True, (), ())
    for x in (w, b):
        assert (x.prepared_lever, x.preparation_resets_setpoint) == ("room_setpoint", False)
        assert x.aux_originals == ("mode_select", "setpoint_comfort", "setpoint_setback")
        assert x.schemata == ("praefixe", "normal_bereich")
    assert (s.prepared_lever, s.preparation_resets_setpoint, s.aux_originals, s.schemata) == (
        "room_setpoint", False, ("mode_select",), ())
    assert v.labels["min_flow"] == "Mindestvorlauftemperatur" and s.preparation_label == "Heizprogramm"
