import logging
import threading
import time

import pytest

from heizungsbruecke.worker import Event, RegulationWorker


def _recording_worker(clock, *kinds):
    worker = RegulationWorker(clock=clock)
    seen = []
    for kind in kinds:
        worker.register(kind, seen.append)
    return worker, seen


def _kinds(events):
    return [event.kind for event in events]


def test_posted_event_is_dispatched_to_its_handler(clock):
    worker, seen = _recording_worker(clock, "a")

    worker.post(Event("a", {"x": 1}))

    assert worker.run_pending() is None
    assert seen == [Event("a", {"x": 1})]


def test_due_schedule_entries_run_before_queued_events(clock):
    worker, seen = _recording_worker(clock, "planned", "queued")
    worker.post(Event("queued"))
    worker.schedule(0, Event("planned"))

    worker.run_pending()

    assert _kinds(seen) == ["planned", "queued"]


def test_schedule_entry_waits_until_due(clock):
    worker, seen = _recording_worker(clock, "planned")
    worker.schedule(30, Event("planned"))

    worker.run_pending()
    assert seen == []
    clock.advance(29.9)
    worker.run_pending()
    assert seen == []
    clock.advance(0.1)
    worker.run_pending()
    assert _kinds(seen) == ["planned"]


def test_schedule_orders_by_due_time_then_insertion(clock):
    worker, seen = _recording_worker(clock, "a", "b", "c")
    worker.schedule(10, Event("b"))
    worker.schedule(5, Event("a"))
    worker.schedule(10, Event("c"))

    clock.advance(10)
    worker.run_pending()

    assert _kinds(seen) == ["a", "b", "c"]


def test_cancelled_entry_is_not_dispatched(clock):
    worker, seen = _recording_worker(clock, "a", "b")
    handle = worker.schedule(5, Event("a"))
    worker.schedule(5, Event("b"))

    worker.cancel(handle)
    clock.advance(5)
    worker.run_pending()

    assert _kinds(seen) == ["b"]


def test_coalesced_posts_produce_one_event_with_ored_flags(clock):
    worker, seen = _recording_worker(clock, "local_check")

    worker.post_coalesced("local_check", room_target_fired=False)
    worker.post_coalesced("local_check", room_target_fired=True)
    worker.post_coalesced("local_check", room_target_fired=False)
    worker.run_pending()

    assert seen == [Event("local_check", {"room_target_fired": True})]


def test_coalescing_starts_fresh_after_dispatch(clock):
    worker, seen = _recording_worker(clock, "local_check")

    worker.post_coalesced("local_check", room_target_fired=True)
    worker.run_pending()
    worker.post_coalesced("local_check", room_target_fired=False)
    worker.run_pending()

    assert seen == [
        Event("local_check", {"room_target_fired": True}),
        Event("local_check", {"room_target_fired": False}),
    ]


def test_coalescing_from_many_threads_yields_single_event(clock):
    # Review Focus 5: eine room_actual-Flut aus dem WS-Thread plus ein room_target-Trigger.
    worker, seen = _recording_worker(clock, "local_check")

    def flood(fired):
        for _ in range(200):
            worker.post_coalesced("local_check", room_target_fired=fired)

    threads = [threading.Thread(target=flood, args=(index == 2,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    worker.run_pending()

    assert seen == [Event("local_check", {"room_target_fired": True})]


def test_handler_exception_is_logged_and_worker_continues(clock, caplog):
    worker, seen = _recording_worker(clock, "ok")

    def boom(event):
        raise RuntimeError("kaputt")

    worker.register("boom", boom)
    worker.post(Event("boom"))
    worker.post(Event("ok"))

    with caplog.at_level(logging.ERROR):
        worker.run_pending()

    assert _kinds(seen) == ["ok"]
    assert "boom" in caplog.text


def test_unknown_event_kind_is_logged_and_dropped(clock, caplog):
    worker, seen = _recording_worker(clock, "ok")
    worker.post(Event("niemand"))
    worker.post(Event("ok"))

    with caplog.at_level(logging.WARNING):
        worker.run_pending()

    assert _kinds(seen) == ["ok"]
    assert "niemand" in caplog.text


def test_handler_can_schedule_follow_up_with_zero_delay(clock):
    worker, seen = _recording_worker(clock, "second")
    worker.register("first", lambda event: worker.schedule(0, Event("second")))

    worker.post(Event("first"))
    worker.run_pending()

    assert _kinds(seen) == ["second"]


class _Stop(BaseException):
    """run() endet nie von selbst; der Test bricht die Schleife ueber eine BaseException ab,
    die _dispatch (faengt nur Exception) nicht abfaengt."""


def _raise_stop(event):
    raise _Stop


def test_run_processes_a_scheduled_event_when_it_is_due():
    worker = RegulationWorker()  # echte monotone Uhr
    worker.register("stop", _raise_stop)
    worker.schedule(0.05, Event("stop"))
    started = time.monotonic()

    with pytest.raises(_Stop):
        worker.run()
    assert time.monotonic() - started >= 0.05


def test_run_wakes_up_for_event_posted_from_other_thread():
    worker = RegulationWorker()
    worker.register("stop", _raise_stop)
    worker.register("far_future", lambda event: None)
    worker.schedule(60, Event("far_future"))

    threading.Timer(0.05, worker.post, args=(Event("stop"),)).start()

    with pytest.raises(_Stop):
        worker.run()


def test_after_each_runs_after_every_handled_event_and_its_errors_are_logged(clock, caplog):
    worker = RegulationWorker(clock=clock)
    seen = []
    worker.register("a", lambda event: seen.append("a"))
    calls = {"n": 0}

    def _after():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("kaputt")

    worker.after_each(_after)
    worker.post(Event("a"))
    worker.post(Event("a"))
    worker.post(Event("unbekannt"))

    with caplog.at_level(logging.ERROR):
        worker.run_pending()

    assert seen == ["a", "a"]
    assert calls["n"] == 2  # nicht nach dem verworfenen Ereignis ohne Handler
    assert "kaputt" in caplog.text
