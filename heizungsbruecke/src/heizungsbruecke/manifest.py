from dataclasses import dataclass

ALL_ROLES = (
    "room_actual", "room_target", "curve_current", "shift_current", "min_flow",
    "outdoor_temp", "heat_limit", "flow_setpoint",
    # Plan 3b: Niveau (Viessmann), Betriebsart/Heizprogramm und Hilfs-Sollwerte (Weishaupt). Vor flow_temperature, weil
    # test_config_yaml die KPI-Rollen ab dort zaehlt.
    "level_current", "mode_select", "setpoint_comfort", "setpoint_setback",
    "flow_temperature", "return_temperature", "operating_mode", "system_water_pressure",
    "efficiency_ratio", "energy_electrical_heating", "energy_electrical_dhw",
    "energy_primary_heating", "energy_primary_dhw", "energy_thermal_heating", "energy_thermal_dhw",
)

# Pflicht-Entities der Bruecke (fuer alle Profile gleich). room_actual (und bei einer weather-Quelle
# outdoor_temp) kommen aus derived_sensors.
REQUIRED_ROLES = (
    "room_actual", "room_target", "curve_current", "shift_current", "min_flow", "heat_limit", "outdoor_temp",
)

# Plan 3b: Pflicht-Rollen je Hebelsatz (Spec 5.5 "Pflicht je Hebelsatz"); Vaillant = REQUIRED_ROLES. Bei
# weishaupt_wwp_basis sind curve_current/heat_limit optional (curve_current nur lesend, Snapshot readonly).
REQUIRED_ROLES_BY_LEVER_SET: dict[str, tuple[str, ...]] = {
    "vaillant_vrc720": REQUIRED_ROLES,
    "weishaupt_wwp": (
        "room_actual", "room_target", "curve_current", "shift_current", "heat_limit", "outdoor_temp", "mode_select",
        "setpoint_comfort", "setpoint_setback",
    ),
    "weishaupt_wwp_basis": (
        "room_actual", "room_target", "shift_current", "outdoor_temp", "mode_select", "setpoint_comfort",
        "setpoint_setback",
    ),
    "viessmann_vicare": (
        "room_actual", "room_target", "curve_current", "level_current", "shift_current", "outdoor_temp", "mode_select",
    ),
}

# Attribut der Zonen-Wunschtemperatur einer Climate-Entity (ha_api.get_state liest "entity::attribut").
CLIMATE_TARGET_ATTRIBUTE = "temperature"


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class ChannelManifest:
    entity_ids: dict[str, str]


def entity_ref(role: str, value: str) -> str:
    """Referenz, unter der die Bruecke die Rolle liest: eine Climate-Zone als Parallelverschiebung
    wird ueber ihr Attribut `temperature` gelesen."""
    if role == "shift_current" and value.startswith("climate.") and "::" not in value:
        return f"{value}::{CLIMATE_TARGET_ATTRIBUTE}"
    return value


def build_manifest(
    options: dict, derived_entity_ids: dict[str, str] | None = None, lever_set_id: str = "vaillant_vrc720",
) -> ChannelManifest:
    derived_entity_ids = derived_entity_ids or {}
    entity_ids = {}
    for role in ALL_ROLES:
        if role in derived_entity_ids:
            entity_ids[role] = derived_entity_ids[role]
            continue
        value = options.get(f"entity_{role}")
        if value:
            entity_ids[role] = entity_ref(role, value)

    missing = [role for role in REQUIRED_ROLES_BY_LEVER_SET[lever_set_id] if role not in entity_ids]
    if missing:
        raise ManifestError(f"Pflicht-Rollen fehlen in der Add-on-Konfiguration: {', '.join(missing)}")

    return ChannelManifest(entity_ids=entity_ids)
