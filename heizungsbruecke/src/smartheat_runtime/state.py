"""Zustand der Bruecke und seine Dateien in /data (backup.json, failsafe_state.json).

Der Zustand wird nur im Worker-Thread gelesen und geaendert, deshalb ohne Lock. Beide
Dateien werden beim Start genau einmal gelesen und danach nur geschrieben, wenn sich ihr
Inhalt aendert: jeder Schreibvorgang geht auf den Datentraeger des Pi."""
import logging
import math
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from smartheat_runtime import backup_store
from smartheat_runtime.delivery import DeliveryState, from_persisted, to_persisted
from smartheat_runtime.waerme import WaermeState, parse_since

logger = logging.getLogger(__name__)


class StorageError(OSError):
    """backup.json oder failsafe_state.json liess sich nicht schreiben (Datentraeger voll oder
    schreibgeschuetzt, TP12b/AU-005). Unterklasse von OSError: Aufrufer, die OSError erwarten,
    bleiben gueltig."""

_NUMBER_FIELDS = ("last_room_target", "last_published_target_rt")
_FLAG_FIELDS = ("boost_active", "emergency_boost_active")
_TEXT_FIELDS = ("last_daily_trigger_date", "last_ack_at", "waerme_fehlt_seit", "boost_since", "setup_id", "plant_id")
_TEXT_MAP_FIELDS = ("notify_states", "notify_messages")
_LEVER_MAP_FIELDS = ("restore_point", "originals", "learned")
_OVERRIDE_FIELDS = ("manual_override", "manual_override_pending")
# Plan 3b: Lebensdauerzaehler, zurueckgestellte Serverwerte, Hilfs-Ursprungswerte, Energie-Normalisierung. Fehlen sie in
# backup.json (bis 0.30.0), gilt der Standardwert; leer bzw. 0 werden sie nicht geschrieben (eine bestehende backup.json
# bleibt gleich).
_PLAN3B_FIELDS = ("lifetime_writes", "deferred_levers", "aux_originals", "energy_state")
BACKUP_FIELDS = (
    _NUMBER_FIELDS + _FLAG_FIELDS + _TEXT_FIELDS + _TEXT_MAP_FIELDS + _LEVER_MAP_FIELDS + _OVERRIDE_FIELDS
    + ("write_budget",) + _PLAN3B_FIELDS
)
_OMITTED_WHEN_EMPTY = _TEXT_MAP_FIELDS + _LEVER_MAP_FIELDS + ("write_budget",) + _PLAN3B_FIELDS


