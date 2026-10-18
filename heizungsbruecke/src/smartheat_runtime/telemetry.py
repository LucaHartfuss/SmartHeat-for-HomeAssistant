"""KPI- und Regel-Telemetrie auf smartheat/<geraete_id>/telemetry. Seit TP11 wertet der Server daraus
Regelabweichung und Betriebspunkt aus (room_target, outdoor_temp, flow_setpoint); fehlende Werte
fuehren dort nur zu 'nicht lernen'. Den Takt gibt der Planeintrag EV_TELEMETRY vor."""
import logging
import math
from collections.abc import Callable, Mapping
from typing import Any

from smartheat_core import energy as energy_core
from smartheat_core import wallclock
from smartheat_core.binding import ENERGY_TOTAL
from smartheat_core.bindungsparameter import ENERGIE_KANAELE
from smartheat_core.zahlen import ist_endliche_zahl
from smartheat_runtime.delivery import DataFault

logger = logging.getLogger(__name__)

# Optionale KPI-Rollen, nur wenn gemappt. Nicht lesbare Sensoren werden weggelassen, nie als
# null/0 gesendet: der Server speichert fuer fehlende Felder NULL.
KPI_NUMERIC_ROLES = (
    "flow_temperature", "return_temperature", "system_water_pressure", "efficiency_ratio",
    "generator_hours", "generator_starts",
)
# Text-Rollen (Rohzustand, ohne Zahlumwandlung): Betriebsart und Status des Waermeerzeugers.
KPI_TEXT_ROLES = ("operating_mode", "generator_state")
#: Liefer-Signale fuer die Waermelieferung auf dem Server (Audit 4 P-B). Gegenstueck: heizungsserver
#: regelung/history.TELEMETRY_DELIVERY_FIELDS (Contract-Check (Vertrag V1b)).
DELIVERY_ROLES = ("generator_hours", "generator_starts", "generator_state")
# Die Liefer-Signale gehen ueber die KPI-Rollen hinaus (read_kpi_fields); ein Liefer-Signal ohne Leser haelt den Import an.
if not set(DELIVERY_ROLES) <= {*KPI_NUMERIC_ROLES, *KPI_TEXT_ROLES}:
    raise RuntimeError("telemetry: DELIVERY_ROLES nennt eine Rolle ausserhalb der KPI-Rollen")
KPI_ENERGY_ROLES = tuple(f"energy_{kanal}" for kanal in ENERGIE_KANAELE)

#: Kanal -> zuletzt gemeldete fremde Einheit: eine Warnung je Wechsel, nicht je Tick (Spec Stufe 3 S4b 5).
_EINHEIT_GEWARNT: dict[str, str] = {}

# Regel-Telemetrie fuer den Regelkern (TP11). Gegenstueck: heizungsserver
# regelung/history.TELEMETRY_REGULATION_FIELDS (Contract-Check (Vertrag V1b)).
REGULATION_FIELDS = ("room_target", "outdoor_temp", "flow_setpoint")

#: Anstehender Datenfehler fuer den Health-Check des Servers (nur solange einer besteht).
#: Feldname und Quellen prueft ein Contract-Check (Vertrag V1b) gegen heizungsserver.regelung.history.
DATENFEHLER_KEY = "datenfehler"


def run_telemetry_tick(
    manifest, signals, mqtt_client, boost_active: bool, failsafe_active: bool,
    datenfehler: DataFault | None = None, room_target: float | None = None,
    energy: Callable[[dict, dict], dict] | None = None, einheiten: Mapping[str, str] | None = None,
) -> None:
    """Liest room_actual selbst (lokaler HA-REST-Aufruf, kein Cloud-Roundtrip). `energy` normalisiert die
    Energie-Rohwerte (Plan 3b, Tageszaehler; None = unveraendert), `einheiten` ist je Energiekanal die erwartete
    Einheit (Spec Stufe 3 S4b 5; ohne Eintrag kein Kanal). Wirft nie."""
    if "room_actual" not in manifest.refs:
        return
    try:
        room_actual = signals.get_state(manifest.refs["room_actual"])
        if not ist_endliche_zahl(room_actual):
            # Pflichtfeld der Telemetrie (der Server lehnt sie sonst ab, Testoffensive T2, Nr. 17): kein Senden, der
            # naechste Tick liest erneut
            logger.warning("Telemetrie nicht gesendet: room_actual ist keine endliche Zahl")
            return
        kpi_fields = read_kpi_fields(manifest, signals, energy, einheiten=einheiten)
        regulation_fields = read_regulation_fields(manifest, signals, room_target)
        publish_telemetry(
            mqtt_client=mqtt_client, room_actual=room_actual,
            boost_active=boost_active, failsafe_active=failsafe_active,
            kpi_fields=kpi_fields, datenfehler=datenfehler, regulation_fields=regulation_fields,
        )
    except Exception:
        logger.exception("Fehler beim Veroeffentlichen der KPI-Telemetrie, wird beim naechsten Tick erneut versucht")


def publish_telemetry(
    mqtt_client, room_actual: float, boost_active: bool, failsafe_active: bool, kpi_fields: dict | None = None,
    datenfehler: DataFault | None = None, regulation_fields: dict | None = None,
) -> None:
    payload = {
        "room_actual": room_actual,
        "boost_active": boost_active,
        "failsafe_active": failsafe_active,
        "ts": wallclock.now().isoformat(),
        **(kpi_fields or {}),
        **(regulation_fields or {}),
    }
    if datenfehler is not None:
        payload[DATENFEHLER_KEY] = {"source": datenfehler.source, "detail": list(datenfehler.detail)}
    mqtt_client.publish_telemetry(payload)


