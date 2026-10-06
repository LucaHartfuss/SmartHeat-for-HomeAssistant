"""Agent-Zustand fuer LED und Diagnose (Spec SHG G2 6.5): jedes Mal neu abgeleitet, als /data/agent_state.json
mindestens jede Minute neu geschrieben (der LED-Dienst zeigt eine Datei aelter als 5 min als stoerung, G2b).
abo_beendet zaehlt als stoerung (Plan G2a: steht nicht in der Tabelle der Spec, braucht aber Aufmerksamkeit).
Die Datei wird nie gelesen, nur ueberschrieben: eine kaputte Datei (Stromausfall) stoert nicht (Review Focus 5)."""
from collections.abc import Callable
from pathlib import Path

from smartheat_gateway.files import write_json

SERVER_DOWN_SECONDS = 300
_RUNNING = ("regelt", "abo_inaktiv")
_FAULT = ("notbetrieb", "datenfehler", "konfigurationsfehler", "zugang_abgelehnt", "abo_beendet")


def derive(*, server_seen: bool, server_down_seconds: float | None, device_state: str | None,
           runtime_status: dict | None) -> str:
    if not server_seen:
        return "startet"
    if server_down_seconds is not None and server_down_seconds > SERVER_DOWN_SECONDS:
        return "keine_verbindung"
    if device_state != "uebernommen":
        return "nicht_uebernommen"
    status = (runtime_status or {}).get("status")
    if status in (None, "abgemeldet"):
        return "wartet_auf_einrichtung"
    if status in _RUNNING:
        return "regelt"
    if status in _FAULT:
        return "stoerung"
    return "startet"


class AgentStateWriter:
    def __init__(self, path: Path, now_iso: Callable[[], str], clock: Callable[[], float], every: float = 60.0) -> None:
        self._path, self._now_iso, self._clock, self._every = path, now_iso, clock, every
        self._last: tuple[str, float] | None = None
        self._since: str | None = None

    def update(self, zustand: str) -> None:
        now = self._clock()
        if self._last is not None and self._last[0] == zustand and now - self._last[1] < self._every:
            return
        if self._last is None or self._last[0] != zustand:
            self._since = self._now_iso()
        write_json(self._path, {"zustand": zustand, "seit": self._since, "ts": self._now_iso()})
        self._last = (zustand, now)
