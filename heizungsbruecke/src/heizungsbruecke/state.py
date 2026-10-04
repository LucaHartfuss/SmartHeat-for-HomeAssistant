"""Zustand der Bruecke und seine Dateien in /data (backup.json, failsafe_state.json).

Der Zustand wird nur im Worker-Thread gelesen und geaendert, deshalb ohne Lock. Beide
Dateien werden beim Start genau einmal gelesen und danach nur geschrieben, wenn sich ihr
Inhalt aendert: jeder Schreibvorgang geht auf den Datentraeger des Pi."""
import logging
import math
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from heizungsbruecke import backup_store
from heizungsbruecke.delivery import DeliveryState, from_persisted, to_persisted
from heizungsbruecke.waerme import WaermeState, parse_since

logger = logging.getLogger(__name__)


class StorageError(OSError):
    """backup.json oder failsafe_state.json liess sich nicht schreiben (Datentraeger voll oder
    schreibgeschuetzt, TP12b/AU-005). Unterklasse von OSError: Aufrufer, die OSError erwarten,
    bleiben gueltig."""

_NUMBER_FIELDS = ("last_room_target", "last_published_target_rt")
_FLAG_FIELDS = ("boost_active", "emergency_boost_active")
_TEXT_FIELDS = ("last_daily_trigger_date", "last_ack_at", "waerme_fehlt_seit")
_TEXT_MAP_FIELDS = ("notify_states", "notify_messages")
_LEVER_MAP_FIELDS = ("restore_point", "originals", "learned")
_OVERRIDE_FIELDS = ("manual_override", "manual_override_pending")
# Plan 3b: Lebensdauerzaehler, zurueckgestellte Serverwerte, Hilfs-Ursprungswerte, Energie-Normalisierung. Fehlen sie in
# backup.json (bis 0.30.0), gilt der Standardwert; leer bzw. 0 werden sie nicht geschrieben (backup.json von client1
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
    last_room_target: float | None = None
    last_published_target_rt: float | None = None
    last_daily_trigger_date: str | None = None
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


# Plan 2 (P2-4, Plan-Praezisierung 1): Felder bis Add-on 0.29.0 (Rollen-Namen) -> Hebel-Namen. Die alten Schluessel
# werden uebernommen und entfernt, sonst blieben sie als unbekannte Schluessel fuer immer in backup.json.
_LEGACY_RESTORE_KEYS = {"curve_current": "curve", "shift_current": "room_setpoint", "heat_limit": "heat_limit"}
_LEGACY_ROLE_NAMES = {"curve_current": "curve", "shift_current": "room_setpoint"}
# KPI-Felder eines Eingriffs bis 0.29.0 (manual_override, manual_override_pending) -> Hebel in `levers`.
_LEGACY_KPI_KEYS = {"curve": "curve", "shift": "room_setpoint"}
_MANUAL_OVERRIDE_KEY = "manueller_eingriff"  # manual_override.KEY (Import waere zyklisch)


def _lever_name(role: str) -> str:
    return _LEGACY_ROLE_NAMES.get(role, role)


def _lever_signature(signature):
    """Signatur eines Eingriffs ("curve_current=1.2,shift_current=18") in Hebel-Namen: manual_override vergleicht
    signatur und gemeldet; beide werden gleich umbenannt, damit derselbe Eingriff nach dem Update nicht erneut
    gemeldet wird und eine Neuberechnung aus `rollen` dieselbe Signatur ergibt."""
    if not isinstance(signature, str):
        return signature
    return ",".join(
        f"{_lever_name(name)}{separator}{value}"
        for name, separator, value in (part.partition("=") for part in signature.split(","))
    )


def _lever_kpi(entry):
    """KPI-Eintrag bis 0.29.0 ({"curve", "shift", "erkannt", ...}) in die Form ab 0.30.0 ({"levers": {"curve",
    "room_setpoint"}, "erkannt", ...}); der Rest des Eintrags bleibt. Nur ein Eintrag mit beiden alten Schluesseln war
    bis 0.29.0 gueltig; jeder andere bleibt unveraendert (ein schon umgestellter, ein ungueltiger faellt danach in
    _is_override weg). Neben einem vorhandenen `levers` gelten die alten Werte (Rueckweg, Controller-Ruling Task 8)."""
    if not isinstance(entry, dict) or not all(key in entry for key in _LEGACY_KPI_KEYS):
        return entry
    existing = entry.get("levers")
    return {
        **{key: value for key, value in entry.items() if key not in _LEGACY_KPI_KEYS},
        "levers": {
            **(existing if isinstance(existing, dict) else {}),
            **{lever: entry[key] for key, lever in _LEGACY_KPI_KEYS.items()},
        },
    }


