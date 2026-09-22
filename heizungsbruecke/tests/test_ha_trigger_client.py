import json
import threading
import time
from unittest.mock import MagicMock, patch

from heizungsbruecke.ha_trigger_client import HaTriggerClient


def _wait_until(predicate, timeout=2.0):
    # Polls via a private Event's wait(), not time.sleep(): several tests in this file
    # monkeypatch heizungsbruecke.ha_trigger_client.time.sleep, which -- since `time` is
    # one shared module object -- patches time.sleep globally, not just inside that
    # module. Using time.sleep here too used to let this helper's own polling ticks leak
    # into e.g. test_reconnect_uses_increasing_backoff_and_resubscribes's captured
    # sleep_calls list, making that test's outcome depend on GIL scheduling luck.
    deadline = time.time() + timeout
    poll_gate = threading.Event()
    while time.time() < deadline:
        if predicate():
            return
        poll_gate.wait(0.01)
    raise AssertionError("condition not met within timeout")


def _patch_ws_app(captured):
    """Captures the callbacks HaTriggerClient registers on WebSocketApp and hands back
    a fake app whose run_forever() returns immediately (instead of blocking like the
    real one) -- lets the test drive the connection by calling the captured callbacks
    directly, and lets the background thread's reconnect loop actually run.
    """
    def _factory(url, on_message=None, on_close=None, on_error=None):
        fake_app = MagicMock()
        fake_app.run_forever = MagicMock(return_value=None)
        captured["calls"] = captured.get("calls", 0) + 1
        captured["url"] = url
        captured["on_message"] = on_message
        captured["on_close"] = on_close
        captured["on_error"] = on_error
        captured["app"] = fake_app
        return fake_app
    return patch("heizungsbruecke.ha_trigger_client.websocket.WebSocketApp", side_effect=_factory)


def test_auth_handshake_sends_auth_then_subscribes_with_full_trigger_list():
    captured = {}
    triggers = [
        {"platform": "state", "entity_id": "sensor.room_target"},
        {"platform": "state", "entity_id": "sensor.room_actual"},
        {"platform": "time", "at": "12:00"},
    ]
    with _patch_ws_app(captured):
        client = HaTriggerClient(
            ws_url="ws://x/api/websocket", token="tok123", triggers=triggers, on_trigger_event=MagicMock(),
        )
        client.start()
        _wait_until(lambda: "on_message" in captured)

        on_message = captured["on_message"]
        fake_app = captured["app"]

        on_message(fake_app, json.dumps({"type": "auth_required"}))
        fake_app.send.assert_called_once_with(json.dumps({"type": "auth", "access_token": "tok123"}))

        fake_app.send.reset_mock()
        on_message(fake_app, json.dumps({"type": "auth_ok"}))
        fake_app.send.assert_called_once_with(
            json.dumps({"id": 1, "type": "subscribe_trigger", "trigger": triggers})
        )
        assert client.connected is False  # subscribe result not received yet

        client.stop()


def test_connected_becomes_true_only_after_subscribe_result_success():
    captured = {}
    with _patch_ws_app(captured):
        client = HaTriggerClient(
            ws_url="ws://x", token="t", triggers=[{"platform": "time", "at": "12:00"}],
            on_trigger_event=MagicMock(),
        )
        client.start()
        _wait_until(lambda: "on_message" in captured)
        on_message = captured["on_message"]
        fake_app = captured["app"]

        on_message(fake_app, json.dumps({"type": "auth_required"}))
        on_message(fake_app, json.dumps({"type": "auth_ok"}))
        assert client.connected is False

        on_message(fake_app, json.dumps({"id": 1, "type": "result", "success": True, "result": None}))
        assert client.connected is True

        client.stop()


def test_connected_becomes_false_after_on_close():
    captured = {}
    with _patch_ws_app(captured):
        client = HaTriggerClient(
            ws_url="ws://x", token="t", triggers=[{"platform": "time", "at": "12:00"}],
            on_trigger_event=MagicMock(),
        )
        client.start()
        _wait_until(lambda: "on_message" in captured)
        on_message = captured["on_message"]
        fake_app = captured["app"]
        on_message(fake_app, json.dumps({"type": "auth_required"}))
        on_message(fake_app, json.dumps({"type": "auth_ok"}))
        on_message(fake_app, json.dumps({"id": 1, "type": "result", "success": True, "result": None}))
        assert client.connected is True

        captured["on_close"](fake_app, 1006, "abnormal closure")
        assert client.connected is False

        client.stop()


