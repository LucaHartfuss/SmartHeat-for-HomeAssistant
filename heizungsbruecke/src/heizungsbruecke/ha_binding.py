"""HA-Binding der Bruecke (Hersteller-Abstraktion, Plan 2 P2-2): liest und schreibt die Hebel ueber die HA-API.
Alles mypyllant-Spezifische steht hier (vorher plant.py): die Zonen-Wunschtemperatur ist eine Climate-Entity, die
nur im Modus "Manuell" (HA heat_cool, sonst Quick-Veto) wirkt und ueber ihr Attribut `temperature` gelesen wird; sie
meldet 0, wenn die Zone gerade nicht heizt. Die Hebel haengen an den Manifest-Rollen der unveraenderten
Add-on-Optionen (P2-3)."""
import logging
import math

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
