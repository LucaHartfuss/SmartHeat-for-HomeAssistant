"""Soll-Speicher (Spec SHG G2 3.3, Spec 5c 1.3): <daten>/runtime/room_target.json = {value, source, ts}, geschrieben
nur von der Laufzeit (im Worker-Thread), fuer Gateway und HA-Add-on gleich. Der zuletzt gestellte Wert gilt. Bedienung
vom Server (portal): 15-25 °C in 0,5-K-Schritten (Bedienbereich, keine Sicherheitsgrenze, Spec 5b 3.2; der
Geraetekern prueft vorher, hier erneut). Soll-Quelle (thermostat; HA: Soll-Entity) und lokale Eingabe (lokal; HA:
Ereignis smartheat_wunsch): nur Plausibilitaet 5-35 °C, kein Clamp (den Bedienbereich setzt der Wunsch des
Geraetekerns). Nach eigenem Schreiben an die Soll-Quelle gelten deren Meldungen ECHO_WINDOW_SECONDS lang als Echo;
meldet sie das geltende Soll, aendert sich nichts (auch nicht die Quelle). Ein offener Schreibbefehl (PENDING_SECONDS)
verwirft verspaetete Meldungen des alten Werts, bis die Quelle den geschriebenen Wert meldet; jeder andere Wert gilt
als Eingabe an der Quelle (Plan G2b-2 Task 4).

Die Laufzeit importiert smartheat_device nicht (tests/test_runtime_purity.py); der Bedienbereich
steht deshalb hier (Gleichheit mit smartheat_device.documents.im_bedienbereich sichert test_soll_speicher.py)."""
import logging
from collections.abc import Callable
from pathlib import Path

from smartheat_core import dateien
from smartheat_runtime.plausibility import ROOM_TEMP_RANGE, is_plausible

logger = logging.getLogger(__name__)

SOURCE_PORTAL = "portal"
SOURCE_THERMOSTAT = "thermostat"
SOURCE_LOKAL = "lokal"
_SOURCES = (SOURCE_PORTAL, SOURCE_THERMOSTAT, SOURCE_LOKAL)
BEDIENBEREICH_MIN, BEDIENBEREICH_MAX, BEDIENBEREICH_STEP = 15.0, 25.0, 0.5
ECHO_WINDOW_SECONDS = 60
PENDING_SECONDS = 1800


def im_bedienbereich(value) -> bool:
    """Bedienbereich (Spec 5b 3.2, keine Sicherheitsgrenze): 15-25 °C in 0,5-K-Schritten, wie Server und Geraetekern."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if not BEDIENBEREICH_MIN <= value <= BEDIENBEREICH_MAX:  # zuerst: exakt fuer grosse int, NaN faellt hier durch
        return False
    steps = value / BEDIENBEREICH_STEP
    return abs(steps - round(steps)) < 1e-9


def is_valid_portal_target(value) -> bool:
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
        stored = dateien.lesen_json(path)
        if isinstance(stored, dict) and is_plausible(stored.get("value"), ROOM_TEMP_RANGE) and stored.get("source") in _SOURCES:
            self.value, self.source = float(stored["value"]), stored["source"]
        else:
            self.value, self.source = float(start_value), SOURCE_PORTAL
            self._write()

    def apply_portal(self, value) -> bool:
        if not is_valid_portal_target(value):
            logger.warning("Wunschtemperatur %r aus dem Portal verworfen (erlaubt 15-25 °C in 0,5-K-Schritten)", value)
            return False
        return self._set(float(value), SOURCE_PORTAL)

    def note_own_write(self, veraltet: float | None = None) -> None:
        """veraltet: was das Thermostat zuletzt gemeldet hat, falls bekannt (Wiedereinschalten: der Wert vor dem
        Ausschalten, nicht unbedingt der vorige Wert des Speichers); gilt zusaetzlich als alter Wert."""
        now = self._clock()
        self._echo_until = now + ECHO_WINDOW_SECONDS
        if self._pending is not None and now < self._pending[2]:  # zweiter Befehl vor der Bestaetigung des ersten
            stale = self._pending[1] | {self._pending[0]}
        else:
            stale = frozenset({self._before if self._before is not None else self.value})
        if veraltet is not None:
            stale |= {veraltet}
        self._pending = (self.value, stale - {self.value}, now + PENDING_SECONDS)

    def echo_rest(self) -> float:
        """Sekunden, die das Echo-Fenster nach dem eigenen Schreiben noch laeuft (0 = keins)."""
        if self._echo_until is None:
            return 0.0
        return max(self._echo_until - self._clock(), 0.0)

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

    def apply_lokal(self, value) -> bool:
        """Lokale Eingabe (HA: Wunschtemperatur-Entity der Integration, Spec 5c 1.3): gilt sofort, ohne Echo-Pruefung
        (sie stammt nicht von der Soll-Quelle); nur plausibel 5-35 °C."""
        if not is_plausible(value, ROOM_TEMP_RANGE):
            logger.warning("Lokale Wunschtemperatur %r unplausibel, verworfen", value)
            return False
        return self._set(float(value), SOURCE_LOKAL)

    def begrenzen(self, value: float) -> bool:
        """Begrenzter Wunsch (Spec 5b 3.2): der begrenzte Wert gilt sofort, die Quelle bleibt die des Wunsches
        (thermostat bzw. lokal; der Wunsch hat sie gerade gesetzt)."""
        return self._set(float(value), self.source)

    def wiederherstellen(self, value: float, source: str) -> bool:
        """Verworfener Thermostat-Wunsch (Thermostat aus, Spec 5b 3.2): zurueck auf den Stand vor dem Wunsch."""
        return self._set(float(value), source)

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
        dateien.schreiben_json(self._path, {"value": self.value, "source": self.source, "ts": self._now_iso()},
                               modus=dateien.LESBAR)
