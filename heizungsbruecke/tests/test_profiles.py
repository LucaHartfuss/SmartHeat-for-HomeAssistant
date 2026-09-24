import pytest

from heizungsbruecke.manifest import ALL_ROLES
from heizungsbruecke.profiles import (
    KPI_ENERGY_CHANNELS_BY_PROFILE,
    LOCAL_BOOST_DEFAULTS,
    LOCAL_CLAMP_DEFAULTS,
    LOCAL_WINDOW_DEFAULTS,
    REQUIRED_ROLES_BY_PROFILE,
    UnknownProfileError,
    WindowDefaults,
    required_roles_for,
    resolve_boost_defaults,
    resolve_local_clamps,
    resolve_window_defaults,
    window_size_hours,
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


def test_resolve_window_defaults_returns_profile_defaults():
    windows = resolve_window_defaults("vaillant_gastherme_heizkoerper")

    assert windows == LOCAL_WINDOW_DEFAULTS["vaillant_gastherme_heizkoerper"]


def test_resolve_window_defaults_raises_for_profile_without_defaults():
    with pytest.raises(UnknownProfileError):
        resolve_window_defaults("weishaupt_waermepumpe_fussbodenheizung")


def test_window_size_hours_computes_difference():
    assert window_size_hours("14:00", "17:00") == 3.0
    assert window_size_hours("04:00", "07:00") == 3.0


def test_vaillant_window_defaults_match_daily_trigger_time():
    windows = resolve_window_defaults("vaillant_gastherme_heizkoerper")

    assert windows.daily_trigger_time == "12:00"


def test_resolve_window_defaults_rejects_mismatched_window_sizes(monkeypatch):
    # Guard: day and night windows are read from the SAME rolling statistics sensor
    # (see derived_sensors.py) -- one max_age_hours value must serve both.
    monkeypatch.setitem(
        LOCAL_WINDOW_DEFAULTS, "vaillant_gastherme_heizkoerper",
        WindowDefaults(
            daily_trigger_time="12:00",
            day_avg_window_start="14:00", day_avg_window_end="18:00",  # 4h
            night_avg_window_start="04:00", night_avg_window_end="07:00",  # 3h
        ),
    )

    with pytest.raises(UnknownProfileError):
        resolve_window_defaults("vaillant_gastherme_heizkoerper")


def test_resolve_window_defaults_rejects_non_positive_window(monkeypatch):
    monkeypatch.setitem(
        LOCAL_WINDOW_DEFAULTS, "vaillant_gastherme_heizkoerper",
        WindowDefaults(
            daily_trigger_time="12:00",
            day_avg_window_start="17:00", day_avg_window_end="14:00",  # inverted
            night_avg_window_start="04:00", night_avg_window_end="07:00",
        ),
    )

    with pytest.raises(UnknownProfileError):
        resolve_window_defaults("vaillant_gastherme_heizkoerper")


def test_vaillant_gastherme_has_kpi_energy_channels():
    assert KPI_ENERGY_CHANNELS_BY_PROFILE["vaillant_gastherme_heizkoerper"] == (
        "electrical_heating", "electrical_dhw",
        "primary_heating", "primary_dhw",
        "thermal_heating", "thermal_dhw",
    )
