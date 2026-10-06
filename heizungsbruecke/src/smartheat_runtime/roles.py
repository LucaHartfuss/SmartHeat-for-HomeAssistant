"""Hostneutrales Rollen-Vokabular (Spec SHG 3.3): welche Signale und Hebel die Laufzeit kennt und welche je Hebelsatz
Pflicht sind. ChannelManifest bildet Rolle -> Referenz der SignalSource ab (HA: Entity-ID bzw. entity::attribut)."""
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
    "energy_electrical_total",
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


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class ChannelManifest:
    """Rolle -> Referenz der SignalSource."""

    refs: dict[str, str]
