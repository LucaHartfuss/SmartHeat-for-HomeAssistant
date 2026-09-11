from dataclasses import dataclass

from heizungsbruecke.profiles import UnknownProfileError, required_roles_for

ALL_ROLES = (
    "room_actual", "room_target", "curve_current", "offset_current",
    "outdoor_temp", "room_day_avg", "room_night_avg", "heat_limit", "dat", "dart",
)


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class ChannelManifest:
    entity_ids: dict[str, str]


def build_manifest(options: dict) -> ChannelManifest:
    profile_id = options.get("profile")
    if not profile_id:
        raise ManifestError("Pflichtfeld 'profile' fehlt in der Add-on-Konfiguration")

    try:
        required_roles = required_roles_for(profile_id)
    except UnknownProfileError as exc:
        raise ManifestError(str(exc)) from exc

    entity_ids = {}
    for role in ALL_ROLES:
        value = options.get(f"entity_{role}")
        if value:
            entity_ids[role] = value

    missing = [role for role in required_roles if role not in entity_ids]
    if missing:
        raise ManifestError(f"Pflicht-Rollen fehlen in der Add-on-Konfiguration: {', '.join(missing)}")

    return ChannelManifest(entity_ids=entity_ids)
