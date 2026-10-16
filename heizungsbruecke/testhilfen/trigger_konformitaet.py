"""Ereignisvertrag der Trigger-Quellen (smartheat_runtime/ports.py TriggerSource; Audit 5 A5-58; Spec Stufe 3 S6 4.1),
fuer beide Hosts: (Wieder-)Verbindung -> EV_LOCAL_CHECK(room_target_fired=True) und EV_SOURCE_CONNECTED; Meldung eines
zugeordneten Raumfuehlers -> EV_LOCAL_CHECK(room_target_fired=False), eine Flut ergibt einen Check; Tagestick ->
EV_LOCAL_CHECK(room_target_fired=False). Die Entprellung der Wunschtemperatur (10 s) gehoert nicht dazu: HA entprellt im
HA-Trigger (for:), das Gateway im eigenen Debouncer (je Host eigene Tests)."""
from collections.abc import Callable
from dataclasses import dataclass

from smartheat_runtime.runtime import EV_LOCAL_CHECK, EV_SOURCE_CONNECTED
from smartheat_runtime.worker import RegulationWorker


def beobachten(worker: RegulationWorker) -> list:
    seen: list = []
    worker.register(EV_LOCAL_CHECK, lambda event: seen.append(("check", bool(event.data.get("room_target_fired")))))
    worker.register(EV_SOURCE_CONNECTED, lambda event: seen.append(("connected", None)))
    return seen


@dataclass
class TriggerWelt:
    run: Callable[[], None]  # Worker abarbeiten
    seen: list  # aus beobachten()
    verbinden: Callable[[], None]  # Quelle trennt und verbindet neu
    sensor_meldung: Callable[[], None]  # ein zugeordneter Raumfuehler meldet einen Wert
    tagestick: Callable[[], None]  # der Tagestick faellt


def _still(w: TriggerWelt) -> None:
    w.run()
    w.seen.clear()


def verbindung_meldet_frischen_check_und_verbunden(w: TriggerWelt) -> None:
    _still(w)
    w.verbinden()
    w.run()
    assert ("check", True) in w.seen and ("connected", None) in w.seen


def fuehlermeldung_ist_ein_check_ohne_soll(w: TriggerWelt) -> None:
    _still(w)
    w.sensor_meldung()
    w.run()
    assert w.seen == [("check", False)]


def eine_flut_von_fuehlermeldungen_ist_ein_check(w: TriggerWelt) -> None:
    _still(w)
    for _ in range(10):
        w.sensor_meldung()
    w.run()
    assert w.seen == [("check", False)]


def tagestick_ist_ein_check_ohne_soll(w: TriggerWelt) -> None:
    _still(w)
    w.tagestick()
    w.run()
    assert w.seen == [("check", False)]


PRUEFUNGEN = (verbindung_meldet_frischen_check_und_verbunden, fuehlermeldung_ist_ein_check_ohne_soll,
              eine_flut_von_fuehlermeldungen_ist_ein_check, tagestick_ist_ein_check_ohne_soll)