@dataclass(frozen=True)
class BridgeState:
    # Wiederherstellungspunkt je Hebel (Plan 2 P2-4): zuletzt vom Server bestaetigt oder vor dem ersten Boost von der
    # Anlage gesichert. Darauf setzt jedes Boost-Ende zurueck.
    restore_point: dict = field(default_factory=dict)
    # Ursprungswerte je Hebel vor dem ersten eigenen Schreiben (P2-5; Vaillant: nur die Heizgrenze): Abo-Ende und
    # Abmelden stellen sie wieder her.
    originals: dict = field(default_factory=dict)
    # Plan 3c: Lernwerte der letzten gueltigen Serverantwort (learned: {"curve": c, "heat_limit": G}), nur Anzeige im
    # Statusereignis.
    learned: dict = field(default_factory=dict)
    boost_active: bool = False
    emergency_boost_active: bool = False
    # Audit 4, A4-09: Beginn des laufenden Comfort-Boosts (ISO), fuer die Hoechstdauer; None ohne Boost.
    boost_since: str | None = None
    # Aufeinanderfolgende lokale Checks ohne lesbaren Raumwert (nur Laufzeit, A4-09).
    room_actual_misses: int = 0
    last_room_target: float | None = None
    last_published_target_rt: float | None = None
    last_daily_trigger_date: str | None = None
    # Audit 4, A4-08: Einrichtung und Anlage, zu denen der Zustand gehoert (bind_to_setup).
    setup_id: str | None = None
    plant_id: str | None = None
    delivery: DeliveryState = field(default_factory=DeliveryState)
    # Zuletzt gemeldeter Zustand je Meldeschluessel (notifier.py); fehlender Schluessel = "ok".
    notify_states: dict = field(default_factory=dict)
    # Letzter Meldetext je nicht-"ok" kritischem Schluessel: damit legt der notifier die
    # HA-Benachrichtigung nach einem HA-Neustart neu an (HA haelt sie nur im Speicher).
    notify_messages: dict = field(default_factory=dict)
    # Zeitpunkt (ISO) der letzten Serverantwort auf einen offenen Tick (Status letzte_serverantwort).
    last_ack_at: str | None = None
    # Durchsetzung (enforce.py): aktiver Eingriff {levers, erkannt, rollen, signatur, gemeldet} bis
    # zur Rueckkehr (Hinweis im Status); levers, rollen und signatur nennen Hebel.
    manual_override: dict | None = None
    # Durchsetzung (enforce.py): noch nicht vom Server verarbeiteter Eingriff {levers, erkannt} (KPI im
    # naechsten Snapshot).
    manual_override_pending: dict | None = None
    # Nur Laufzeit (die Abo-Frist selbst liegt in entitlement_state.json).
    stable_target: float | None = None
    # Mindestvorlauf, wie er zuletzt auf der Anlage stand (derived.py, fuer den Status). Nur
    # Laufzeit: wird nicht persistiert (Praezisierung 12), beim Start neu ermittelt.
    min_flow_current: float | None = None
    # Aufeinanderfolgende EV_HEALTH-Runden ohne gueltigen Wert je Raumfuehler (room_sensors.py).
    room_sensor_misses: dict = field(default_factory=dict)
    # Aufeinanderfolgende EV_HEALTH-Runden mit Abweichung von Kurve/Parallelverschiebung
    # (enforce.py).
    manual_override_misses: int = 0
    abo_inactive_since: datetime | None = None
    abo_finished: bool = False
    # Schreibbudget je Schluessel (write_budget.py, TP12b): Durchsetzung, Zonenvorbereitung,
    # Boost-Start/-Ende, Wiederherstellung. In backup.json; "last" nur zur Laufzeit gueltig.
    write_budget: dict = field(default_factory=dict)
    # Seit wann die Therme trotz Anforderung keine Waerme liefert (waerme.py, TP12f): ISO-Zeitpunkt,
    # uebersteht Neustarts (keine zweite Meldung). Der Phasenzustand darunter ist nur Laufzeit: er aendert sich
    # mit jedem Tick, und backup.json wird nur bei geaenderten Feldern geschrieben.
    waerme_fehlt_seit: str | None = None
    waerme: WaermeState | None = None
    # Plan 3b: erfolgreiche physische Schreibvorgaenge seit der Einrichtung (nur Bindings mit lifetime_hint_at, EEPROM).
    lifetime_writes: int = 0
    # Plan 3b: Hebel, deren Serverwert wegen des Tagesbudgets noch nicht geschrieben ist (LeverPipeline.write_deferred);
    # Durchsetzen wertet sie nicht als Eingriff.
    deferred_levers: tuple[str, ...] = ()
    # Plan 3b: Hilfs-Ursprungswerte (BindingDescription.aux_originals) vor dem ersten eigenen Schreiben, z. B.
    # {"mode_select": "Automatik", "setpoint_comfort": 22.0}; Abo-Ende und Abmelden stellen sie zurueck.
    aux_originals: dict = field(default_factory=dict)
    # Plan 3b: Energie-Normalisierung je Kanal {"raw": letzter Rohwert, "sum": monotone Summe} (nur Tageszaehler).
    energy_state: dict = field(default_factory=dict)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_override(value) -> bool:
    """KPI-Eintrag eines Eingriffs: `levers` ein nicht leeres Dict aus endlichen Zahlen je Hebel, `erkannt` Text."""
    if not isinstance(value, dict) or not isinstance(value.get("erkannt"), str):
        return False
    levers = value.get("levers")
    return (
        isinstance(levers, dict) and bool(levers)
        and all(isinstance(lever, str) and _is_number(number) for lever, number in levers.items())
    )


def _is_aux(value) -> bool:
    return isinstance(value, str) or _is_number(value)


def _is_energy_entry(value) -> bool:
    return isinstance(value, dict) and set(value) == {"raw", "sum"} and all(_is_number(v) for v in value.values())


