import pytest

from heizungsbruecke.manifest import ALL_ROLES
from heizungsbruecke.profiles import (
    LOCAL_BOOST_DEFAULTS,
    LOCAL_CLAMP_DEFAULTS,
    PROFILE_CATALOG,
    PROFILE_LABELS,
    REQUIRED_ROLES_BY_PROFILE,
    UnknownProfileError,
    is_verified,
    required_roles_for,
    resolve_boost_defaults,
    resolve_local_clamps,
)


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


def test_profile_labels_and_required_roles_cover_the_same_profiles():
    assert PROFILE_LABELS.keys() == REQUIRED_ROLES_BY_PROFILE.keys()


def test_every_required_role_is_a_known_manifest_role():
    for profile_id, roles in REQUIRED_ROLES_BY_PROFILE.items():
        unknown = set(roles) - set(ALL_ROLES)
        assert not unknown, f"Profil '{profile_id}' fordert unbekannte Rollen: {sorted(unknown)}"


def test_resolve_local_clamps_returns_profile_defaults():
    clamps = resolve_local_clamps("vaillant_gastherme_heizkoerper")

    assert clamps == LOCAL_CLAMP_DEFAULTS["vaillant_gastherme_heizkoerper"]


def test_resolve_local_clamps_raises_for_profile_without_defaults():
    with pytest.raises(UnknownProfileError):
        resolve_local_clamps("weishaupt_waermepumpe_fussbodenheizung")


def test_resolve_boost_defaults_returns_profile_values():
    defaults = resolve_boost_defaults("vaillant_gastherme_heizkoerper")

    assert defaults.threshold_k == 0.5
    assert defaults.curve_value == 1.5
    assert defaults.offset_value == 30.0


def test_resolve_boost_defaults_raises_for_profile_without_defaults():
    with pytest.raises(UnknownProfileError):
        resolve_boost_defaults("weishaupt_waermepumpe_fussbodenheizung")


def test_profile_catalog_entries_have_known_profile_ids():
    for entry in PROFILE_CATALOG:
        assert entry.profile_id in REQUIRED_ROLES_BY_PROFILE


def test_is_verified_true_for_profile_with_full_defaults():
    assert is_verified("vaillant_gastherme_heizkoerper") is True


def test_is_verified_false_for_profile_without_defaults():
    assert is_verified("weishaupt_waermepumpe_fussbodenheizung") is False


def test_is_verified_false_for_unknown_profile():
    assert is_verified("nicht_vorhanden") is False
