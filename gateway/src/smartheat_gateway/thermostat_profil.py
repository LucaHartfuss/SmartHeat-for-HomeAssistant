"""Thermostat-Profile (Fixrunde A, L4-22, Plan 2026-10-21-w100-thermostat): was ein Zigbee-Modell nach dem Koppeln
braucht, um als Thermostat zu arbeiten, und wie es das Soll zeigt. Aqara W100 (TH-S04D), Labor 2026-10-21 und
Konverter lumi.ts: Zigbee2MQTT setzt beim Koppeln thermostat_mode OFF und meldet danach weiter den alten Zustand;
mehrere Schluessel in einer set-Nachricht ueberschreiben sich (jeder rechnet vom alten Zustand), also je Schluessel
eine Nachricht; das Geraet holt Soll und Modus selbst aus dem Zustand von Zigbee2MQTT; es zeigt und meldet nur ganze
Grad. Die Pflege laeuft im Bus-Thread und liest den Soll-Speicher nie: das gebundene Thermostat richtet die Laufzeit
ueber ihre Bindung im Worker ein, jedes andere bekommt DEFAULT_RAUM_SOLL. Soll und heizen gehen erst hinaus, wenn das
Geraet thermostat_mode ON gemeldet hat (Labor 2026-10-22, L4-30): der Konverter verwirft beide, solange sein
zwischengespeicherter Zustand nicht ON ist, und ON steht dort erst nach der Meldung des Geraets; bis dahin merkt sich
die Pflege je Geraet einen offenen Rest, ohne Timer und ohne Wiederholung. Dasselbe Warten auf ON (nach_an) nutzt das
Angleichen beim Start; eine Bindung uebernimmt den offenen Rest einer ungebundenen Kopplung (L4-30)."""
import logging
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from smartheat_runtime.soll_speicher import DEFAULT_RAUM_SOLL

logger = logging.getLogger(__name__)

MODUS_AN = "ON"  # thermostat_mode des W100 im Thermostatmodus


class _Spiegel(Protocol):
    def payload(self, ieee: str) -> dict: ...
    def on_device_message(self, hook: Callable[[str, dict], None]) -> None: ...
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


def soll_und_heizen(mirror: _Spiegel, ieee: str, p: ThermostatProfil, soll: float) -> None:
    """Soll, dann heizen, je eine Nachricht; der Aufrufer sorgt dafuer, dass das Geraet thermostat_mode ON gemeldet hat."""
    mirror.write_setpoint(ieee, p.anzeige(soll))
    mirror.set(ieee, p.heizen)


class ThermostatPflege:
    """Am Zigbee-Spiegel: stellt Thermostate mit Profil nach jedem erfolgreichen Interview ein. Hoechstens ein
    gebundenes Thermostat (das der laufenden Konfiguration); eine neue Bindung ersetzt die vorige."""

    def __init__(self, mirror: _Spiegel) -> None:
        self._mirror = mirror
        self._lock = threading.Lock()
        self._gebunden: tuple[str, Callable[[], None]] | None = None
        #: je Geraet hoechstens ein Rest, der nach ON laeuft, und ob er aus einer ungebundenen Kopplung stammt
        self._offen: dict[str, tuple[Callable[[], None], bool]] = {}
        mirror.on_interview(self._on_interview)
        mirror.on_device_message(self._on_geraet)

    def binden(self, ieee: str, einrichten_lassen: Callable[[], None]) -> None:
        """Wartet das Geraet noch auf ON mit dem Rest einer ungebundenen Kopplung (Soll DEFAULT_RAUM_SOLL), uebernimmt
        die Bindung die Einrichtung (L4-30): der Rest faellt weg, einrichten_lassen() laeuft sofort (im Thread des
        Aufrufers) und richtet mit dem Soll aus dem Soll-Speicher ein. Jeder andere offene Rest bleibt."""
        with self._lock:
            self._gebunden = (ieee, einrichten_lassen)
            rest = self._offen.get(ieee)
            uebernehmen = rest is not None and rest[1]
            if uebernehmen:
                del self._offen[ieee]
        if uebernehmen:
            einrichten_lassen()

    def loesen(self, ieee: str) -> None:
        with self._lock:
            if self._gebunden is not None and self._gebunden[0] == ieee:
                self._gebunden = None
            self._offen.pop(ieee, None)

    def einrichten(self, ieee: str, p: ThermostatProfil, abschluss: Callable[[], None]) -> None:
        """Nach dem Koppeln: Thermostatmodus und Anzeige sofort (je eine Nachricht), danach abschluss() (Soll, heizen)
        wie bei nach_an."""
        self._einrichten(ieee, p, abschluss, ungebunden=False)

    def nach_an(self, ieee: str, abschluss: Callable[[], None]) -> None:
        """abschluss() sobald das Geraet thermostat_mode ON gemeldet hat, sofort, wenn der Spiegel ON schon kennt (auch
        aus einer retained Meldung: sie ist der Zustand von Zigbee2MQTT). abschluss laeuft im Bus-Thread oder im Thread
        des Aufrufers, genau einmal; ein neuer Aufruf fuer dasselbe Geraet ersetzt den offenen Rest."""
        self._merken(ieee, abschluss, ungebunden=False)

    def _einrichten(self, ieee: str, p: ThermostatProfil, abschluss: Callable[[], None], *, ungebunden: bool) -> None:
        modus_setzen(self._mirror, ieee, p)
        self._merken(ieee, abschluss, ungebunden=ungebunden)

    def _merken(self, ieee: str, abschluss: Callable[[], None], *, ungebunden: bool) -> None:
        with self._lock:  # erst merken, dann pruefen: eine ON-Meldung dazwischen findet den Rest oder wird gesehen
            self._offen[ieee] = (abschluss, ungebunden)
        if self._mirror.payload(ieee).get("thermostat_mode") == MODUS_AN:
            self._ausfuehren(ieee)

    def _ausfuehren(self, ieee: str) -> None:
        with self._lock:
            rest = self._offen.pop(ieee, None)
        if rest is not None:
            rest[0]()

    def _on_geraet(self, ieee: str, payload: dict) -> None:
        if payload.get("thermostat_mode") == MODUS_AN:
            self._ausfuehren(ieee)

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
        self._einrichten(ieee, p, lambda: soll_und_heizen(self._mirror, ieee, p, DEFAULT_RAUM_SOLL), ungebunden=True)
