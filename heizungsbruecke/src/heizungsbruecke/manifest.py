from dataclasses import dataclass

ALL_ROLES = (
    "room_actual", "room_target", "curve_current", "shift_current", "min_flow",
    "outdoor_temp", "heat_limit", "flow_setpoint",
    "flow_temperature", "return_temperature", "operating_mode", "system_water_pressure",
    "efficiency_ratio", "energy_electrical_heating", "energy_electrical_dhw",
    "energy_primary_heating", "energy_primary_dhw", "energy_thermal_heating", "energy_thermal_dhw",
)

# Pflicht-Entities der Bruecke (fuer alle Profile gleich). room_actual (und bei einer weather-Quelle
# outdoor_temp) kommen aus derived_sensors.
REQUIRED_ROLES = (
    "room_actual", "room_target", "curve_current", "shift_current", "min_flow", "heat_limit", "outdoor_temp",
)

# Rollen des Snapshots an den Server -- muss mit REQUIRED_ROLES in heizungsserver/generic/messages.py
# uebereinstimmen (Contract-Check 4). min_flow ist rein lokal (= Raum-Soll), outdoor_temp und
# flow_setpoint laufen ueber die Telemetrie.
SNAPSHOT_ROLES = ("heat_limit", "room_target", "curve_current", "shift_current")

# Optionale Snapshot-Rollen (Server: OPTIONAL_ROLES). Seit TP11 keine mehr.
OPTIONAL_SNAPSHOT_ROLES: tuple[str, ...] = ()

# Attribut der Zonen-Wunschtemperatur einer Climate-Entity (ha_api.get_state liest "entity::attribut").
CLIMATE_TARGET_ATTRIBUTE = "temperature"


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class ChannelManifest:
    entity_ids: dict[str, str]


def build_manifest(options: dict, derived_entity_ids: dict[str, str] | None = None) -> ChannelManifest:
    derived_entity_ids = derived_entity_ids or {}
    entity_ids = {}
    for role in ALL_ROLES:
        if role in derived_entity_ids:
            entity_ids[role] = derived_entity_ids[role]
            continue
        value = options.get(f"entity_{role}")
        if value and role == "shift_current" and value.startswith("climate.") and "::" not in value:
            value = f"{value}::{CLIMATE_TARGET_ATTRIBUTE}"
        if value:
            entity_ids[role] = value

    missing = [role for role in REQUIRED_ROLES if role not in entity_ids]
    if missing:
        raise ManifestError(f"Pflicht-Rollen fehlen in der Add-on-Konfiguration: {', '.join(missing)}")

    return ChannelManifest(entity_ids=entity_ids)