def _parse_budget(raw) -> dict | None:
    """Tag, Anzahl und Limit-Markierung je Schluessel; "last" (monotone Uhr des vorigen Laufs) faellt
    weg. None bei jedem Formfehler (dann gilt ein leeres Budget)."""
    if not isinstance(raw, dict):
        return None
    parsed = {}
    for key, entry in raw.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            return None
        day, count = entry.get("day"), entry.get("count")
        if not isinstance(day, str) or not isinstance(count, int) or isinstance(count, bool) or count < 0:
            return None
        parsed[key] = {"day": day, "count": count}
        if isinstance(entry.get("limit_notified"), str):
            parsed[key]["limit_notified"] = entry["limit_notified"]
    return parsed


def _parse_backup(raw: dict, path: Path) -> tuple[dict, dict]:
    """Liest die bekannten Felder tolerant: ein Feld mit falschem Typ faellt auf seinen
    Standardwert zurueck, die anderen bleiben. Unbekannte Schluessel werden unveraendert
    mitgefuehrt (spaetere Versionen; Felder bis 0.29.0 seit Audit 4 P-E ebenso)."""
    values = {}

    def _invalid(key):
        logger.warning("%s: Feld '%s' ungueltig (%r), Standardwert wird verwendet", path, key, raw[key])

    for key in _NUMBER_FIELDS:
        if raw.get(key) is None:
            continue
        if _is_number(raw[key]):
            values[key] = raw[key]
        else:
            _invalid(key)
    for key in _FLAG_FIELDS:
        if key not in raw:
            continue
        if isinstance(raw[key], bool):
            values[key] = raw[key]
        else:
            _invalid(key)
    for key in _TEXT_FIELDS:
        if raw.get(key) is None:
            continue
        # waerme_fehlt_seit, boost_since: nur ein ISO-Zeitpunkt mit Zeitzone ist gueltig, alles andere "kein Flag"
        # (Spec 1.4; boost_since: Audit 4 P-C2, sonst bricht der lokale Check bei jedem Lauf ab).
        valid = isinstance(raw[key], str) and (
            key not in ("waerme_fehlt_seit", "boost_since") or parse_since(raw[key]) is not None
        )
        if valid:
            values[key] = raw[key]
        else:
            _invalid(key)
    for key in _TEXT_MAP_FIELDS:
        if key not in raw:
            continue
        mapping = raw[key]
        if isinstance(mapping, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()):
            values[key] = dict(mapping)
        else:
            _invalid(key)
    for key in _LEVER_MAP_FIELDS:
        if key not in raw:
            continue
        mapping = raw[key]
        if isinstance(mapping, dict) and all(isinstance(k, str) and _is_number(v) for k, v in mapping.items()):
            values[key] = dict(mapping)
        else:
            _invalid(key)
    for key in _OVERRIDE_FIELDS:
        if raw.get(key) is None:
            continue
        if _is_override(raw[key]):
            values[key] = dict(raw[key])
        else:
            _invalid(key)
    if "write_budget" in raw:
        budget = _parse_budget(raw["write_budget"])
        if budget is None:
            _invalid("write_budget")
        else:
            values["write_budget"] = budget
    _parse_plan3b(raw, values, _invalid)
    extra = {key: value for key, value in raw.items() if key not in BACKUP_FIELDS}
    return values, extra


def _parse_plan3b(raw: dict, values: dict, invalid) -> None:
    """Felder aus Plan 3b, tolerant wie die uebrigen: falscher Typ -> Standardwert (Warnung)."""
    checks = {
        "lifetime_writes": lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0,
        "deferred_levers": lambda v: isinstance(v, list) and all(isinstance(item, str) for item in v),
        "aux_originals": lambda v: isinstance(v, dict) and all(isinstance(k, str) and _is_aux(x) for k, x in v.items()),
        "energy_state": lambda v: isinstance(v, dict) and all(
            isinstance(k, str) and _is_energy_entry(x) for k, x in v.items()
        ),
    }
    for key, valid in checks.items():
        if key not in raw:
            continue
        if not valid(raw[key]):
            invalid(key)
        elif key == "deferred_levers":
            values[key] = tuple(raw[key])
        elif isinstance(raw[key], dict):
            values[key] = {k: dict(v) if isinstance(v, dict) else v for k, v in raw[key].items()}
        else:
            values[key] = raw[key]


