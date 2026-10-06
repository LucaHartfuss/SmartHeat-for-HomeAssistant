"""Raum-Kanal (Plan G2a Praezisierung 5): shg/raum (retained) = {ist, soll, soll_quelle, ts} fuer Agent und Portal;
das Status-Modell Schema 2 bleibt unveraendert. Veroeffentlicht jedes Mal (bei Soll-Aenderung und im 30-s-Takt),
der Zeitstempel ist der Veroeffentlichungszeitpunkt."""
from collections.abc import Callable

from smartheat_gateway import topics
from smartheat_gateway.bus import Bus
from smartheat_gateway.signals import REF_ROOM_MEAN
from smartheat_runtime.ports import SignalNotFound, SignalSource, SourceUnavailable


class RaumPublisher:
    def __init__(self, bus: Bus, signals: SignalSource, store, now_iso: Callable[[], str]) -> None:
        self._bus, self._signals, self._store, self._now_iso = bus, signals, store, now_iso

    def publish(self) -> None:
        try:
            ist = self._signals.get_state(REF_ROOM_MEAN)
        except (ValueError, KeyError, TypeError, SignalNotFound, SourceUnavailable):
            ist = None
        body = {"ist": ist, "soll": self._store.value, "soll_quelle": self._store.source, "ts": self._now_iso()}
        self._bus.publish(topics.RAUM, body, retain=True)
