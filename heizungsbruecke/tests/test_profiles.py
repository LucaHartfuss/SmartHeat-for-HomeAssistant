import re
from pathlib import Path

import pytest

from heizungsbruecke.manifest import ALL_ROLES
from heizungsbruecke.profiles import (
    LOCAL_CLAMP_DEFAULTS,
    PROFILE_LABELS,
    REQUIRED_ROLES_BY_PROFILE,
    UnknownProfileError,
    required_roles_for,
    resolve_local_clamps,
)

CONFIG_YAML = Path(__file__).resolve().parents[1] / "config.yaml"


def _profile_ids_from_config_yaml() -> set[str]:
    """Extracts the members of config.yaml's `schema.profile` dropdown, which has the
    form `list(a|b|c)`. Deliberately a plain string operation instead of a real YAML
    parse: PyYAML is not a dependency of this add-on and adding one just to read a
    single line would be a heavier price than this regex.
    """
    text = CONFIG_YAML.read_text(encoding="utf-8")
    match = re.search(r'^\s*profile:\s*"?list\(([^)]*)\)"?\s*$', text, re.MULTILINE)
    assert match is not None, "config.yaml enthaelt kein schema.profile der Form list(...)"
    return {member.strip() for member in match.group(1).split("|")}


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


def test_config_yaml_dropdown_matches_profile_registry():
    assert _profile_ids_from_config_yaml() == set(REQUIRED_ROLES_BY_PROFILE)


def test_every_required_role_is_a_known_manifest_role():
    for profile_id, roles in REQUIRED_ROLES_BY_PROFILE.items():
        unknown = set(roles) - set(ALL_ROLES)
        assert not unknown, f"Profil '{profile_id}' fordert unbekannte Rollen: {sorted(unknown)}"


def test_resolve_local_clamps_uses_defaults_when_options_empty():
    clamps = resolve_local_clamps("vaillant_gastherme_heizkoerper", {})

    assert clamps == LOCAL_CLAMP_DEFAULTS["vaillant_gastherme_heizkoerper"]


def test_resolve_local_clamps_applies_partial_override():
    clamps = resolve_local_clamps("vaillant_gastherme_heizkoerper", {"offset_max": 28.0})

    assert clamps.offset_max == 28.0
    assert clamps.curve_min == LOCAL_CLAMP_DEFAULTS["vaillant_gastherme_heizkoerper"].curve_min


def test_resolve_local_clamps_ignores_falsy_option_values():
    # 0.0 ist fuer keinen dieser vier Werte ein plausibler echter Wert -- config.yaml
    # nutzt 0.0 konsistent mit dem bestehenden Muster (z.B. entity_outdoor_temp: "")
    # als Sentinel fuer "nicht gesetzt".
    clamps = resolve_local_clamps("vaillant_gastherme_heizkoerper", {"curve_min": 0.0})

    assert clamps.curve_min == LOCAL_CLAMP_DEFAULTS["vaillant_gastherme_heizkoerper"].curve_min


def test_resolve_local_clamps_raises_for_profile_without_defaults_and_missing_fields():
    with pytest.raises(UnknownProfileError):
        resolve_local_clamps("weishaupt_waermepumpe_fussbodenheizung", {})


def test_resolve_local_clamps_succeeds_for_profile_without_defaults_when_fully_overridden():
    options = {"curve_min": 0.2, "curve_max": 0.8, "offset_min": 0.0, "offset_max": 5.0}

    clamps = resolve_local_clamps("weishaupt_waermepumpe_fussbodenheizung", options)

    assert clamps.curve_min == 0.2
    assert clamps.offset_max == 5.0
