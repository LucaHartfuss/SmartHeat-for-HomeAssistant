import re
from pathlib import Path

import pytest

from heizungsbruecke.manifest import ALL_ROLES
from heizungsbruecke.profiles import (
    PROFILE_LABELS,
    REQUIRED_ROLES_BY_PROFILE,
    UnknownProfileError,
    required_roles_for,
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
