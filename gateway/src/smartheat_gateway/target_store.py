"""Soll-Speicher (Spec SHG G2 3.3): /data/runtime/room_target.json = {value, source, ts}, geschrieben nur von der
Laufzeit (im Worker-Thread). Der zuletzt gestellte Wert gilt. Portal: 15-25 °C in 0,5-K-Schritten (Regel 4,
Nutzer-Entscheidung 2026-10-06; der Agent prueft vorher, hier erneut). Thermostat: nur Plausibilitaet 5-35 °C, kein
Clamp. Nach eigenem Schreiben ans Thermostat gelten dessen Meldungen ECHO_WINDOW_SECONDS lang als Echo."""
import logging
import math
from collections.abc import Callable
from pathlib import Path

from smartheat_gateway.agent import wire
from smartheat_gateway.files import read_json, write_json
from smartheat_runtime.plausibility import ROOM_TEMP_RANGE, is_plausible

logger = logging.getLogger(__name__)

SOURCE_PORTAL = "portal"
SOURCE_THERMOSTAT = "thermostat"
ECHO_WINDOW_SECONDS = 60


def is_valid_portal_target(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return False
    if not wire.ROOM_TARGET_MIN <= value <= wire.ROOM_TARGET_MAX:
        return False
    steps = value / wire.ROOM_TARGET_STEP
    return abs(steps - round(steps)) < 1e-9


class TargetStore:
    def __init__(self, path: Path, start_value: float, clock: Callable[[], float], now_iso: Callable[[], str],
                 on_change: Callable[[], None] | None = None) -> None:
        self._path = path
        self._clock = clock
        self._now_iso = now_iso
        self._on_change = on_change
        self._echo_until: float | None = None
        stored = read_json(path)
        if isinstance(stored, dict) and is_plausible(stored.get("value"), ROOM_TEMP_RANGE) and stored.get("source") in (
            SOURCE_PORTAL, SOURCE_THERMOSTAT,
        ):
            self.value, self.source = float(stored["value"]), stored["source"]
        else:
            self.value, self.source = float(start_value), SOURCE_PORTAL
            self._write()

    def apply_portal(self, value) -> bool:
        if not is_valid_portal_target(value):
            logger.warning("Wunschtemperatur %r aus dem Portal verworfen (erlaubt 15-25 °C in 0,5-K-Schritten)", value)
            return False
        return self._set(float(value), SOURCE_PORTAL)

    def apply_thermostat(self, value) -> bool:
        if self._echo_until is not None and self._clock() < self._echo_until:
            return False
        if not is_plausible(value, ROOM_TEMP_RANGE):
            logger.warning("Wunschtemperatur %r vom Thermostat unplausibel, verworfen", value)
            return False
        return self._set(float(value), SOURCE_THERMOSTAT)

    def note_own_write(self) -> None:
        self._echo_until = self._clock() + ECHO_WINDOW_SECONDS

    def _set(self, value: float, source: str) -> bool:
        if value == self.value and source == self.source:
            return False
        self.value, self.source = value, source
        self._write()
        if self._on_change is not None:
            self._on_change()
        return True

    def _write(self) -> None:
        write_json(self._path, {"value": self.value, "source": self.source, "ts": self._now_iso()})
