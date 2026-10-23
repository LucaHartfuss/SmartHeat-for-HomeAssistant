"""Arbeits-Thread fuer blockierende Aufrufe von Befehlen (Gateway: Treiber, HA: Inventur), neben dem Geraete-Thread: der
Handler liefert Waiting, dessen check im Geraete-Thread nur das Future liest. Aus gateway/befehle.py (bis 5c)."""
import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future

HINTERGRUND_NAME = "befehle-hintergrund"


class Hintergrund:
    """Ein Arbeits-Thread fuer blockierende Aufrufe (der Reihe nach). Daemon: ein
    laufender Aufruf haelt das Prozessende (Neustart, SIGTERM) nie auf; ThreadPoolExecutor wartet beim Beenden auf
    seine Threads. Der Thread startet beim ersten Aufruf. stop() bricht wartende Aufrufe ab; ein laufender endet
    selbst (Zeitgrenzen der Treiber bzw. der HA-API), sein Ergebnis liest niemand mehr."""

    def __init__(self) -> None:
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stopped = False

    def submit(self, call: Callable[[], object]) -> Future:
        future: Future = Future()
        with self._lock:
            if self._stopped:
                future.set_exception(RuntimeError("Hintergrund der Befehle ist gestoppt"))
                return future
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name=HINTERGRUND_NAME, daemon=True)
                self._thread.start()
            self._queue.put((future, call))
        return future

    def stop(self, timeout: float = 0.0) -> None:
        """Stoppt den Hintergrund. Mit timeout > 0 wartet stop() danach hoechstens so lange auf das Ende des Threads
        (ein haengender Treiber-Aufruf haelt den Aufrufer nie laenger auf); ohne timeout kehrt es sofort zurueck."""
        with self._lock:
            self._stopped = True
            while True:
                try:
                    future, _ = self._queue.get_nowait()
                except queue.Empty:
                    break
                future.cancel()
            thread = self._thread
            if thread is not None:
                self._queue.put(None)
        if thread is not None and timeout > 0:
            thread.join(timeout)

    def _run(self) -> None:
        while (item := self._queue.get()) is not None:
            future, call = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = call()
            except BaseException as error:  # das Future traegt die Ausnahme; check() wirft sie im Geraete-Thread
                future.set_exception(error)
            else:
                future.set_result(result)
