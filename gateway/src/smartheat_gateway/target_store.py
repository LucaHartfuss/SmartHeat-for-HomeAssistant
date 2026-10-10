"""Soll-Speicher (Spec SHG G2 3.3): /data/runtime/room_target.json = {value, source, ts}, geschrieben nur von der
Laufzeit (im Worker-Thread). Der zuletzt gestellte Wert gilt. Portal: 15-25 °C in 0,5-K-Schritten (Regel 4,
Nutzer-Entscheidung 2026-10-06; der Agent prueft vorher, hier erneut; Bedienung vom Server, Spec 5b 3.2;
Bedienbereich, keine Sicherheitsgrenze). Thermostat: nur Plausibilitaet 5-35 °C, kein
Clamp. Nach eigenem Schreiben ans Thermostat gelten dessen Meldungen ECHO_WINDOW_SECONDS lang als Echo; meldet das
Thermostat das geltende Soll, aendert sich nichts (auch nicht die Quelle). Ein offener Schreibbefehl
(PENDING_SECONDS) verwirft verspaetete Meldungen des alten Werts, bis das Thermostat den geschriebenen Wert meldet;
jeder andere Wert gilt als Eingabe am Thermostat (Plan G2b-2 Task 4)."""
import logging
from collections.abc import Callable
from pathlib import Path

from smartheat_device.documents import im_bedienbereich
from smartheat_gateway.files import read_json, write_json
from smartheat_runtime.plausibility import ROOM_TEMP_RANGE, is_plausible

logger = logging.getLogger(__name__)

SOURCE_PORTAL = "portal"
SOURCE_THERMOSTAT = "thermostat"
ECHO_WINDOW_SECONDS = 60
PENDING_SECONDS = 1800


def is_valid_portal_target(value) -> bool:
    """Bedienbereich (Spec 5b 3.2): 15-25 °C in 0,5-K-Schritten, wie Server und Geraetekern."""
    return im_bedienbereich(value)


class TargetStore:
    def __init__(self, path: Path, start_value: float, clock: Callable[[], float], now_iso: Callable[[], str],
                 on_change: Callable[[], None] | None = None) -> None:
        self._path = path
        self._clock = clock
        self._now_iso = now_iso
        self._on_change = on_change
        self._echo_until: float | None = None
        # (geschrieben, veraltete Werte, Frist) - nur im Speicher
        self._pending: tuple[float, frozenset[float], float] | None = None
        self._before: float | None = None  # Wert vor der letzten Aenderung (= was das Thermostat noch zeigt)
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

    def note_own_write(self) -> None:
        now = self._clock()
        self._echo_until = now + ECHO_WINDOW_SECONDS
        if self._pending is not None and now < self._pending[2]:  # zweiter Befehl vor der Bestaetigung des ersten
            stale = self._pending[1] | {self._pending[0]}
        else:
            stale = frozenset({self._before if self._before is not None else self.value})
        self._pending = (self.value, stale - {self.value}, now + PENDING_SECONDS)

    def apply_thermostat(self, value) -> bool:
        if self._echo_until is not None and self._clock() < self._echo_until:
            if self._pending is not None and is_plausible(value, ROOM_TEMP_RANGE) and float(value) == self._pending[0]:
                self._pending = None  # Bestaetigung im Echo-Fenster schliesst den offenen Befehl trotzdem
            return False
        if not is_plausible(value, ROOM_TEMP_RANGE):
            logger.warning("Wunschtemperatur %r vom Thermostat unplausibel, verworfen", value)
            return False
        value = float(value)
        if self._pending is not None:
            written, stale, until = self._pending
            if self._clock() >= until:
                self._pending = None
                if value != written:
                    logger.warning("Thermostat hat das Soll %.1f nicht bestaetigt, sein Wert %.1f gilt", written, value)
            elif value == written:
                self._pending = None
                return False
            elif value in stale:
                logger.info("Thermostat meldet noch einen alten Sollwert %.1f (Schreibbefehl offen), verworfen", value)
                return False
            else:
                self._pending = None
        if value == self.value:  # Thermostat meldet das geltende Soll (Echo, Wiederholung): keine Aenderung
            return False
        return self._set(value, SOURCE_THERMOSTAT)

    def begrenzen(self, value: float) -> bool:
        """Begrenzter Thermostat-Wunsch (Spec 5b 3.2): der begrenzte Wert gilt sofort, Quelle bleibt das Thermostat."""
        return self._set(float(value), SOURCE_THERMOSTAT)

    def _set(self, value: float, source: str) -> bool:
        if value == self.value and source == self.source:
            return False
        self._before = self.value
        self.value, self.source = value, source
        self._write()
        if self._on_change is not None:
            self._on_change()
        return True

    def _write(self) -> None:
        write_json(self._path, {"value": self.value, "source": self.source, "ts": self._now_iso()})
