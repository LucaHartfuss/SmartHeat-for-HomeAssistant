"""Welche Steigung und Parallelverschiebung auf der Anlage stehen sollen, und das Schreiben
dorthin.

Sollwert-Regel (Vorrang von oben nach unten):
    Notfall-Boost aktiv -> curve_max / shift_max
    Comfort-Boost aktiv -> boost_curve_value / boost_shift_value
    sonst               -> Wiederherstellungspunkt curve_current / shift_current (nur vorhandene)

Der Mindestvorlauf gehoert zu keiner Zeile (immer = Raum-Soll, min_flow.py), wird aber auch
hier geschrieben (einziger Schreibweg zur Anlage).

Geschrieben wird nur bei tatsaechlicher Aenderung, weil jeder Schreibvorgang bei mypyllant ein
Cloud-Aufruf ist; jeder Wert wird auf die lokalen Clamps begrenzt und auf die Schrittweite der
Anlage gerundet (plant.py)."""
import logging
import math
import time
from collections.abc import Callable

from heizungsbruecke import plant
from heizungsbruecke.clamping import clamp

logger = logging.getLogger(__name__)

ROLES = ("curve_current", "shift_current")
_LIMIT_KEYS = {"curve_current": "curve", "shift_current": "shift", "min_flow": "min_flow"}

_ROW_EMERGENCY = "emergency"
_ROW_COMFORT = "comfort"
_ROW_RESTORE = "restore"
_ROW_LOG = {
    _ROW_EMERGENCY: "Notfall-Boost aktiv: Sollwerte auf Maximalwerte gesetzt",
    _ROW_COMFORT: "Boost aktiv: Sollwerte auf Boost-Werte gesetzt",
    _ROW_RESTORE: "Boost beendet: Wiederherstellungspunkt geschrieben (sofern vorhanden)",
}

# Ergebnis von _ensure_restore_point: der aktuelle Punkt ist gesichert, oder (nur Notfall-Boost)
# der zuletzt gespeicherte aeltere Punkt gilt (N6).
_POINT_CURRENT = "current"
_POINT_SAVED = "saved"


class DeviceWriteError(Exception):
    """Ein Wert konnte nicht auf die Anlage geschrieben werden (z. B. myVAILLANT-Cloud gestoert)."""

    def __init__(self, role: str, entity_id: str, cause: BaseException) -> None:
        self.role = role
        self.entity_id = entity_id
        super().__init__(f"{role} ({entity_id}): {str(cause)[:200]}")


def _row(comfort: bool, emergency: bool) -> str:
    if emergency:
        return _ROW_EMERGENCY
    if comfort:
        return _ROW_COMFORT
    return _ROW_RESTORE


