"""HA-Binding der Bruecke (Hersteller-Abstraktion, Plan 2 P2-2): liest und schreibt die Hebel ueber die HA-API.
Alles mypyllant-Spezifische steht hier (vorher plant.py): die Zonen-Wunschtemperatur ist eine Climate-Entity, die
nur im Modus "Manuell" (HA heat_cool, sonst Quick-Veto) wirkt und ueber ihr Attribut `temperature` gelesen wird; sie
meldet 0, wenn die Zone gerade nicht heizt. Die Hebel haengen an den Manifest-Rollen der unveraenderten
Add-on-Optionen (P2-3)."""
import logging
import math
from collections.abc import Mapping

from heizungsbruecke.manifest import ChannelManifest
from smartheat_core.binding import VAILLANT_MYPYLLANT, BindingDescription

logger = logging.getLogger(__name__)

MANUAL_HVAC_MODE = "heat_cool"
# Darunter ist die Wunschtemperatur kein Sollwert, sondern "Zone inaktiv" (Plan-Praezisierung 11, TP11).
ROOM_SETPOINT_READ_MIN = 5.0
# Server-Plausibilitaet fuer room_setpoint (messages.PLAUSIBLE_RANGES, Contract-Check 11): darueber ist der Wert ein
# Lesefehler und wird nie gemeldet.
ROOM_SETPOINT_READ_MAX = 35.0
LEVER_ROLES = {
    "curve": "curve_current", "room_setpoint": "shift_current", "level": "level_current", "heat_limit": "heat_limit",
    "min_flow": "min_flow",
}


def entity_of(ref: str) -> str:
    return ref.partition("::")[0]


def is_climate(ref: str) -> bool:
    return entity_of(ref).startswith("climate.")


class HaPlantBinding:
    def __init__(self, ha_api, manifest: ChannelManifest, description: BindingDescription = VAILLANT_MYPYLLANT) -> None:
        self._ha_api = ha_api
        self._manifest = manifest
        self.description = description
        # Plan 3b: erfolgreiche Service-Aufrufe (Schreiben, Vorbereitung, Hilfswerte), siehe PlantBinding.
        self.physical_writes = 0

    def has(self, lever: str) -> bool:
        role = LEVER_ROLES.get(lever)
        return role is not None and role in self._manifest.entity_ids

    def ref(self, lever: str) -> str:
        return self._manifest.entity_ids[LEVER_ROLES[lever]]

    def read(self, lever: str) -> float | None:
        value = self._ha_api.get_state(self.ref(lever))
        if lever != "room_setpoint":
            return value
        if not math.isfinite(value):
            return None
        if value > ROOM_SETPOINT_READ_MAX:
            raise ValueError(f"Wunschtemperatur {value} liegt ueber dem plausiblen Maximum {ROOM_SETPOINT_READ_MAX}")
        if value < ROOM_SETPOINT_READ_MIN:
            return None
        return value

    def read_or(self, lever: str, fallback: float | None) -> float | None:
        """Fuer den Snapshot: Live-Wert, sonst `fallback` (Wiederherstellungspunkt). Der Server vergleicht den Wert am
        Tagestick mit seinem gesendeten ("Anlage folgt nicht"); der Rueckfall verhindert den Fehlalarm, solange die
        ruhende Zone 0 liest."""
        try:
            live = self.read(lever)
        except Exception as error:
            logger.warning("%s nicht lesbar (%s), verwende gespeicherten Wert %s", lever, error, fallback)
            return fallback
        return live if live is not None else fallback

    def write(self, lever: str, value: float) -> None:
        """AU-016: HA ueberspringt eine nicht verfuegbare Entity im Service-Aufruf still (HTTP 200); get_raw_state wirft
        bei unavailable/unknown (lokaler HA-Read, kein Cloud-Aufruf)."""
        ref = self.ref(lever)
        entity_id = entity_of(ref)
        self._ha_api.get_raw_state(entity_id)
        if is_climate(ref):
            self._ha_api.set_climate_temperature(entity_id, value)
        else:
            self._ha_api.set_number_value(entity_id, value)
        self.physical_writes += 1

    def needs_preparation(self) -> bool:
        lever = self.description.prepared_lever
        return lever is not None and self.has(lever) and is_climate(self.ref(lever))

    def is_prepared(self) -> bool:
        lever = self.description.prepared_lever
        assert lever is not None
        return self._ha_api.get_raw_state(entity_of(self.ref(lever))) == MANUAL_HVAC_MODE

    def prepare(self) -> bool:
        """Stellt die Zone auf heat_cool; True, wenn umgestellt wurde. Wirft bei Fehlern."""
        if not self.needs_preparation():
            return False
        lever = self.description.prepared_lever
        assert lever is not None
        entity_id = entity_of(self.ref(lever))
        if self._ha_api.get_raw_state(entity_id) == MANUAL_HVAC_MODE:
            return False
        self._ha_api.set_hvac_mode(entity_id, MANUAL_HVAC_MODE)
        self.physical_writes += 1
        logger.warning("Zone %s auf Manuell (%s) gestellt", entity_id, MANUAL_HVAC_MODE)
        return True

    def read_aux(self) -> dict[str, str | float]:
        """Vaillant hat keine Hilfs-Ursprungswerte (description.aux_originals leer)."""
        return {}

    def restore_aux(self, values: Mapping[str, str | float]) -> None:
        return None