def _backup_content(state: BridgeState, extra: dict) -> dict:
    """Unbekannte Schluessel plus alle gesetzten Felder. Nicht gesetzte und leere Felder fehlen."""
    content = dict(extra)
    for key in BACKUP_FIELDS:
        value = getattr(state, key)
        if value is None or (key in _OMITTED_WHEN_EMPTY and not value):
            continue
        content[key] = list(value) if isinstance(value, tuple) else value
    return content


class StateStore:
    def __init__(self, backup_path: Path, failsafe_path: Path) -> None:
        self._backup_path = backup_path
        self._failsafe_path = failsafe_path
        values, self._extra = self._load_backup()
        self._state = BridgeState(**values, delivery=self._load_delivery())
        self._backup_saved = _backup_content(self._state, self._extra)
        self._failsafe_saved = to_persisted(self._state.delivery)
        self._backup_dirty = False
        self._failsafe_dirty = False

    @property
    def state(self) -> BridgeState:
        return self._state

    def update(self, **changes) -> None:
        """Aendert Felder und schreibt backup.json, wenn sich deren Inhalt aendert oder ein
        frueherer Schreibversuch gescheitert ist. Reine Laufzeitfelder schreiben nie. Ein
        Schreibfehler wird nach der Aenderung im Speicher als StorageError weitergereicht."""
        if "delivery" in changes:
            raise ValueError("delivery nur ueber set_delivery aendern")
        self._state = replace(self._state, **changes)
        if not set(changes) & set(BACKUP_FIELDS):
            return
        content = _backup_content(self._state, self._extra)
        if content == self._backup_saved and not self._backup_dirty:
            return
        try:
            self._save(self._backup_path, content)
        except StorageError:
            self._backup_dirty = True
            raise
        self._backup_saved, self._backup_dirty = content, False

    @property
    def storage_failed(self) -> bool:
        """True, solange backup.json oder failsafe_state.json nicht geschrieben werden konnte
        (TP12b, AU-005). Lebt nur im Speicher: ein kaputter Datentraeger kann sich nicht merken,
        dass er kaputt ist."""
        return self._backup_dirty or self._failsafe_dirty

    def flush(self) -> None:
        """Holt einen gescheiterten Schreibvorgang nach, ohne den Zustand zu aendern (Takt
        EV_HEALTH): so verschwindet die Datentraeger-Meldung auch ohne neuen Schreibanlass.
        Wirft StorageError, solange es weiter scheitert."""
        if self._backup_dirty:
            content = _backup_content(self._state, self._extra)
            self._save(self._backup_path, content)
            self._backup_saved, self._backup_dirty = content, False
        if self._failsafe_dirty:
            content = to_persisted(self._state.delivery)
            self._save(self._failsafe_path, content)
            self._failsafe_saved, self._failsafe_dirty = content, False

    @staticmethod
    def _save(path: Path, content: dict) -> None:
        try:
            backup_store.save_backup(path, content)
        except Exception as error:
            raise StorageError(f"{path.name} nicht schreibbar: {error}") from error

    def is_saved(self, *keys: str) -> bool:
        """True, wenn die genannten backup.json-Felder so auf dem Datentraeger stehen wie im Speicher
        (False nach einem gescheiterten Schreibversuch, bis er nachgeholt ist)."""
        content = _backup_content(self._state, self._extra)
        return all(content.get(key) == self._backup_saved.get(key) for key in keys)

    def update_saved(self, **changes) -> None:
        """Wie update(), aber bei einem Schreibfehler bleiben die Felder im Speicher auf dem alten
        Stand (N5): ein Folgeaufruf sieht dieselbe Aenderung dann erneut."""
        previous = {key: getattr(self._state, key) for key in changes}
        try:
            self.update(**changes)
        except Exception:
            self._state = replace(self._state, **previous)
            raise

    def saved_restore_point(self, required: tuple[str, ...]) -> dict | None:
        """Der zuletzt erfolgreich gespeicherte Wiederherstellungspunkt, wenn er alle `required` Hebel hat, sonst None
        (N6; die Hebel aus optional_restore duerfen fehlen)."""
        point = self._backup_saved.get("restore_point") or {}
        return dict(point) if all(_is_number(point.get(lever)) for lever in required) else None

    def revert_to_saved(self, *keys: str) -> None:
        """Setzt Felder im Speicher auf den zuletzt gespeicherten Stand zurueck, ohne zu schreiben (N6)."""
        reverted = {}
        for key in keys:
            value = self._backup_saved.get(key)
            reverted[key] = (dict(value) if value else {}) if key in _LEVER_MAP_FIELDS else value
        self._state = replace(self._state, **reverted)
        self._backup_dirty = _backup_content(self._state, self._extra) != self._backup_saved

    def set_delivery(self, delivery_state: DeliveryState) -> None:
        """Best effort: ein Schreibfehler wird geloggt, der Zustand gilt dann bis zum
        naechsten erfolgreichen Schreiben bzw. Neustart."""
        self._state = replace(self._state, delivery=delivery_state)
        content = to_persisted(delivery_state)
        if content == self._failsafe_saved and not self._failsafe_dirty:
            return
        try:
            self._save(self._failsafe_path, content)
        except StorageError:
            self._failsafe_dirty = True
            logger.exception(
                "failsafe_state.json konnte nicht geschrieben werden - Zustand gilt nur bis zum naechsten Neustart"
            )
            return
        self._failsafe_saved, self._failsafe_dirty = content, False

    def _load_backup(self) -> tuple[dict, dict]:
        try:
            raw = backup_store.load_backup(self._backup_path)
        except Exception as error:
            logger.warning("%s nicht lesbar, starte mit Standardwerten: %s", self._backup_path, error)
            return {}, {}
        if not isinstance(raw, dict):
            logger.warning("%s enthaelt kein JSON-Objekt, starte mit Standardwerten", self._backup_path)
            return {}, {}
        return _parse_backup(raw, self._backup_path)

    def _load_delivery(self) -> DeliveryState:
        try:
            raw = backup_store.load_backup(self._failsafe_path)
        except Exception as error:
            logger.warning("%s nicht lesbar, starte mit Standardwerten: %s", self._failsafe_path, error)
            return DeliveryState()
        if not isinstance(raw, dict):
            logger.warning("%s enthaelt kein JSON-Objekt, starte mit Standardwerten", self._failsafe_path)
            return DeliveryState()
        return from_persisted(raw)