def _migrate_legacy_roles(raw: dict) -> dict:
    """Wie bis 0.29.0 wird jedes alte Feld einzeln geprueft: ein ungueltiger Wert faellt weg (Warnung), die anderen
    bleiben.

    Stehen alte und neue Felder zugleich in der Datei, kommt das nur aus 0.30.0 -> Rueckweg auf 0.29.0 -> erneutes
    Update (0.30.0 schreibt die alten Schluessel nie, 0.29.0 fuehrt unbekannte mit). Dann gilt (Controller-Ruling
    Task 8): die alten Werte des Wiederherstellungspunkts und alte Budget-Eintraege enforce:<rolle> sind die neueren
    und gewinnen; Hebel ohne alten Wert behalten ihren Eintrag. Beim Ursprungswert gewinnt dagegen ein vorhandenes
    `originals`: 0.29.0 haette nach dem Rueckweg den schon gelernten Live-Wert als heat_limit_original gemerkt."""
    raw = dict(raw)
    legacy = {key: raw.pop(key) for key in list(_LEGACY_RESTORE_KEYS) if key in raw}
    original = raw.pop("heat_limit_original", None)
    for key, value in {**legacy, "heat_limit_original": original}.items():
        if value is not None and not _is_number(value):
            logger.warning("backup.json: altes Feld '%s' ungueltig (%r), wird verworfen", key, value)
    if legacy:
        existing = raw.get("restore_point")
        if "restore_point" in raw:
            logger.warning("backup.json: alte Felder neben restore_point (Rueckweg auf 0.29.0?), die alten gelten")
        raw["restore_point"] = {
            **({k: v for k, v in existing.items() if _is_number(v)} if isinstance(existing, dict) else {}),
            **{_LEGACY_RESTORE_KEYS[key]: value for key, value in legacy.items() if _is_number(value)},
        }
    if "originals" not in raw and _is_number(original):
        raw["originals"] = {"heat_limit": original}
    budget = raw.get("write_budget")
    if isinstance(budget, dict):
        prefix = "enforce:"

        def _legacy_budget_key(key) -> bool:
            return isinstance(key, str) and key.startswith(prefix) and key[len(prefix):] in _LEGACY_ROLE_NAMES

        raw["write_budget"] = {
            **{key: entry for key, entry in budget.items() if not _legacy_budget_key(key)},
            **{
                prefix + _lever_name(key[len(prefix):]): entry
                for key, entry in budget.items() if _legacy_budget_key(key)
            },
        }
    for key in ("manual_override", "manual_override_pending"):
        if key in raw:
            raw[key] = _lever_kpi(raw[key])
    override = raw.get("manual_override")
    if isinstance(override, dict) and isinstance(override.get("rollen"), dict):
        raw["manual_override"] = {
            **override, "rollen": {_lever_name(role): value for role, value in override["rollen"].items()},
            **{key: _lever_signature(override[key]) for key in ("signatur", "gemeldet") if key in override},
        }
    # Der Meldezustand des Eingriffs (notifier, Schluessel manual_override.KEY) ist dieselbe Signatur: sonst gaelte
    # eine nach dem Update erstmals gemeldete, schon vorher zugestellte Signatur als neuer Zustand.
    notify_states = raw.get("notify_states")
    if isinstance(notify_states, dict) and isinstance(notify_states.get(_MANUAL_OVERRIDE_KEY), str):
        raw["notify_states"] = {
            **notify_states, _MANUAL_OVERRIDE_KEY: _lever_signature(notify_states[_MANUAL_OVERRIDE_KEY]),
        }
    return raw


def _parse_backup(raw: dict, path: Path) -> tuple[dict, dict]:
    """Liest die bekannten Felder tolerant: ein Feld mit falschem Typ faellt auf seinen
    Standardwert zurueck, die anderen bleiben. Unbekannte Schluessel werden unveraendert
    mitgefuehrt (spaetere Versionen). Felder bis 0.29.0 werden vorher auf Hebel umbenannt."""
    raw = _migrate_legacy_roles(raw)
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
        # waerme_fehlt_seit: nur ein ISO-Zeitpunkt mit Zeitzone ist ein Flag, alles andere "kein Flag" (Spec 1.4).
        valid = isinstance(raw[key], str) and (key != "waerme_fehlt_seit" or parse_since(raw[key]) is not None)
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
