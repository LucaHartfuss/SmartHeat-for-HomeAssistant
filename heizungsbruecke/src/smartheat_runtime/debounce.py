"""Entprellung der Wunschtemperatur (Spec SHG G2 2.4): eine Aenderung gilt erst nach ROOM_TARGET_DEBOUNCE_SECONDS
Stabilitaet. Der HA-Host entprellt ueber `for:` im WebSocket-Trigger (gleiches Fenster), das Gateway mit Debouncer."""
from collections.abc import Callable

from smartheat_runtime.worker import Event, RegulationWorker

ROOM_TARGET_DEBOUNCE_SECONDS = 10


class Debouncer:
    """poke() nur aus dem Worker-Thread oder vor worker.run() (worker.schedule). Jede Aenderung verschiebt den
    Zeitpunkt; on_stable laeuft einmal, wenn seit dem letzten poke() `seconds` vergangen sind."""

    def __init__(self, worker: RegulationWorker, kind: str, seconds: float, on_stable: Callable[[], None]) -> None:
        self._worker = worker
        self._kind = kind
        self._seconds = seconds
        self._on_stable = on_stable
        self._generation = 0
        worker.register(kind, self._on_due)

    def poke(self) -> None:
        self._generation += 1
        self._worker.schedule(self._seconds, Event(self._kind, {"gen": self._generation}))

    def _on_due(self, event: Event) -> None:
        if event.data.get("gen") == self._generation:
            self._on_stable()
