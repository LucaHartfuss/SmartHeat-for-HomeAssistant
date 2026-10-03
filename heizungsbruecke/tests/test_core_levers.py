import pytest

from smartheat_core.levers import (
    LEVER_SETS,
    LEVERS,
    VAILLANT_VRC720,
    VIESSMANN_VICARE,
    WEISHAUPT_WWP,
    WEISHAUPT_WWP_BASIS,
    LeverSet,
)


def test_vocabulary_and_vaillant_lever_set():
    assert LEVERS == ("curve", "room_setpoint", "level", "heat_limit", "min_flow")
    assert LeverSet("vaillant_vrc720", ("curve", "room_setpoint", "heat_limit"), ("min_flow",)) == VAILLANT_VRC720
    assert LEVER_SETS["vaillant_vrc720"] is VAILLANT_VRC720


def test_new_lever_sets_mirror_the_server():
    """Plan 3b: gleiche IDs, Hebel und Reihenfolge wie heizungsserver.generic.plants (Contract-Checks 4 und 40)."""
    assert LeverSet("weishaupt_wwp", ("curve", "room_setpoint", "heat_limit")) == WEISHAUPT_WWP
    assert LeverSet("weishaupt_wwp_basis", ("room_setpoint",)) == WEISHAUPT_WWP_BASIS
    assert LeverSet("viessmann_vicare", ("curve", "level", "room_setpoint")) == VIESSMANN_VICARE
    assert list(LEVER_SETS) == ["vaillant_vrc720", "weishaupt_wwp", "weishaupt_wwp_basis", "viessmann_vicare"]


@pytest.mark.parametrize(("levers", "derived", "match"), [
    ((), (), "keine Hebel"),
    (("curve", "slope"), (), "unbekannte Hebel: slope"),
    (("curve",), ("curve",), "sowohl gesendet als auch abgeleitet: curve"),
])
def test_invalid_lever_sets(levers, derived, match):
    with pytest.raises(ValueError, match=match):
        LeverSet("x", levers, derived)
