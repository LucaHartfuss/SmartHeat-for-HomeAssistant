"""Raum fuer up/status (Spec 5b 3.3, Spec 5c 3; Audit 5 A5-26): {ist, soll, soll_quelle, ts} aus dem Raumfuehler-Mittel
und dem Soll-Speicher, fuer beide Hosts. Im Worker-Thread bei jeder Soll-Aenderung und im 30-s-Takt; abgelegt ueber den
Rueckruf des Hosts (HA: HaDeviceHost.raum_merken, Gateway: GeraetZustand.raum). Der Zeitstempel ist der Zeitpunkt der
Veroeffentlichung (der Geraetekern vergleicht ohne ihn, A4-35). Ein nicht lesbarer Fuehler ergibt ist=None und nie
einen Fehler im Worker (der Takt darf nicht abreissen)."""
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
