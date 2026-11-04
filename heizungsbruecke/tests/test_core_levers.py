import dataclasses

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
    assert LeverSet("vaillant_vrc720", ("curve", "room_setpoint", "heat_limit", "min_flow")) == VAILLANT_VRC720
    assert LEVER_SETS["vaillant_vrc720"] is VAILLANT_VRC720


def test_new_lever_sets_mirror_the_server():
    """Gleiche IDs, Hebel und Reihenfolge wie heizungsserver.regelung.hersteller.plants (Contract-Checks,
    Vertrag V1 und V4)."""
    assert LeverSet("weishaupt_wwp", ("curve", "room_setpoint", "heat_limit")) == WEISHAUPT_WWP
    assert LeverSet("weishaupt_wwp_basis", ("room_setpoint",)) == WEISHAUPT_WWP_BASIS
    assert LeverSet("viessmann_vicare", ("curve", "level", "room_setpoint")) == VIESSMANN_VICARE
    assert list(LEVER_SETS) == ["vaillant_vrc720", "weishaupt_wwp", "weishaupt_wwp_basis", "viessmann_vicare"]


@pytest.mark.parametrize(("levers", "match"), [
    ((), "keine Hebel"),
    (("curve", "slope"), "unbekannte Hebel: slope"),
])
def test_invalid_lever_sets(levers, match):
    with pytest.raises(ValueError, match=match):
        LeverSet("x", levers)


def test_every_lever_of_a_set_comes_from_the_server():
    """Kein Hebel wird am Geraet abgeleitet: ein Hebelsatz kennt nur gesendete Hebel (auch den Mindestvorlauf)."""
    assert [field.name for field in dataclasses.fields(LeverSet)] == ["id", "levers"]
