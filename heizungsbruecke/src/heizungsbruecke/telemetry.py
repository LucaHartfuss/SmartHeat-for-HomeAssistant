"""KPI-Telemetrie: reine Beobachtung auf smartheat/<tenant>/telemetry, ohne Einfluss auf die
Regelung. Den Takt gibt der Planeintrag EV_TELEMETRY vor."""
import logging
import math
from datetime import datetime

logger = logging.getLogger(__name__)

# Optionale KPI-Rollen, nur wenn gemappt. Nicht lesbare Sensoren werden weggelassen, nie als
# null/0 gesendet: der Server speichert fuer fehlende Felder NULL.
KPI_NUMERIC_ROLES = (
    "flow_temperature", "return_temperature", "system_water_pressure", "efficiency_ratio",
)
KPI_ENERGY_ROLES = (
    "energy_electrical_heating", "energy_electrical_dhw",
    "energy_primary_heating", "energy_primary_dhw",
    "energy_thermal_heating", "energy_thermal_dhw",
)


def run_telemetry_tick(manifest, ha_api, mqtt_client, boost_active: bool, failsafe_active: bool) -> None:
    """Liest room_actual selbst (lokaler HA-REST-Aufruf, kein Cloud-Roundtrip). Wirft nie."""
    if "room_actual" not in manifest.entity_ids:
        return
    try:
        room_actual = ha_api.get_state(manifest.entity_ids["room_actual"])
        publish_telemetry(
            mqtt_client=mqtt_client, room_actual=room_actual,
            boost_active=boost_active, failsafe_active=failsafe_active,
            kpi_fields=read_kpi_fields(manifest, ha_api),
        )
    except Exception:
        logger.exception("Fehler beim Veroeffentlichen der KPI-Telemetrie, wird beim naechsten Tick erneut versucht")


def publish_telemetry(
    mqtt_client, room_actual: float, boost_active: bool, failsafe_active: bool, kpi_fields: dict | None = None,
) -> None:
    mqtt_client.publish_telemetry({
        "room_actual": room_actual,
        "boost_active": boost_active,
        "failsafe_active": failsafe_active,
        "ts": datetime.now().isoformat(),
        **(kpi_fields or {}),
    })


def read_kpi_fields(manifest, ha_api) -> dict:
    """Jeder Sensor fuer sich: ein nicht lesbarer oder nicht endlicher Wert wird mit WARNING
    weggelassen und blockiert weder die Kern-Telemetrie noch die anderen Sensoren. `energy`
    gibt es nur, wenn mindestens ein Kanal lesbar war."""
    kpi_fields: dict = {}

    def _read(role, reader):
        try:
            value = reader(manifest.entity_ids[role])
            # "nan"/"inf" ueberstehen float(), der Server lehnt nicht-endliche Zahlen aber
            # fuer die ganze Nachricht ab.
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"nicht-endlicher Wert {value!r}")
            return True, value
        except Exception as exc:
            logger.warning("KPI-Sensor '%s' nicht lesbar, Feld wird weggelassen: %s", role, exc)
            return False, None

    for role in KPI_NUMERIC_ROLES:
        if role in manifest.entity_ids:
            ok, value = _read(role, ha_api.get_state)
            if ok:
                kpi_fields[role] = value
    if "operating_mode" in manifest.entity_ids:
        ok, value = _read("operating_mode", ha_api.get_raw_state)
        if ok:
            kpi_fields["operating_mode"] = value

    energy: dict = {}
    for role in KPI_ENERGY_ROLES:
        if role in manifest.entity_ids:
            ok, value = _read(role, ha_api.get_state)
            if ok:
                energy[role.removeprefix("energy_")] = value
    if energy:
        kpi_fields["energy"] = energy
    return kpi_fields
