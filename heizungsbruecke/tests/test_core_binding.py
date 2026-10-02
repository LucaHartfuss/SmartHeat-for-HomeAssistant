import dataclasses

import pytest

from smartheat_core.binding import BINDINGS, VAILLANT_MYPYLLANT
from smartheat_core.levers import VAILLANT_VRC720


def test_vaillant_mypyllant_description():
    d = VAILLANT_MYPYLLANT
    assert d.lever_set == VAILLANT_VRC720
    assert d.steps == {"curve": 0.05, "room_setpoint": 0.5, "heat_limit": 0.1, "min_flow": 0.1}
    assert d.enforce_tolerance == {"curve": 0.025, "room_setpoint": 0.25, "heat_limit": 0.05, "min_flow": 0.1}
    assert d.settle_seconds == 2100
    assert (d.restore_originals, d.optional_restore, d.prepared_lever) == (("heat_limit",), ("heat_limit",), "room_setpoint")
    assert d.labels == {
        "curve": "Heizkurve", "room_setpoint": "Wunschtemperatur der Zone", "heat_limit": "Heizgrenze",
        "min_flow": "Mindestvorlauftemperatur",
    }
    assert d.preparation_label == "Betriebsart der Zone"
    assert BINDINGS == {"vaillant_vrc720": VAILLANT_MYPYLLANT}


@pytest.mark.parametrize(("change", "match"), [
    ({"steps": {"curve": 0.05}}, "Schrittweite"),
    ({"enforce_tolerance": {"curve": 0.025}}, "Toleranz"),
    ({"labels": {}}, "Anzeigename"),
    ({"restore_originals": ("level",)}, "restore_originals"),
    ({"optional_restore": ("min_flow",)}, "optional_restore"),
    ({"prepared_lever": "level"}, "prepared_lever"),
    ({"settle_seconds": 0}, "Wartezeit"),
])
def test_description_is_validated(change, match):
    with pytest.raises(ValueError, match=match):
        dataclasses.replace(VAILLANT_MYPYLLANT, **change)
