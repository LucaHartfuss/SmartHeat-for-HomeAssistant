"""Thermostat-Profile (Fixrunde A, L4-22, Plan 2026-10-21-w100-thermostat): was ein Zigbee-Modell nach dem Koppeln
braucht, um als Thermostat zu arbeiten, und wie es das Soll zeigt. Aqara W100 (TH-S04D), Labor 2026-10-21 und
Konverter lumi.ts: Zigbee2MQTT setzt beim Koppeln thermostat_mode OFF und meldet danach weiter den alten Zustand;
mehrere Schluessel in einer set-Nachricht ueberschreiben sich (jeder rechnet vom alten Zustand), also je Schluessel
eine Nachricht; das Geraet holt Soll und Modus selbst aus dem Zustand von Zigbee2MQTT; es zeigt und meldet nur ganze
Grad. Die Pflege laeuft im Bus-Thread und liest den Soll-Speicher nie: das gebundene Thermostat richtet die Laufzeit
ueber ihre Bindung im Worker ein, jedes andere bekommt DEFAULT_RAUM_SOLL."""
import logging
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from smartheat_runtime.soll_speicher import DEFAULT_RAUM_SOLL

logger = logging.getLogger(__name__)


class _Spiegel(Protocol):
    def set(self, ieee: str, payload: dict) -> None: ...
    def write_setpoint(self, ieee: str, value: float) -> None: ...
    def on_interview(self, hook: Callable[[str, str], None]) -> None: ...


@dataclass(frozen=True)
class ThermostatProfil:
    modus: tuple[dict, ...]  # Thermostatmodus und Anzeige, je eine Nachricht; auch nach Start und Selbstheilung
    heizen: dict  # nach dem Koppeln und wenn das Geraet einen fremden Modus meldet
    fremde_modi: frozenset[str]  # am Geraet waehlbar, fuer die Heizung sinnlos: zurueck auf heizen ("off" nicht)
    eigene_aktionen: frozenset[str]  # Aktionen im Thermostatmodus; jede andere heisst: Geraet nicht im Thermostatmodus
    ganzzahlig: bool

    def anzeige(self, soll: float) -> float:
        """Was das Thermostat zeigen soll: ganzzahlig kaufmaennisch gerundet (20,5 -> 21, Nutzer 2026-10-21)."""
        return float(math.floor(soll + 0.5)) if self.ganzzahlig else soll


_PROFILE = {
    "TH-S04D": ThermostatProfil(
        modus=({"thermostat_mode": "ON"}, {"auto_hide_middle_line": False}),
        heizen={"system_mode": "heat"},
        fremde_modi=frozenset({"cool", "auto"}),
        eigene_aktionen=frozenset({"W100_PMTSD_request"}),
        ganzzahlig=True,
    ),
}


def profil(model: str | None) -> ThermostatProfil | None:
    return _PROFILE.get(model) if isinstance(model, str) else None


def modus_setzen(mirror: _Spiegel, ieee: str, p: ThermostatProfil) -> None:
    for payload in p.modus:
        mirror.set(ieee, payload)


def einrichten(mirror: _Spiegel, ieee: str, p: ThermostatProfil, soll: float) -> None:
    """Nach dem Koppeln: Modus, Anzeige, Soll, heizen, in dieser Reihenfolge und je eine Nachricht."""
    modus_setzen(mirror, ieee, p)
    mirror.write_setpoint(ieee, p.anzeige(soll))
    mirror.set(ieee, p.heizen)


class ThermostatPflege:
    """Am Zigbee-Spiegel: stellt Thermostate mit Profil nach jedem erfolgreichen Interview ein. Hoechstens ein
    gebundenes Thermostat (das der laufenden Konfiguration); eine neue Bindung ersetzt die vorige."""

    def __init__(self, mirror: _Spiegel) -> None:
        self._mirror = mirror
        self._lock = threading.Lock()
        self._gebunden: tuple[str, Callable[[], None]] | None = None
        mirror.on_interview(self._on_interview)

    def binden(self, ieee: str, einrichten_lassen: Callable[[], None]) -> None:
        with self._lock:
            self._gebunden = (ieee, einrichten_lassen)

    def loesen(self, ieee: str) -> None:
        with self._lock:
            if self._gebunden is not None and self._gebunden[0] == ieee:
                self._gebunden = None

    def _on_interview(self, ieee: str, model: str) -> None:
        p = profil(model)
        if p is None:
            return
        with self._lock:
            gebunden = self._gebunden
        if gebunden is not None and gebunden[0] == ieee:
            gebunden[1]()
            return
        logger.info("Thermostat %s (%s) gekoppelt: als Thermostat eingestellt", ieee, model)
        einrichten(self._mirror, ieee, p, DEFAULT_RAUM_SOLL)