def read_regulation_fields(manifest, signals, room_target: float | None) -> dict:
    """Aussentemperatur und Vorlauf-Soll lesen (nur gemappte, nur endliche Werte) plus das
    stabile Raum-Soll. Ein nicht lesbarer Wert fehlt, er wird nie als 0 gesendet."""
    fields: dict = {}
    if room_target is not None and math.isfinite(room_target):
        fields["room_target"] = room_target
    for role in (rolle for rolle in REGULATION_FIELDS if rolle != "room_target"):  # room_target ist das Argument
        entity_id = manifest.refs.get(role)
        if entity_id is None:
            continue
        try:
            value = signals.get_state(entity_id)
        except Exception as exc:
            logger.warning("Regel-Telemetrie '%s' nicht lesbar, Feld wird weggelassen: %s", role, exc)
            continue
        if ist_endliche_zahl(value):
            fields[role] = value
    return fields


def read_kpi_fields(
    manifest, signals, normalize_energy: Callable[[dict, dict], dict] | None = None,
    einheiten: Mapping[str, str] | None = None,
) -> dict:
    """Jeder Sensor fuer sich: ein nicht lesbarer oder nicht endlicher Wert wird mit WARNING
    weggelassen und blockiert weder die Kern-Telemetrie noch die anderen Sensoren. `energy`
    gibt es nur, wenn mindestens ein Kanal lesbar war. Ein Energiekanal wird nur gelesen, wenn `einheiten` seine
    erwartete Einheit nennt, und nur gesendet, wenn die Entity genau diese meldet (Wh wird in kWh umgerechnet); sonst
    fehlt er (eine Warnung je Wechsel). normalize_energy(werte, zieleinheiten) (Plan 3b) macht aus Tageszaehlern
    monoton wachsende Summen unter denselben Kanalnamen."""
    einheiten = einheiten or {}
    kpi_fields: dict = {}

    def _read(role, reader) -> tuple[bool, Any]:
        try:
            value = reader(manifest.refs[role])
            # "nan"/"inf" ueberstehen float(), der Server lehnt nicht-endliche Zahlen aber
            # fuer die ganze Nachricht ab.
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"nicht-endlicher Wert {value!r}")
            return True, value
        except Exception as exc:
            logger.warning("KPI-Sensor '%s' nicht lesbar, Feld wird weggelassen: %s", role, exc)
            return False, None

    for role in KPI_NUMERIC_ROLES:
        if role in manifest.refs:
            ok, value = _read(role, signals.get_state)
            if ok:
                kpi_fields[role] = value
    for role in KPI_TEXT_ROLES:
        if role in manifest.refs:
            ok, value = _read(role, signals.get_raw_state)
            if ok:
                kpi_fields[role] = value

    energy: dict = {}
    ziel: dict = {}
    for role in KPI_ENERGY_ROLES:
        kanal = role.removeprefix("energy_")
        erwartet = einheiten.get(kanal)
        if role not in manifest.refs or erwartet is None:
            continue  # Spec Stufe 3 S4b 5: ohne erwartete Einheit kein Energiekanal
        ok, value = _read(role, signals.get_state)
        if not ok:
            continue
        try:
            gemeldet = signals.get_unit(manifest.refs[role])
        except ValueError:
            gemeldet = ""  # Attribut fehlt oder ist leer (HA, Gateway): gilt als gemeldete Einheit "", warnt je Wechsel
        except Exception as exc:  # voruebergehend (Quelle nicht erreichbar, Referenz fehlt): wie jeder KPI-Sensor
            logger.warning("KPI-Sensor '%s' nicht lesbar, Feld wird weggelassen: %s", role, exc)
            continue
        if gemeldet != erwartet:
            if _EINHEIT_GEWARNT.get(kanal) != gemeldet:
                _EINHEIT_GEWARNT[kanal] = gemeldet
                logger.warning(
                    "Energiekanal '%s' meldet %s statt %s und wird weggelassen",
                    kanal, f"'{gemeldet}'" if gemeldet else "keine Einheit", erwartet,
                )
            continue
        _EINHEIT_GEWARNT.pop(kanal, None)
        energy[kanal], ziel[kanal] = energy_core.umrechnen(value, gemeldet)
    if energy and normalize_energy is not None:
        energy = normalize_energy(energy, ziel)
    if energy:
        kpi_fields["energy"] = energy
    return kpi_fields


def energy_normalizer(store, kind: str) -> Callable[[dict, dict], dict] | None:
    """Normalisierer fuer die Zaehlerart des Bindings (BindingDescription.energy_counters); None fuer Summenzaehler
    (Vaillant: unveraendert, kein Zustand). Der Zustand liegt in BridgeState.energy_state (backup.json); scheitert das
    Speichern, gilt er im Speicher weiter (StateStore.update aendert vor dem Schreiben)."""
    if kind == ENERGY_TOTAL:
        return None

    def _normalize(raw: dict, einheiten: dict) -> dict:
        sent, state = energy_core.normalize(kind, store.state.energy_state, raw, einheiten)
        if state != store.state.energy_state:
            try:
                store.update(energy_state=state)
            except Exception as error:
                logger.warning("Energie-Zaehlerstand nicht gespeichert (%s), gilt bis zum Neustart", error)
        return sent

    return _normalize
