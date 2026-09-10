from dataclasses import dataclass

REQUIRED_ROLES = ("room_actual", "room_target", "curve_current", "offset_current")
OPTIONAL_ROLES = ("outdoor_temp", "room_day_avg", "room_night_avg", "heat_limit", "dat", "dart")
ALL_ROLES = REQUIRED_ROLES + OPTIONAL_ROLES


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class ChannelManifest:
    entity_ids: dict[str, str]


def build_manifest(options: dict) -> ChannelManifest:
    entity_ids = {}
    for role in ALL_ROLES:
        value = options.get(f"entity_{role}")
        if value:
            entity_ids[role] = value

    missing = [role for role in REQUIRED_ROLES if role not in entity_ids]
    if missing:
        raise ManifestError(f"Pflicht-Rollen fehlen in der Add-on-Konfiguration: {', '.join(missing)}")

    return ChannelManifest(entity_ids=entity_ids)
