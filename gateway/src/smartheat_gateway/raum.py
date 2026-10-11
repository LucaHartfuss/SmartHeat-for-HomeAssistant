"""Raum-Kanal (Plan G2a Praezisierung 5, Spec 5b Plan D2): {ist, soll, soll_quelle, ts} der Laufzeit fuer up/status
und die Diagnoseseite, im Prozess (GeraetZustand; bis 0.5.0 retained auf shg/raum fuer den Agenten). Bei
Soll-Aenderung und im 30-s-Takt neu, der Zeitstempel ist der Zeitpunkt der Veroeffentlichung."""
from collections.abc import Callable

from smartheat_gateway.geraet_host import GeraetZustand
from smartheat_gateway.signals import REF_ROOM_MEAN
from smartheat_runtime.ports import SignalNotFound, SignalSource, SourceUnavailable


class RaumPublisher:
    def __init__(self, zustand: GeraetZustand, signals: SignalSource, store, now_iso: Callable[[], str]) -> None:
        self._zustand, self._signals, self._store, self._now_iso = zustand, signals, store, now_iso

    def publish(self) -> None:
        try:
            ist = self._signals.get_state(REF_ROOM_MEAN)
        except (ValueError, KeyError, TypeError, SignalNotFound, SourceUnavailable):
            ist = None
        self._zustand.raum = {"ist": ist, "soll": self._store.value, "soll_quelle": self._store.source,
                              "ts": self._now_iso()}
