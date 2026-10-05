"""KPI- und Regel-Telemetrie auf smartheat/<tenant>/telemetry. Seit TP11 wertet der Server daraus
Regelabweichung und Betriebspunkt aus (room_target, outdoor_temp, flow_setpoint); fehlende Werte
fuehren dort nur zu 'nicht lernen'. Den Takt gibt der Planeintrag EV_TELEMETRY vor."""
import logging
import math
from collections.abc import Callable

from heizungsbruecke.delivery import DataFault
from smartheat_core import energy as energy_core
from smartheat_core import wallclock
from smartheat_core.binding import ENERGY_TOTAL

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
    "energy_electrical_total",
)

# Regel-Telemetrie fuer den Regelkern (TP11). Gegenstueck: heizungsserver
# generic/history.TELEMETRY_REGULATION_FIELDS (Contract-Check 22).
REGULATION_FIELDS = ("room_target", "outdoor_temp", "flow_setpoint")

#: Anstehender Datenfehler fuer den Health-Check des Servers (nur solange einer besteht).
#: Feldname und Quellen prueft der Contract-Check 21 gegen heizungsserver.generic.history.
DATENFEHLER_KEY = "datenfehler"

#: Telemetrie-Flag "Therme liefert trotz Anforderung keine Waerme" (TP12f). Feldname prueft der
#: Contract-Check 27 gegen heizungsserver.generic.history.TELEMETRY_WAERME_KEY.
WAERME_FEHLT_KEY = "waerme_fehlt"


def run_telemetry_tick(
    manifest, ha_api, mqtt_client, boost_active: bool, failsafe_active: bool,
    datenfehler: DataFault | None = None, room_target: float | None = None,
    waerme: Callable[[float, dict, dict], bool] | None = None,
    energy: Callable[[dict], dict] | None = None,
) -> None:
    """Liest room_actual selbst (lokaler HA-REST-Aufruf, kein Cloud-Roundtrip). `energy` normalisiert die
    Energie-Rohwerte (Plan 3b, Tageszaehler; None = unveraendert). Wirft nie."""
    if "room_actual" not in manifest.entity_ids:
        return
    try:
        room_actual = ha_api.get_state(manifest.entity_ids["room_actual"])
        kpi_fields = read_kpi_fields(manifest, ha_api, energy)
        regulation_fields = read_regulation_fields(manifest, ha_api, room_target)
        waerme_fehlt = False if waerme is None else waerme(room_actual, kpi_fields, regulation_fields)
        publish_telemetry(
            mqtt_client=mqtt_client, room_actual=room_actual,
            boost_active=boost_active, failsafe_active=failsafe_active,
            kpi_fields=kpi_fields, datenfehler=datenfehler, regulation_fields=regulation_fields,
            waerme_fehlt=waerme_fehlt,
        )
    except Exception:
        logger.exception("Fehler beim Veroeffentlichen der KPI-Telemetrie, wird beim naechsten Tick erneut versucht")


def publish_telemetry(
    mqtt_client, room_actual: float, boost_active: bool, failsafe_active: bool, kpi_fields: dict | None = None,
    datenfehler: DataFault | None = None, regulation_fields: dict | None = None,
    waerme_fehlt: bool = False,
) -> None:
    payload = {
        "room_actual": room_actual,
        "boost_active": boost_active,
        "failsafe_active": failsafe_active,
        WAERME_FEHLT_KEY: waerme_fehlt,
        "ts": wallclock.now().isoformat(),
        **(kpi_fields or {}),
        **(regulation_fields or {}),
    }
    if datenfehler is not None:
        payload[DATENFEHLER_KEY] = {"source": datenfehler.source, "detail": list(datenfehler.detail)}
    mqtt_client.publish_telemetry(payload)


def read_regulation_fields(manifest, ha_api, room_target: float | None) -> dict:
    """Aussentemperatur und Vorlauf-Soll lesen (nur gemappte, nur endliche Werte) plus das
    stabile Raum-Soll. Ein nicht lesbarer Wert fehlt, er wird nie als 0 gesendet."""
    fields: dict = {}
    if room_target is not None and math.isfinite(room_target):
        fields["room_target"] = room_target
    for role in ("outdoor_temp", "flow_setpoint"):
        entity_id = manifest.entity_ids.get(role)
        if entity_id is None:
            continue
        try:
            value = ha_api.get_state(entity_id)
        except Exception as exc:
            logger.warning("Regel-Telemetrie '%s' nicht lesbar, Feld wird weggelassen: %s", role, exc)
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            fields[role] = value
    return fields


def read_kpi_fields(manifest, ha_api, normalize_energy: Callable[[dict], dict] | None = None) -> dict:
    """Jeder Sensor fuer sich: ein nicht lesbarer oder nicht endlicher Wert wird mit WARNING
    weggelassen und blockiert weder die Kern-Telemetrie noch die anderen Sensoren. `energy`
    gibt es nur, wenn mindestens ein Kanal lesbar war. normalize_energy (Plan 3b) macht aus Tageszaehlern
    monoton wachsende Summen unter denselben Kanalnamen."""
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
    if energy and normalize_energy is not None:
        energy = normalize_energy(energy)
    if energy:
        kpi_fields["energy"] = energy
    return kpi_fields


def energy_normalizer(store, kind: str) -> Callable[[dict], dict] | None:
    """Normalisierer fuer die Zaehlerart des Bindings (BindingDescription.energy_counters); None fuer Summenzaehler
    (Vaillant: unveraendert, kein Zustand). Der Zustand liegt in BridgeState.energy_state (backup.json); scheitert das
    Speichern, gilt er im Speicher weiter (StateStore.update aendert vor dem Schreiben)."""
    if kind == ENERGY_TOTAL:
        return None

    def _normalize(raw: dict) -> dict:
        sent, state = energy_core.normalize(kind, store.state.energy_state, raw)
        if state != store.state.energy_state:
            try:
                store.update(energy_state=state)
            except Exception as error:
                logger.warning("Energie-Zaehlerstand nicht gespeichert (%s), gilt bis zum Neustart", error)
        return sent

    return _normalize