# Plan 3b, Weishaupt (weishaupt_modbus, Spec 1.2/5.4). Annahme bis zur Inventur: Optionstext der Betriebsart "Normal"
# (Register 41103: Automatik, Komfort, Normal, Absenkbetrieb, Standby).
WEISHAUPT_NORMAL_MODE = "Normal"
# Hilfs-Sollwerte gelten als unveraendert innerhalb eines halben Geraeteschritts (0,5 K).
AUX_SETPOINT_TOLERANCE = 0.25


class WeishauptHaBinding(HaPlantBinding):
    """Weishaupt-Waermepumpe ueber weishaupt_modbus: Hebel sind number-Entities (Heizkennlinie, Raumsolltemperatur
    Normal, Sommer-Winter-Umschaltung; Rollen curve_current, shift_current, heat_limit -- "shift_current" ist der
    historische Rollenname). Vorbereitung: Betriebsart-Select (Rolle mode_select) auf "Normal". Das Geraet erzwingt
    Absenk <= Normal <= Komfort: vor dem Anheben des Normal-Solls ueber das Komfort-Soll wird zuerst Komfort, vor dem
    Senken unter das Absenk-Soll zuerst Absenk auf den neuen Wert gesetzt (Rollen setpoint_comfort, setpoint_setback).
    Scheitert der zweite Schritt, steht die Anlage weiter in einem gueltigen Zustand (nur Komfort bzw. Absenk
    verschoben, beide gehoeren zu den Hilfs-Ursprungswerten)."""

    def write(self, lever: str, value: float) -> None:
        if lever == "room_setpoint":
            self._make_room_for_normal(value)
        super().write(lever, value)

    def _number(self, role: str) -> float:
        value = self._ha_api.get_state(self._manifest.entity_ids[role])
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise ValueError(f"{role} liefert keinen endlichen Wert: {value!r}")
        return value

    def _set_number(self, role: str, value: float) -> None:
        entity_id = self._manifest.entity_ids[role]
        self._ha_api.get_raw_state(entity_id)  # AU-016: nicht verfuegbare Entity wirft statt still 200
        self._ha_api.set_number_value(entity_id, value)
        self.physical_writes += 1
        logger.info("%s (%s) auf %s gesetzt", role, entity_id, value)

    def _make_room_for_normal(self, value: float) -> None:
        if value > self._number("setpoint_comfort"):
            self._set_number("setpoint_comfort", value)
        elif value < self._number("setpoint_setback"):
            self._set_number("setpoint_setback", value)

    def needs_preparation(self) -> bool:
        return "mode_select" in self._manifest.entity_ids

    def is_prepared(self) -> bool:
        return self._ha_api.get_raw_state(self._manifest.entity_ids["mode_select"]) == WEISHAUPT_NORMAL_MODE

    def prepare(self) -> bool:
        """Betriebsart auf "Normal"; True, wenn umgestellt wurde. Wirft bei Fehlern."""
        if not self.needs_preparation() or self.is_prepared():
            return False
        entity_id = self._manifest.entity_ids["mode_select"]
        self._ha_api.select_option(entity_id, WEISHAUPT_NORMAL_MODE)
        self.physical_writes += 1
        logger.warning("Betriebsart %s auf %s gestellt", entity_id, WEISHAUPT_NORMAL_MODE)
        return True

    def read_aux(self) -> dict[str, str | float]:
        return {
            "mode_select": self._ha_api.get_raw_state(self._manifest.entity_ids["mode_select"]),
            "setpoint_comfort": self._number("setpoint_comfort"),
            "setpoint_setback": self._number("setpoint_setback"),
        }

    def restore_aux(self, values: Mapping[str, str | float]) -> None:
        """Komfort und Absenk zurueck, begrenzt auf das aktuelle Normal-Soll (Absenk <= Normal <= Komfort bleibt
        gueltig, auch wenn dessen Ursprungswert unbekannt war), danach die Betriebsart. Schreibt nur Abweichungen."""
        normal = self._number("shift_current")
        for role, bound in (("setpoint_comfort", max), ("setpoint_setback", min)):
            original = values.get(role)
            if not isinstance(original, (int, float)) or isinstance(original, bool):
                continue
            target = bound(original, normal)
            if target != original:
                logger.warning("%s: Ursprungswert %s auf %s begrenzt (Normal-Soll %s)", role, original, target, normal)
            if abs(self._number(role) - target) > AUX_SETPOINT_TOLERANCE:
                self._set_number(role, target)
        mode = values.get("mode_select")
        entity_id = self._manifest.entity_ids["mode_select"]
        if isinstance(mode, str) and self._ha_api.get_raw_state(entity_id) != mode:
            self._ha_api.select_option(entity_id, mode)
            self.physical_writes += 1
            logger.warning("Betriebsart %s auf den Ursprungswert %s zurueckgestellt", entity_id, mode)