# Audit 4, A4-08 (Nutzer-Entscheidung E3): was zur Anlage gehoert (bei einer anderen Anlage verworfen) und was zur
# Einrichtung (bei jeder neuen Einrichtung zurueckgesetzt: Erstkontakt-Tick, frische Zustellung).
_PLANT_BOUND = (
    "restore_point", "originals", "aux_originals", "learned", "boost_active", "emergency_boost_active", "boost_since",
    "manual_override", "manual_override_pending", "write_budget", "deferred_levers", "energy_state",
    "lifetime_writes", "last_room_target", "waerme_fehlt_seit",
)
_SETUP_BOUND = ("last_published_target_rt", "last_daily_trigger_date")


def bind_to_setup(store: "StateStore", setup_id: str | None, plant_id: str) -> str:
    """Bindet den Zustand an Einrichtung und Anlage. Ein Bestand ohne Bindung (bis Add-on 0.34.0) gilt als gebunden
    (nichts verwerfen, Review Focus 1). Andere Anlage: anlagenbezogene Felder auf den Standardwert. Neue Einrichtung
    derselben Anlage: Ursprungswerte bleiben, nur Tick-Buchung und Zustellung beginnen neu."""
    state, defaults = store.state, BridgeState()
    if state.setup_id is None and state.plant_id is None:
        store.update(setup_id=setup_id, plant_id=plant_id)
        return "bestand"
    if state.plant_id != plant_id:
        logger.warning("Andere Anlage als in backup.json: anlagenbezogener Zustand wird verworfen")
        # Zustellung zuerst: set_delivery wirft nicht, update bei einem Schreibfehler (StorageError) schon
        store.set_delivery(DeliveryState())
        store.update(**{key: getattr(defaults, key) for key in _PLANT_BOUND + _SETUP_BOUND},
                     setup_id=setup_id, plant_id=plant_id)
        return "andere_anlage"
    if state.setup_id != setup_id:
        logger.info("Neue Einrichtung derselben Anlage: Ursprungswerte bleiben, Erstkontakt-Tick folgt")
        store.set_delivery(DeliveryState())
        store.update(**{key: getattr(defaults, key) for key in _SETUP_BOUND}, setup_id=setup_id)
        return "neue_einrichtung"
    return "unveraendert"
