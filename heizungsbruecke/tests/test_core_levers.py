import pytest

from smartheat_core.levers import LEVER_SETS, LEVERS, VAILLANT_VRC720, LeverSet


def test_vocabulary_and_vaillant_lever_set():
    assert LEVERS == ("curve", "room_setpoint", "level", "heat_limit", "min_flow")
    assert LeverSet("vaillant_vrc720", ("curve", "room_setpoint", "heat_limit"), ("min_flow",)) == VAILLANT_VRC720
    assert LEVER_SETS == {"vaillant_vrc720": VAILLANT_VRC720}


@pytest.mark.parametrize(("levers", "derived", "match"), [
    ((), (), "keine Hebel"),
    (("curve", "slope"), (), "unbekannte Hebel: slope"),
    (("curve",), ("curve",), "sowohl gesendet als auch abgeleitet: curve"),
])
def test_invalid_lever_sets(levers, derived, match):
    with pytest.raises(ValueError, match=match):
        LeverSet("x", levers, derived)
