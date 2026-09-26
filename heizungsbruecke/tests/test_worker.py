import logging
import threading
import time

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


def test_request_exit_stops_processing_and_returns_code(clock):
    worker, seen = _recording_worker(clock, "later")
    worker.register("stop", lambda event: worker.request_exit(0))
    worker.post(Event("stop"))
    worker.post(Event("later"))

    assert worker.run_pending() == 0
    assert seen == []


def test_run_returns_exit_code_after_scheduled_event_fires():
    worker = RegulationWorker()  # echte monotone Uhr
    worker.register("stop", lambda event: worker.request_exit(3))
    worker.schedule(0.05, Event("stop"))
    started = time.monotonic()

    assert worker.run() == 3
    assert time.monotonic() - started >= 0.05


def test_run_wakes_up_for_event_posted_from_other_thread():
    worker = RegulationWorker()
    worker.register("stop", lambda event: worker.request_exit(0))
    worker.register("far_future", lambda event: None)
    worker.schedule(60, Event("far_future"))

    threading.Timer(0.05, worker.post, args=(Event("stop"),)).start()

    assert worker.run() == 0