def _is_finite_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class Override:
    def __init__(self, store, manifest, ha_api, options: dict, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._store = store
        self._manifest = manifest
        self._ha_api = ha_api
        self._options = options
        self._clock = clock
        # Zeitpunkt (clock) des letzten erfolgreichen eigenen Schreibens je Rolle, nur Laufzeit:
        # die R6-Erkennung (manual_override) pausiert danach, weil HA den alten Wert noch eine
        # Weile zeigen kann.
        self._last_write_at: dict[str, float] = {}

    def seconds_since_write(self, role: str) -> float | None:
        """Sekunden seit dem letzten eigenen erfolgreichen Schreiben dieser Rolle; None, wenn seit
        dem Start nicht geschrieben wurde (die Durchsetzung pausiert danach, HA hinkt nach)."""
        last = self._last_write_at.get(role)
        return None if last is None else self._clock() - last

    def set_boosts(self, comfort: bool, emergency: bool) -> tuple[bool, bool]:
        """Setzt die Boost-Flags und schreibt die Werte der neuen Sollwert-Zeile, falls sie
        sich aendert. Ein neu startender Boost braucht vorher einen in backup.json
        gespeicherten Wiederherstellungspunkt, sonst wird er abgelehnt; ein laufender Boost
        wird nie abgelehnt. Ein neuer Notfall-Boost darf zusaetzlich auf dem zuletzt
        gespeicherten, aelteren Punkt starten, wenn der aktuelle nicht speicherbar ist (N6); sein
        Ende setzt dann auf genau diesen zurueck. Wirft das Schreiben, bleiben die Flags
        unveraendert und der naechste Check versucht es erneut. Gibt die tatsaechlich gesetzten
        Flags zurueck."""
        state = self._store.state
        starting_comfort = comfort and not state.boost_active
        starting_emergency = emergency and not state.emergency_boost_active
        if starting_comfort or starting_emergency:
            point = self._ensure_restore_point(allow_saved_fallback=starting_emergency)
            if point != _POINT_CURRENT and starting_comfort:
                logger.warning("Kein Wiederherstellungspunkt, Boost ausgesetzt - naechster Check versucht es erneut")
                comfort = False
            if point is None and starting_emergency:
                logger.warning(
                    "Kein Wiederherstellungspunkt, Notfall-Boost ausgesetzt - naechster Check versucht es erneut"
                )
                emergency = False
        new_row = _row(comfort, emergency)
        if new_row != _row(state.boost_active, state.emergency_boost_active):
            self._write(self._row_values(new_row))
            logger.warning(_ROW_LOG[new_row])
        self._store.update(boost_active=comfort, emergency_boost_active=emergency)
        return comfort, emergency

    def apply_server_values(self, curve: float, shift: float) -> None:
        """Speichert die (geclampten) Serverwerte als Wiederherstellungspunkt, bevor irgendetwas
        geschrieben wird, und schreibt sie nur, wenn kein Boost laeuft; sonst uebernimmt sie
        das Boost-Ende."""
        clamped = {}
        for role, value in (("curve_current", curve), ("shift_current", shift)):
            minimum, maximum = self._limits(role)
            clamped[role] = clamp(value, minimum, maximum)
            if clamped[role] != value:
                logger.warning(
                    "Wert fuer Rolle '%s' geclampt: empfangen=%s, geschrieben=%s (Bereich [%s, %s])",
                    role, value, clamped[role], minimum, maximum,
                )
        self._store.update(**clamped)
        state = self._store.state
        if state.boost_active or state.emergency_boost_active:
            logger.info("Serverwerte waehrend aktivem (Notfall-)Boost nur gespeichert, die Anlage bleibt auf den Boost-Werten.")
            return
        self._write(clamped)

    def restore_and_clear(self, always_restore: bool) -> bool:
        """Abo-Fristende: beide Boosts beenden und den Wiederherstellungspunkt schreiben (bei
        always_restore auch ohne laufenden Boost). False, wenn das Schreiben scheitert: die
        Flags bleiben, der Aufrufer versucht es erneut. Scheitert danach nur das Speichern der
        Flags, gilt die Wiederherstellung als erfolgt, die Werte stehen ja auf der Anlage."""
        state = self._store.state
        if not (always_restore or state.boost_active or state.emergency_boost_active):
            return True
        try:
            self._write(self._row_values(_ROW_RESTORE))
        except Exception:
            logger.exception(
                "Zuletzt gelernte Werte konnten nicht wiederhergestellt werden - Boost-Flags "
                "bleiben gesetzt, es wird erneut versucht"
            )
            return False
        try:
            self._store.update(boost_active=False, emergency_boost_active=False)
        except Exception:
            logger.exception("Zuletzt gelernte Werte wiederhergestellt, Boost-Flags konnten aber nicht gespeichert werden")
        return True

    def _limits(self, role: str) -> tuple[float, float]:
        prefix = _LIMIT_KEYS[role]
        return self._options[f"{prefix}_min"], self._options[f"{prefix}_max"]

    def _row_values(self, row: str) -> dict:
        if row == _ROW_EMERGENCY:
            return {"curve_current": self._options["curve_max"], "shift_current": self._options["shift_max"]}
        if row == _ROW_COMFORT:
            return {
                "curve_current": self._options["boost_curve_value"],
                "shift_current": self._options["boost_shift_value"],
            }
        state = self._store.state
        restore = {"curve_current": state.curve_current, "shift_current": state.shift_current}
        return {role: value for role, value in restore.items() if value is not None}

    def _write_role(self, role: str, ref: str, value: float, *, ensure_mode: bool = True) -> float:
        """Schreibt EINEN Wert (Steigung, Parallelverschiebung oder Mindestvorlauf) ueber
        plant.write, aber nur bei tatsaechlicher Aenderung (Spec 5.2: Kontingent-Schutz der
        Hersteller-Cloud). Der aktuelle Wert kommt aus einem lokalen HA-Read (kein Cloud-Aufruf,
        bei der Parallelverschiebung ueber plant.read_shift, das die Zone-inaktiv-Meldung < 5
        beruecksichtigt); schlaegt der Read fehl oder ist er nicht auswertbar, wird trotzdem
        geschrieben. Ein uebersprungener Schreibvorgang merkt sich keinen Zeitpunkt. Wirft
        DeviceWriteError."""
        minimum, maximum = self._limits(role)
        target = plant.round_to_step(clamp(value, minimum, maximum), plant.STEPS[role])
        try:
            current = plant.read_shift(self._ha_api, ref) if role == "shift_current" else self._ha_api.get_state(ref)
        except Exception:
            current = None
        if current is not None and _is_finite_number(current) and abs(current - target) <= plant.STEPS[role] / 2:
            return target
        try:
            written = plant.write(self._ha_api, role, ref, value, minimum, maximum, ensure_mode=ensure_mode)
        except Exception as error:
            raise DeviceWriteError(role, ref, error) from error
        self._last_write_at[role] = self._clock()
        return written

    def _write(self, values: dict) -> None:
        """Betriebsart -> Steigung -> Parallelverschiebung (Spec 5.2), nur gemappte Rollen, jeder
        Wert begrenzt und auf die Schrittweite der Anlage gerundet (plant.write ueber
        _write_role). Ist die Parallelverschiebung eine Climate-Zone, wird sie VOR der Steigung
        auf Manuell gestellt; ihr eigener Schreibvorgang laeuft danach ohne erneute
        Modus-Pruefung (Ruling #3: mypyllant meldet einen Cloud-Modus-Wechsel erst mit
        Verzoegerung an HA zurueck)."""
        shift_ref = self._manifest.entity_ids.get("shift_current")
        if "shift_current" in values and shift_ref and plant.is_climate(shift_ref):
            self.ensure_manual_zone()
        for role in ROLES:
            if role not in values or role not in self._manifest.entity_ids:
                continue
            ref = self._manifest.entity_ids[role]
            self._write_role(role, ref, values[role], ensure_mode=(role != "shift_current"))

    def expected_values(self) -> dict:
        """Sollwerte der aktuellen Zeile, so wie sie auf der Anlage stehen muessten (begrenzt und
        gerundet); nur vorhandene Werte."""
        return {
            role: plant.round_to_step(clamp(value, *self._limits(role)), plant.STEPS[role])
            for role, value in self._row_values(self._current_row()).items()
            if role in self._manifest.entity_ids
        }

    def write_roles(self, roles: tuple[str, ...]) -> None:
        """Nur diese Rollen der aktuellen Zeile schreiben (Durchsetzung). Wirft DeviceWriteError."""
        values = self._row_values(self._current_row())
        self._write({role: values[role] for role in roles if role in values})

    def write_min_flow(self, value: float) -> float:
        ref = self._manifest.entity_ids["min_flow"]
        return self._write_role("min_flow", ref, value)

    def ensure_manual_zone(self) -> bool:
        ref = self._manifest.entity_ids["shift_current"]
        try:
            switched = plant.ensure_manual_mode(self._ha_api, ref)
        except Exception as error:
            raise DeviceWriteError("shift_current", ref, error) from error
        if switched:
            self._last_write_at["shift_current"] = self._clock()
        return switched

    def prepare_zone(self, start_shift: float | None) -> None:
        """Start (Plan-Praezisierung 11): Zone auf Manuell; meldet sie danach keine brauchbare
        Parallelverschiebung oder wurde gerade umgestellt, den Startwert schreiben und als
        Wiederherstellungspunkt speichern. Startwert: gespeicherter Punkt, sonst `start_shift`
        (Raum-Soll). Wirft DeviceWriteError."""
        ref = self._manifest.entity_ids["shift_current"]
        if not plant.is_climate(ref):
            return
        switched = self.ensure_manual_zone()
        try:
            live = plant.read_shift(self._ha_api, ref)
        except Exception:
            live = None
        low, high = self._limits("shift_current")
        if not switched and live is not None and low <= live <= high:
            return
        start = self._store.state.shift_current if self._store.state.shift_current is not None else start_shift
        if start is None:
            return
        written = self._write_role("shift_current", ref, start, ensure_mode=False)
        self._store.update(shift_current=written)
        logger.warning("Startwert der Parallelverschiebung geschrieben: %s", written)

    def _current_row(self) -> str:
        state = self._store.state
        return _row(state.boost_active, state.emergency_boost_active)

    def _ensure_restore_point(self, *, allow_saved_fallback: bool) -> str | None:
        """Fehlende Werte des Wiederherstellungspunkts (z. B. erster Boost vor der ersten
        Serverantwort) einmalig von der Anlage lesen und speichern. None, wenn das nicht geht
        oder der Punkt noch nicht in backup.json steht: ohne Rueckweg auf dem Datentraeger darf
        kein Boost schreiben -- ausser (allow_saved_fallback) auf dem zuletzt gespeicherten Punkt
        (_POINT_SAVED). Laeuft schon ein Boost, steht die Anlage auf Boost-Werten: dann wird
        nichts gelesen und der zweite Boost darf starten."""
        state = self._store.state
        missing = [role for role in ROLES if role in self._manifest.entity_ids and getattr(state, role) is None]
        if state.boost_active or state.emergency_boost_active:
            if missing:
                logger.info(
                    "Wiederherstellungspunkt fehlt (%s), die Anlage steht schon auf Boost-Werten - "
                    "nicht von der Anlage gelesen", ", ".join(missing),
                )
            return _POINT_CURRENT
        if not missing:
            if self._store.is_saved(*ROLES):
                return _POINT_CURRENT
            try:
                self._store.update(curve_current=state.curve_current, shift_current=state.shift_current)
            except Exception as error:
                logger.warning("Wiederherstellungspunkt nicht gespeichert (backup.json): %s", error)
                return self._saved_fallback() if allow_saved_fallback else None
            return _POINT_CURRENT
        try:
            live = {
                role: (
                    plant.read_shift(self._ha_api, self._manifest.entity_ids[role]) if role == "shift_current"
                    else self._ha_api.get_state(self._manifest.entity_ids[role])
                )
                for role in missing
            }
        except Exception as error:
            logger.warning("Wiederherstellungspunkt nicht lesbar (%s): %s", ", ".join(missing), error)
            return None
        if not all(value is not None and _is_finite_number(value) for value in live.values()):
            logger.warning("Wiederherstellungspunkt ungueltig: %r", live)
            return None
        self._store.update(**live)
        logger.info("Wiederherstellungspunkt vor Boost gesichert: %s", live)
        return _POINT_CURRENT

    def _saved_fallback(self) -> str | None:
        saved = self._store.saved_restore_point()
        if saved is None:
            return None
        self._store.revert_to_saved(*ROLES)
        logger.warning(
            "Notfall-Boost auf dem zuletzt gespeicherten Wiederherstellungspunkt %s (neuer Punkt nicht speicherbar)",
            saved,
        )
        return _POINT_SAVED
