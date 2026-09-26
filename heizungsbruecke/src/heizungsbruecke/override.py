"""Welche Kurve und welcher Offset auf der Anlage stehen sollen, und das Schreiben dorthin.

Sollwert-Regel (Vorrang von oben nach unten):
    Notfall-Boost aktiv -> curve_max / offset_max
    Comfort-Boost aktiv -> boost_curve_value / boost_offset_value
    sonst               -> Wiederherstellungspunkt curve_current / offset_current (nur vorhandene)

Geschrieben wird nur beim Wechsel der Zeile, weil jeder Schreibvorgang bei mypyllant ein
Cloud-Aufruf ist; jeder Wert wird auf die lokalen Clamps begrenzt."""
import logging
import math

from heizungsbruecke.clamping import clamp

logger = logging.getLogger(__name__)

ROLES = ("curve_current", "offset_current")

_ROW_EMERGENCY = "emergency"
_ROW_COMFORT = "comfort"
_ROW_RESTORE = "restore"
_ROW_LOG = {
    _ROW_EMERGENCY: "Notfall-Boost aktiv: Sollwerte auf Maximalwerte gesetzt",
    _ROW_COMFORT: "Boost aktiv: Sollwerte auf Boost-Werte gesetzt",
    _ROW_RESTORE: "Boost beendet: Wiederherstellungspunkt geschrieben (sofern vorhanden)",
}


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
    def __init__(self, store, manifest, ha_api, options: dict) -> None:
        self._store = store
        self._manifest = manifest
        self._ha_api = ha_api
        self._options = options

    def set_boosts(self, comfort: bool, emergency: bool) -> tuple[bool, bool]:
        """Setzt die Boost-Flags und schreibt die Werte der neuen Sollwert-Zeile, falls sie
        sich aendert. Ein neu startender Boost braucht vorher einen in backup.json
        gespeicherten Wiederherstellungspunkt, sonst wird er abgelehnt; ein laufender Boost
        wird nie abgelehnt. Wirft das Schreiben,
        bleiben die Flags unveraendert und der naechste Check versucht es erneut. Gibt die
        tatsaechlich gesetzten Flags zurueck."""
        state = self._store.state
        starting_comfort = comfort and not state.boost_active
        starting_emergency = emergency and not state.emergency_boost_active
        if (starting_comfort or starting_emergency) and not self._ensure_restore_point():
            if starting_comfort:
                logger.warning("Kein Wiederherstellungspunkt, Boost ausgesetzt - naechster Check versucht es erneut")
                comfort = False
            if starting_emergency:
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

    def apply_server_values(self, curve: float, offset: float) -> None:
        """Speichert die (geclampten) Serverwerte als Wiederherstellungspunkt, bevor irgendetwas
        geschrieben wird, und schreibt sie nur, wenn kein Boost laeuft; sonst uebernimmt sie
        das Boost-Ende."""
        clamped = {}
        for role, value in (("curve_current", curve), ("offset_current", offset)):
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
        if role == "curve_current":
            return self._options["curve_min"], self._options["curve_max"]
        return self._options["offset_min"], self._options["offset_max"]

    def _row_values(self, row: str) -> dict:
        if row == _ROW_EMERGENCY:
            return {"curve_current": self._options["curve_max"], "offset_current": self._options["offset_max"]}
        if row == _ROW_COMFORT:
            return {
                "curve_current": self._options["boost_curve_value"],
                "offset_current": self._options["boost_offset_value"],
            }
        state = self._store.state
        restore = {"curve_current": state.curve_current, "offset_current": state.offset_current}
        return {role: value for role, value in restore.items() if value is not None}

    def _write(self, values: dict) -> None:
        """Kurve vor Offset, nur gemappte Rollen, jeder Wert geclampt."""
        for role in ROLES:
            if role not in values or role not in self._manifest.entity_ids:
                continue
            entity_id = self._manifest.entity_ids[role]
            value = clamp(values[role], *self._limits(role))
            try:
                self._ha_api.set_number_value(entity_id, value)
            except Exception as error:
                raise DeviceWriteError(role, entity_id, error) from error

    def _ensure_restore_point(self) -> bool:
        """Fehlende Werte des Wiederherstellungspunkts (z. B. erster Boost vor der ersten
        Serverantwort) einmalig von der Anlage lesen und speichern. False, wenn das nicht
        geht oder der Wiederherstellungspunkt noch nicht in backup.json steht: ohne Rueckweg
        auf der Karte darf kein Boost schreiben. Laeuft schon ein Boost, steht die Anlage auf
        Boost-Werten: dann wird nichts gelesen und der zweite Boost darf starten."""
        state = self._store.state
        missing = [role for role in ROLES if role in self._manifest.entity_ids and getattr(state, role) is None]
        if state.boost_active or state.emergency_boost_active:
            if missing:
                logger.info(
                    "Wiederherstellungspunkt fehlt (%s), die Anlage steht schon auf Boost-Werten - "
                    "nicht von der Anlage gelesen", ", ".join(missing),
                )
            return True
        if not missing:
            if self._store.is_saved(*ROLES):
                return True
            try:
                self._store.update(curve_current=state.curve_current, offset_current=state.offset_current)
            except Exception as error:
                logger.warning("Wiederherstellungspunkt nicht gespeichert (backup.json): %s", error)
                return False
            return True
        try:
            live = {role: self._ha_api.get_state(self._manifest.entity_ids[role]) for role in missing}
        except Exception as error:
            logger.warning("Wiederherstellungspunkt nicht lesbar (%s): %s", ", ".join(missing), error)
            return False
        if not all(_is_finite_number(value) for value in live.values()):
            logger.warning("Wiederherstellungspunkt ungueltig: %r", live)
            return False
        self._store.update(**live)
        logger.info("Wiederherstellungspunkt vor Boost gesichert: %s", live)
        return True
