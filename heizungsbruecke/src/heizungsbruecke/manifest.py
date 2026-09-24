from dataclasses import dataclass

from heizungsbruecke.profiles import UnknownProfileError, required_roles_for

ALL_ROLES = (
    "room_actual", "room_target", "curve_current", "offset_current",
    "outdoor_temp", "room_day_avg", "room_night_avg", "heat_limit", "dat", "dart",
    "outdoor_min_24h",
    "flow_temperature", "return_temperature", "operating_mode", "system_water_pressure",
    "efficiency_ratio", "energy_electrical_heating", "energy_electrical_dhw",
    "energy_primary_heating", "energy_primary_dhw", "energy_thermal_heating", "energy_thermal_dhw",
)

# Rollen, die der volle Snapshot an den Server schickt -- muss mit REQUIRED_ROLES in
# heizungsserver/generic/messages.py uebereinstimmen. room_actual/outdoor_temp und die
# optionalen KPI-Rollen sind rein lokal bzw. laufen ueber die Telemetrie.
SNAPSHOT_ROLES = (
    "heat_limit", "dat", "room_target", "dart",
    "room_day_avg", "room_night_avg", "curve_current", "offset_current",
)

# Optionale Snapshot-Rollen (Server: OPTIONAL_ROLES in heizungsserver/generic/messages.py).
# room_target_avg_24h ist keine Entity, sondern wird im Add-on berechnet (target_history.py).
OPTIONAL_SNAPSHOT_ROLES = ("outdoor_min_24h", "room_target_avg_24h")


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class ChannelManifest:
    entity_ids: dict[str, str]


def build_manifest(options: dict, derived_entity_ids: dict[str, str] | None = None) -> ChannelManifest:
    profile_id = options.get("profile")
    if not profile_id:
        raise ManifestError("Pflichtfeld 'profile' fehlt in der Add-on-Konfiguration")

    try:
        required_roles = required_roles_for(profile_id)
    except UnknownProfileError as exc:
        raise ManifestError(str(exc)) from exc

    derived_entity_ids = derived_entity_ids or {}
    entity_ids = {}
    for role in ALL_ROLES:
        if role in derived_entity_ids:
            entity_ids[role] = derived_entity_ids[role]
            continue
        value = options.get(f"entity_{role}")
        if value:
            entity_ids[role] = value

    missing = [role for role in required_roles if role not in entity_ids]
    if missing:
        raise ManifestError(f"Pflicht-Rollen fehlen in der Add-on-Konfiguration: {', '.join(missing)}")

    return ChannelManifest(entity_ids=entity_ids)
