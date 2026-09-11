import pytest

from heizungsbruecke.profiles import UnknownProfileError, required_roles_for


def test_required_roles_for_weishaupt_profile():
    roles = required_roles_for("weishaupt_waermepumpe_fussbodenheizung")

    assert set(roles) == {
        "room_actual", "room_target", "curve_current", "offset_current",
        "room_day_avg", "room_night_avg", "heat_limit", "dat", "dart",
    }


def test_required_roles_for_vaillant_profile():
    roles = required_roles_for("vaillant_gastherme_heizkoerper")

    assert set(roles) == {
        "room_actual", "room_target", "curve_current", "offset_current",
        "room_day_avg", "room_night_avg", "heat_limit", "dat", "dart",
    }


def test_required_roles_for_unknown_profile_raises():
    with pytest.raises(UnknownProfileError):
        required_roles_for("does_not_exist")
