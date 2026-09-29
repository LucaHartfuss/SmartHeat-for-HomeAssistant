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
from heizungsbruecke.target_history import sanitize_history

logger = logging.getLogger(__name__)

_NUMBER_FIELDS = ("curve_current", "offset_current", "last_room_target", "last_published_target_rt")
_FLAG_FIELDS = ("boost_active", "emergency_boost_active")
_TEXT_FIELDS = ("last_daily_trigger_date", "last_ack_at")
_TEXT_MAP_FIELDS = ("notify_states", "notify_messages")
_OVERRIDE_FIELDS = ("manual_override", "manual_override_pending")
BACKUP_FIELDS = _NUMBER_FIELDS + _FLAG_FIELDS + ("target_history",) + _TEXT_FIELDS + _TEXT_MAP_FIELDS + _OVERRIDE_FIELDS


@dataclass(frozen=True)
class BridgeState:
    # Wiederherstellungspunkt: zuletzt vom Server bestaetigt oder vor dem ersten Boost von der
    # Anlage gesichert. Darauf setzt jedes Boost-Ende zurueck.
    curve_current: float | None = None
    offset_current: float | None = None
    boost_active: bool = False
    emergency_boost_active: bool = False
    last_room_target: float | None = None
    target_history: list = field(default_factory=list)
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
    # R6: aktiver manueller Eingriff {curve, offset, erkannt} bis zur Rueckkehr (Hinweis im Status).
    manual_override: dict | None = None
    # R6: noch nicht vom Server verarbeiteter Eingriff (KPI im naechsten Snapshot).
    manual_override_pending: dict | None = None
    # Nur Laufzeit (die Abo-Frist selbst liegt in entitlement_state.json).
    stable_target: float | None = None
    # Aufeinanderfolgende EV_HEALTH-Runden ohne gueltigen Wert je Raumfuehler (room_sensors.py).
    room_sensor_misses: dict = field(default_factory=dict)
    # Aufeinanderfolgende EV_HEALTH-Runden mit Abweichung von Kurve/Offset (manual_override.py).
    manual_override_misses: int = 0
    abo_inactive_since: datetime | None = None
    abo_finished: bool = False


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_override(value) -> bool:
    return (
        isinstance(value, dict) and _is_number(value.get("curve")) and _is_number(value.get("offset"))
        and isinstance(value.get("erkannt"), str)
    )


def _parse_backup(raw: dict, path: Path) -> tuple[dict, dict]:
    """Liest die bekannten Felder tolerant: ein Feld mit falschem Typ faellt auf seinen
    Standardwert zurueck, die anderen bleiben. Unbekannte Schluessel werden unveraendert
    mitgefuehrt (Rueckweg auf 0.16.0, spaetere Versionen)."""
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
        if isinstance(raw[key], str):
            values[key] = raw[key]
        else:
            _invalid(key)
    if "target_history" in raw:
        history = sanitize_history(raw["target_history"])
        if history != raw["target_history"]:
            logger.warning(
                "target_history in Backup war ungueltig (%r) - wird als leer behandelt und neu aufgebaut.",
                raw["target_history"],
            )
        values["target_history"] = history
    for key in _TEXT_MAP_FIELDS:
        if key not in raw:
            continue
        mapping = raw[key]
        if isinstance(mapping, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()):
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
    extra = {key: value for key, value in raw.items() if key not in BACKUP_FIELDS}
    return values, extra


def _backup_content(state: BridgeState, extra: dict) -> dict:
    """Unbekannte Schluessel plus alle gesetzten Felder. Nicht gesetzte Felder fehlen: 0.16.0
    erkennt einen vorhandenen Wiederherstellungspunkt an `"curve_current" in backup`."""
    content = dict(extra)
    for key in BACKUP_FIELDS:
        value = getattr(state, key)
        if value is None or (key in _TEXT_MAP_FIELDS and not value):
            continue
        content[key] = value
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
        Schreibfehler wird nach der Aenderung im Speicher weitergereicht."""
        if "delivery" in changes:
            raise ValueError("delivery nur ueber set_delivery aendern")
        self._state = replace(self._state, **changes)
        if not set(changes) & set(BACKUP_FIELDS):
            return
        content = _backup_content(self._state, self._extra)
        if content == self._backup_saved and not self._backup_dirty:
            return
        try:
            backup_store.save_backup(self._backup_path, content)
        except Exception:
            self._backup_dirty = True
            raise
        self._backup_saved, self._backup_dirty = content, False

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

    def saved_restore_point(self) -> dict | None:
        """Der zuletzt erfolgreich in backup.json gespeicherte Wiederherstellungspunkt (beide
        Werte), sonst None (N6)."""
        point = {key: self._backup_saved.get(key) for key in ("curve_current", "offset_current")}
        return point if all(_is_number(value) for value in point.values()) else None

    def revert_to_saved(self, *keys: str) -> None:
        """Setzt Felder im Speicher auf den zuletzt gespeicherten Stand zurueck, ohne zu schreiben
        (N6: Notfall-Boost auf dem aelteren, gesicherten Wiederherstellungspunkt)."""
        self._state = replace(self._state, **{key: self._backup_saved.get(key) for key in keys})
        self._backup_dirty = _backup_content(self._state, self._extra) != self._backup_saved

    def set_delivery(self, delivery_state: DeliveryState) -> None:
        """Best effort: ein Schreibfehler wird geloggt, der Zustand gilt dann bis zum
        naechsten erfolgreichen Schreiben bzw. Neustart."""
        self._state = replace(self._state, delivery=delivery_state)
        content = to_persisted(delivery_state)
        if content == self._failsafe_saved and not self._failsafe_dirty:
            return
        try:
            backup_store.save_backup(self._failsafe_path, content)
        except Exception:
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
