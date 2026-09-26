"""Zentraler Regel-Worker.

Alle Regelungsereignisse laufen nacheinander in genau einem Thread (dem Hauptthread).
WS- und paho-Thread stellen nur ein (`post`/`post_coalesced`), Zeitplan-Eintraege
(`schedule`) ersetzen threading.Timer und die fruehere Watchdog-Schleife. Damit braucht
der Regelzustand keinen Lock mehr.
"""
import heapq
import itertools
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Event:
    kind: str
    data: dict = field(default_factory=dict)


@dataclass(frozen=True)
class _CoalescedMarker:
    kind: str


Handler = Callable[[Event], None]


class RegulationWorker:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._queue: queue.Queue = queue.Queue()
        self._handlers: dict[str, Handler] = {}
        self._schedule: list[tuple[float, int, Event]] = []
        self._cancelled: set[int] = set()
        self._handles = itertools.count()
        self._coalesce_lock = threading.Lock()
        self._coalesced: dict[str, dict[str, bool]] = {}
        self._exit_code: int | None = None

    def register(self, kind: str, handler: Handler) -> None:
        self._handlers[kind] = handler

    def post(self, event: Event) -> None:
        """Threadsicher: stellt ein Ereignis zur sofortigen Verarbeitung ein."""
        self._queue.put(event)

    def post_coalesced(self, kind: str, **flags: bool) -> None:
        """Threadsicher. Liegt `kind` noch unverarbeitet in der Queue, entsteht kein
        zweiter Eintrag; die Flags werden per ODER in den wartenden Eintrag uebernommen.
        So ergibt eine Flut von room_actual-Events einen einzigen lokalen Check, ohne
        dass ein gleichzeitiger room_target-Trigger verloren geht."""
        with self._coalesce_lock:
            waiting = self._coalesced.get(kind)
            if waiting is not None:
                for name, value in flags.items():
                    waiting[name] = waiting.get(name, False) or value
                return
            self._coalesced[kind] = dict(flags)
            self._queue.put(_CoalescedMarker(kind))

    def schedule(self, delay_s: float, event: Event) -> int:
        """Nur aus dem Worker-Thread (Handler) oder vor `run()` aufrufen."""
        handle = next(self._handles)
        heapq.heappush(self._schedule, (self._clock() + max(delay_s, 0.0), handle, event))
        return handle

    def cancel(self, handle: int) -> None:
        self._cancelled.add(handle)

    def request_exit(self, code: int) -> None:
        self._exit_code = code

    def run(self) -> int:
        """Verarbeitet Ereignisse, bis ein Handler `request_exit` aufruft, und gibt dessen
        Exit-Code zurueck. Faellige Planeintraege gehen neuen Queue-Eintraegen vor."""
        while self._exit_code is None:
            event = self._pop_due()
            if event is None:
                try:
                    item = self._queue.get(timeout=self._seconds_until_next_due())
                except queue.Empty:
                    continue
                event = self._resolve(item)
            self._dispatch(event)
        return self._exit_code

    def run_pending(self) -> int | None:
        """Verarbeitet alles, was jetzt faellig bzw. eingestellt ist, ohne zu blockieren
        (Test-Einstieg mit Fake-Uhr). Gibt den Exit-Code zurueck, falls angefordert."""
        while self._exit_code is None:
            event = self._pop_due()
            if event is None:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                event = self._resolve(item)
            self._dispatch(event)
        return self._exit_code

    def _drop_cancelled_head(self) -> None:
        while self._schedule and self._schedule[0][1] in self._cancelled:
            _, handle, _ = heapq.heappop(self._schedule)
            self._cancelled.discard(handle)

    def _pop_due(self) -> Event | None:
        self._drop_cancelled_head()
        if self._schedule and self._schedule[0][0] <= self._clock():
            return heapq.heappop(self._schedule)[2]
        return None

    def _seconds_until_next_due(self) -> float | None:
        self._drop_cancelled_head()
        if not self._schedule:
            return None
        return max(self._schedule[0][0] - self._clock(), 0.0)

    def _resolve(self, item) -> Event:
        if isinstance(item, _CoalescedMarker):
            with self._coalesce_lock:
                flags = self._coalesced.pop(item.kind, {})
            return Event(item.kind, flags)
        return item

    def _dispatch(self, event: Event) -> None:
        handler = self._handlers.get(event.kind)
        if handler is None:
            logger.warning("Kein Handler fuer Ereignis '%s', verworfen", event.kind)
            return
        try:
            handler(event)
        except Exception:
            logger.exception("Fehler bei der Verarbeitung von Ereignis '%s', Worker laeuft weiter", event.kind)