def test_dispatches_event_trigger_payload_to_callback():
    captured = {}
    on_trigger_event = MagicMock()
    with _patch_ws_app(captured):
        client = HaTriggerClient(
            ws_url="ws://x", token="t", triggers=[{"platform": "state", "entity_id": "sensor.a"}],
            on_trigger_event=on_trigger_event,
        )
        client.start()
        _wait_until(lambda: "on_message" in captured)
        on_message = captured["on_message"]
        fake_app = captured["app"]
        on_message(fake_app, json.dumps({"type": "auth_required"}))
        on_message(fake_app, json.dumps({"type": "auth_ok"}))
        on_message(fake_app, json.dumps({"id": 1, "type": "result", "success": True, "result": None}))

        trigger_payload = {
            "id": "0", "idx": "0", "platform": "state", "entity_id": "sensor.a",
            "from_state": {"state": "19.0"}, "to_state": {"state": "19.5"},
        }
        on_message(fake_app, json.dumps({
            "id": 1, "type": "event", "event": {"variables": {"trigger": trigger_payload}, "context": {}},
        }))

        on_trigger_event.assert_called_once_with(trigger_payload)
        client.stop()


def test_malformed_event_payload_is_ignored_without_crashing():
    captured = {}
    on_trigger_event = MagicMock()
    with _patch_ws_app(captured):
        client = HaTriggerClient(
            ws_url="ws://x", token="t", triggers=[{"platform": "time", "at": "12:00"}],
            on_trigger_event=on_trigger_event,
        )
        client.start()
        _wait_until(lambda: "on_message" in captured)
        on_message = captured["on_message"]
        fake_app = captured["app"]

        on_message(fake_app, "not valid json {{{")  # must not raise
        on_message(fake_app, json.dumps({"type": "event", "event": {}}))  # missing variables.trigger

        on_trigger_event.assert_not_called()
        client.stop()


def test_callback_exception_does_not_crash_the_dispatch_thread():
    captured = {}
    on_trigger_event = MagicMock(side_effect=ValueError("boom"))
    with _patch_ws_app(captured):
        client = HaTriggerClient(
            ws_url="ws://x", token="t", triggers=[{"platform": "time", "at": "12:00"}],
            on_trigger_event=on_trigger_event,
        )
        client.start()
        _wait_until(lambda: "on_message" in captured)
        on_message = captured["on_message"]
        fake_app = captured["app"]

        on_message(fake_app, json.dumps({
            "id": 1, "type": "event",
            "event": {"variables": {"trigger": {"platform": "time", "now": "x"}}, "context": None},
        }))  # must not raise despite the callback raising internally

        on_trigger_event.assert_called_once()
        client.stop()


def test_stop_closes_the_current_websocket_connection():
    """Regression test for the real-HA-Core finding (Task 4, 2026-09-22): stop() must
    not just set an internal flag -- it must actively close the current connection, or
    `connected` can stay True indefinitely against a real, healthy socket that only
    unblocks run_forever() on an actual disconnect. The fake `run_forever()` here
    deliberately does NOT return on its own (unlike the other fakes in this file, which
    return immediately), so this test only passes if stop() itself triggers the close.
    """
    captured = {}
    blocked = threading.Event()

    def _factory(url, on_message=None, on_close=None, on_error=None):
        fake_app = MagicMock()
        fake_app.close.side_effect = lambda: blocked.set()
        fake_app.run_forever.side_effect = lambda: blocked.wait(timeout=2.0)
        captured["app"] = fake_app
        captured["on_message"] = on_message
        return fake_app

    with patch("heizungsbruecke.ha_trigger_client.websocket.WebSocketApp", side_effect=_factory):
        client = HaTriggerClient(
            ws_url="ws://x", token="t", triggers=[{"platform": "time", "at": "12:00"}],
            on_trigger_event=MagicMock(),
        )
        client.start()
        _wait_until(lambda: "app" in captured)

        client.stop()

        captured["app"].close.assert_called_once()
        assert blocked.wait(timeout=2.0)  # proves run_forever() actually unblocked
        client._thread.join(timeout=2.0)  # don't leak this thread into later tests' timing
        assert not client._thread.is_alive()


def test_reconnect_uses_increasing_backoff_and_resubscribes(monkeypatch):
    sleep_calls = []
    monkeypatch.setattr("heizungsbruecke.ha_trigger_client.time.sleep", lambda s: sleep_calls.append(s))
    captured = {}
    with _patch_ws_app(captured):
        client = HaTriggerClient(
            ws_url="ws://x", token="t", triggers=[{"platform": "time", "at": "12:00"}],
            on_trigger_event=MagicMock(),
        )
        client.start()
        _wait_until(lambda: len(sleep_calls) >= 3)
        client.stop()

    assert sleep_calls[:3] == [1, 2, 5]
    assert captured["calls"] >= 4  # initial connect + at least 3 reconnects
