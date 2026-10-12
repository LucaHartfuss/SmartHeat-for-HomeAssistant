"""Raum fuer up/status (Spec 5b 3.3, Spec 5c 3: das Portal zeigt Raumtemperatur und Soll auch fuer HA-Geraete), wie
gateway/raum.py: {ist, soll, soll_quelle, ts} mit dem Raumfuehler-Mittel (Rolle room_actual) und dem Soll-Speicher.
Im Worker-Thread bei jeder Soll-Aenderung und im 30-s-Takt (HaHost); der Geraete-Thread liest das zuletzt abgelegte
Dict ueber HaDeviceHost.raum(). Der Zeitstempel ist der Zeitpunkt der Veroeffentlichung (der Geraetekern vergleicht
ohne ihn, A4-35)."""
from collections.abc import Callable

from smartheat_runtime.ports import SignalSource
from smartheat_runtime.soll_speicher import TargetStore


class RaumPublisher:
    def __init__(self, ablegen: Callable[[dict], None], signals: SignalSource, store: TargetStore,
                 room_actual: str | None, now_iso: Callable[[], str]) -> None:
        self._ablegen, self._signals, self._store = ablegen, signals, store
        self._room_actual, self._now_iso = room_actual, now_iso

    def publish(self) -> None:
        ist = None
        if self._room_actual:
            try:
                ist = self._signals.get_state(self._room_actual)
            except Exception:  # unavailable, HA weg: ohne Ist, der naechste Takt liest erneut
                ist = None
        self._ablegen({"ist": ist, "soll": self._store.value, "soll_quelle": self._store.source,
                       "ts": self._now_iso()})
