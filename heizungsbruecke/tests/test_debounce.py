from conftest import FakeClock

from smartheat_runtime.debounce import ROOM_TARGET_DEBOUNCE_SECONDS, Debouncer
from smartheat_runtime.worker import RegulationWorker


def test_fires_once_after_the_value_was_stable():
    clock = FakeClock()
    worker = RegulationWorker(clock=clock)
    fired = []
    debouncer = Debouncer(worker, "test_debounce", ROOM_TARGET_DEBOUNCE_SECONDS, lambda: fired.append(clock()))
    debouncer.poke()
    clock.advance(6)
    debouncer.poke()  # zweite Aenderung innerhalb des Fensters
    clock.advance(6)
    worker.run_pending()
    assert fired == []  # erst 6 s seit der letzten Aenderung
    clock.advance(4)
    worker.run_pending()
    assert fired == [clock()]
    clock.advance(60)
    worker.run_pending()
    assert len(fired) == 1


def test_the_ha_trigger_keeps_the_same_window():
    from heizungsbruecke import triggers
    assert triggers.ROOM_TARGET_DEBOUNCE_SECONDS == ROOM_TARGET_DEBOUNCE_SECONDS == 10
