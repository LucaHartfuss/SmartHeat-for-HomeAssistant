import json
import logging
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
import websocket

from heizungsbruecke import ha_trigger_client
from heizungsbruecke.ha_trigger_client import HaTriggerClient

WEBSOCKET_APP = "heizungsbruecke.ha_trigger_client.websocket.WebSocketApp"
# Die echte Klasse fuer spec: waehrend des patch ist websocket.WebSocketApp selbst die Attrappe.
WebSocketApp = websocket.WebSocketApp


def _ignorieren(_trigger) -> None:
    """Rueckruf on_trigger_event fuer Tests, die keine Ereignisse pruefen."""


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


def _stop_and_join(client, timeout=2.0):
    """Stops `client` and waits for its background reconnect thread to actually exit.

    `client.stop()` only sets a flag and closes the *current* (mocked) connection --
    the background thread notices and returns from `_run_forever_with_reconnect` on its
    own schedule, one loop iteration later. Not waiting for that here let a thread
    started by an earlier test in this file outlive that test's `monkeypatch` teardown:
    several tests below monkeypatch `heizungsbruecke.ha_trigger_client.time.sleep`,
    which -- since `time` is one shared module object -- patches `time.sleep` globally,
    not just for that test. A still-running leaked thread's next
    reconnect-loop iteration then resolved `time.sleep` to whatever a *later* test had
    it patched to at that moment and silently appended into that later test's own
    `sleep_calls` list (e.g. a stray 30 landing in
    test_backoff_restarts_at_one_second_after_successful_subscribe's list, contributed
    by test_reconnect_uses_increasing_backoff_and_resubscribes's thread -- CI run
    36439655281, reproduced locally by pinning the test process to one CPU core, which
    reliably starves the background thread of a scheduling slice right around
    `client.stop()`). Joining here -- for every test that starts a client, not only the
    two that patch `time.sleep` -- makes that ordering-dependent leak impossible.
    """
    client.stop()
    thread = client._thread
    if thread is not None:
        thread.join(timeout=timeout)
        assert not thread.is_alive(), "HaTriggerClient background thread leaked past stop()"


def _patch_ws_app(captured):
    """Captures the callbacks HaTriggerClient registers on WebSocketApp and hands back
    a fake app whose run_forever() returns immediately (instead of blocking like the
    real one) -- lets the test drive the connection by calling the captured callbacks
    directly, and lets the background thread's reconnect loop actually run.
    """
    def _factory(url, on_message=None, on_close=None, on_error=None):
        fake_app = MagicMock(spec=WebSocketApp)
        fake_app.run_forever.return_value = None
        captured["calls"] = captured.get("calls", 0) + 1
        captured["url"] = url
        captured["on_message"] = on_message
        captured["on_close"] = on_close
        captured["on_error"] = on_error
        captured["app"] = fake_app
        return fake_app
    return patch(WEBSOCKET_APP, autospec=True, side_effect=_factory)


@pytest.mark.ws
def test_auth_handshake_sends_auth_then_subscribes_with_full_trigger_list():
    captured = {}
    triggers = [
        {"platform": "state", "entity_id": "sensor.room_target"},
        {"platform": "state", "entity_id": "sensor.room_actual"},
        {"platform": "time", "at": "12:00"},
    ]
    with _patch_ws_app(captured):
        client = HaTriggerClient(
            ws_url="ws://x/api/websocket", token="tok123", triggers=triggers, on_trigger_event=_ignorieren,
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

        _stop_and_join(client)


@pytest.mark.ws
def test_connected_becomes_true_only_after_subscribe_result_success():
    captured = {}
    with _patch_ws_app(captured):
        client = HaTriggerClient(
            ws_url="ws://x", token="t", triggers=[{"platform": "time", "at": "12:00"}],
            on_trigger_event=_ignorieren,
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

        _stop_and_join(client)


@pytest.mark.ws
def test_connected_becomes_false_after_on_close():
    captured = {}
    with _patch_ws_app(captured):
        client = HaTriggerClient(
            ws_url="ws://x", token="t", triggers=[{"platform": "time", "at": "12:00"}],
            on_trigger_event=_ignorieren,
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

        _stop_and_join(client)


@pytest.mark.ws
def test_dispatches_event_trigger_payload_to_callback():
    captured = {}
    ereignisse = []
    with _patch_ws_app(captured):
        client = HaTriggerClient(
            ws_url="ws://x", token="t", triggers=[{"platform": "state", "entity_id": "sensor.a"}],
            on_trigger_event=ereignisse.append,
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

        assert ereignisse == [trigger_payload]
        _stop_and_join(client)


@pytest.mark.ws
def test_malformed_event_payload_is_ignored_without_crashing():
    captured = {}
    ereignisse = []
    with _patch_ws_app(captured):
        client = HaTriggerClient(
            ws_url="ws://x", token="t", triggers=[{"platform": "time", "at": "12:00"}],
            on_trigger_event=ereignisse.append,
        )
        client.start()
        _wait_until(lambda: "on_message" in captured)
        on_message = captured["on_message"]
        fake_app = captured["app"]

        on_message(fake_app, "not valid json {{{")  # must not raise
        on_message(fake_app, json.dumps({"type": "event", "event": {}}))  # missing variables.trigger

        assert ereignisse == []
        _stop_and_join(client)


@pytest.mark.ws
def test_callback_exception_does_not_crash_the_dispatch_thread():
    captured = {}
    ereignisse = []

    def on_trigger_event(trigger):
        ereignisse.append(trigger)
        raise ValueError("boom")

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

        assert ereignisse == [{"platform": "time", "now": "x"}]
        _stop_and_join(client)


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
        fake_app = MagicMock(spec=WebSocketApp)
        fake_app.close.side_effect = lambda: blocked.set()
        fake_app.run_forever.side_effect = lambda **kwargs: blocked.wait(timeout=2.0)
        captured["app"] = fake_app
        captured["on_message"] = on_message
        return fake_app

    with patch(WEBSOCKET_APP, autospec=True, side_effect=_factory):
        client = HaTriggerClient(
            ws_url="ws://x", token="t", triggers=[{"platform": "time", "at": "12:00"}],
            on_trigger_event=_ignorieren,
        )
        client.start()
        _wait_until(lambda: "app" in captured)

        client.stop()

        captured["app"].close.assert_called_once()
        assert blocked.wait(timeout=2.0)  # proves run_forever() actually unblocked
        client._thread.join(timeout=2.0)  # don't leak this thread into later tests' timing
        assert not client._thread.is_alive()


@pytest.mark.ws
def test_on_connected_callback_fires_when_subscribe_result_succeeds():
    """Re-Review final-review-report.md: der einzig verlaessliche Hook-Punkt fuer einen
    Reconnect/Erstverbindung ist `_handle_subscribe_result`, nicht ein von aussen per
    Watchdog-Takt abgefragtes `connected` -- das haette bei einem kurzen Ausfall
    zwischen zwei Ticks nie einen Uebergang gesehen. Dieser Test prueft direkt an der
    Quelle: `on_connected` feuert exakt dann, wenn `subscribe_trigger` erfolgreich war
    (also `connected` von False auf True wechselt) -- nicht davor.
    """
    captured = {}
    verbunden = []
    with _patch_ws_app(captured):
        client = HaTriggerClient(
            ws_url="ws://x", token="t", triggers=[{"platform": "time", "at": "12:00"}],
            on_trigger_event=_ignorieren, on_connected=lambda: verbunden.append(client.connected),
        )
        client.start()
        _wait_until(lambda: "on_message" in captured)
        on_message = captured["on_message"]
        fake_app = captured["app"]

        on_message(fake_app, json.dumps({"type": "auth_required"}))
        on_message(fake_app, json.dumps({"type": "auth_ok"}))
        assert verbunden == []  # subscribe result not received yet

        on_message(fake_app, json.dumps({"id": 1, "type": "result", "success": True, "result": None}))
        assert client.connected is True
        assert verbunden == [True]  # einmal, und erst nach connected

        _stop_and_join(client)


def test_on_connected_callback_fires_again_on_a_second_successful_subscribe():
    """Direct unit test of the hook point itself (`_handle_subscribe_result`), bypassing
    both the WS message-parsing plumbing and the real background reconnect thread (the
    fake `run_forever` in `_patch_ws_app` returns immediately, so driving a *second*
    connection deterministically through the full on_message/on_close plumbing would
    race the background thread's own reconnect loop). The design's single correct hook
    point is "runs every time `subscribe_trigger` succeeds -- both on the very first
    connect after `start()` and on every reconnect after a disconnect" -- this test
    calls that hook point twice, exactly as `_run_forever_with_reconnect` would after a
    real disconnect (it resets `_subscribed = False` before each new attempt), and
    asserts `on_connected` fires once per success, matching `_connected`'s own
    set/clear/set cycle.
    """
    verbunden = []
    client = HaTriggerClient(
        ws_url="ws://x", token="t", triggers=[{"platform": "time", "at": "12:00"}],
        on_trigger_event=_ignorieren, on_connected=lambda: verbunden.append(True),
    )
    fake_ws = MagicMock(spec=WebSocketApp)

    client._handle_subscribe_result(fake_ws, {"success": True})
    assert client.connected is True
    assert len(verbunden) == 1

    # Simulate a disconnect followed by a reconnect, mirroring what
    # _run_forever_with_reconnect does around each new connection attempt.
    client._connected.clear()
    client._subscribed = False
    client._handle_subscribe_result(fake_ws, {"success": True})
    assert client.connected is True
    assert len(verbunden) == 2


@pytest.mark.ws
def test_on_connected_callback_not_called_when_subscribe_fails():
    captured = {}
    verbunden = []
    with _patch_ws_app(captured):
        client = HaTriggerClient(
            ws_url="ws://x", token="t", triggers=[{"platform": "time", "at": "12:00"}],
            on_trigger_event=_ignorieren, on_connected=lambda: verbunden.append(True),
        )
        client.start()
        _wait_until(lambda: "on_message" in captured)
        on_message = captured["on_message"]
        fake_app = captured["app"]

        on_message(fake_app, json.dumps({"type": "auth_required"}))
        on_message(fake_app, json.dumps({"type": "auth_ok"}))
        on_message(fake_app, json.dumps({"id": 1, "type": "result", "success": False, "error": "boom"}))

        assert client.connected is False
        assert verbunden == []

        _stop_and_join(client)


@pytest.mark.ws
def test_on_connected_callback_exception_does_not_crash_the_ws_thread():
    """Mirrors test_callback_exception_does_not_crash_the_dispatch_thread above --
    `on_connected` is invoked from the same background WS-callback thread as
    `on_trigger_event`, and the class docstring's "must never raise out of its
    background thread" guarantee applies to it just the same.
    """
    captured = {}
    verbunden, ereignisse = [], []

    def on_connected():
        verbunden.append(True)
        raise ValueError("boom")

    with _patch_ws_app(captured):
        client = HaTriggerClient(
            ws_url="ws://x", token="t", triggers=[{"platform": "time", "at": "12:00"}],
            on_trigger_event=ereignisse.append, on_connected=on_connected,
        )
        client.start()
        _wait_until(lambda: "on_message" in captured)
        on_message = captured["on_message"]
        fake_app = captured["app"]

        on_message(fake_app, json.dumps({"type": "auth_required"}))
        on_message(fake_app, json.dumps({"type": "auth_ok"}))
        on_message(fake_app, json.dumps({"id": 1, "type": "result", "success": True, "result": None}))  # must not raise

        assert verbunden == [True]
        assert client.connected is True  # _connected.set() ran before the callback raised

        # Thread survived the exception and can still dispatch a subsequent event.
        trigger_payload = {"platform": "time", "now": "x"}
        on_message(fake_app, json.dumps({
            "id": 1, "type": "event", "event": {"variables": {"trigger": trigger_payload}, "context": {}},
        }))
        assert ereignisse == [trigger_payload]

        _stop_and_join(client)


def test_reconnect_uses_increasing_backoff_and_resubscribes(monkeypatch):
    sleep_calls = []
    monkeypatch.setattr("heizungsbruecke.ha_trigger_client.time.sleep", lambda s: sleep_calls.append(s))
    captured = {}
    with _patch_ws_app(captured):
        client = HaTriggerClient(
            ws_url="ws://x", token="t", triggers=[{"platform": "time", "at": "12:00"}],
            on_trigger_event=_ignorieren,
        )
        client.start()
        _wait_until(lambda: len(sleep_calls) >= 3)
        _stop_and_join(client)

    assert sleep_calls[:3] == [1, 2, 5]
    assert captured["calls"] >= 4  # initial connect + at least 3 reconnects


def test_backoff_restarts_at_one_second_after_successful_subscribe(monkeypatch):
    # B7: nach einer erfolgreichen Verbindung darf der Backoff nicht auf 30 s stehen bleiben.
    sleep_calls = []
    monkeypatch.setattr("heizungsbruecke.ha_trigger_client.time.sleep", lambda s: sleep_calls.append(s))

    def _factory(url, on_message=None, on_close=None, on_error=None):
        fake_app = MagicMock(spec=WebSocketApp)

        def _connect_subscribe_and_drop(**kwargs):
            on_message(fake_app, json.dumps({"type": "auth_required"}))
            on_message(fake_app, json.dumps({"type": "auth_ok"}))
            on_message(fake_app, json.dumps({"id": 1, "type": "result", "success": True, "result": None}))

        fake_app.run_forever.side_effect = _connect_subscribe_and_drop
        return fake_app

    with patch(WEBSOCKET_APP, autospec=True, side_effect=_factory):
        client = HaTriggerClient(
            ws_url="ws://x", token="t", triggers=[{"platform": "time", "at": "12:00"}],
            on_trigger_event=_ignorieren,
        )
        client.start()
        _wait_until(lambda: len(sleep_calls) >= 3)
        _stop_and_join(client)

    assert sleep_calls[:3] == [1, 1, 1]


def test_successful_subscribe_resets_attempt_counter():
    client = HaTriggerClient(ws_url="ws://x", token="t", triggers=[], on_trigger_event=_ignorieren)
    client._attempt = 4

    client._handle_subscribe_result(MagicMock(spec=WebSocketApp), {"success": True})

    assert client._attempt == 0


@pytest.mark.ws
def test_connection_is_kept_alive_with_ws_pings():
    captured = {}
    with _patch_ws_app(captured):
        client = HaTriggerClient(
            ws_url="ws://x", token="t", triggers=[{"platform": "time", "at": "12:00"}],
            on_trigger_event=_ignorieren,
        )
        client.start()
        _wait_until(lambda: "app" in captured and captured["app"].run_forever.called)

        captured["app"].run_forever.assert_called_with(ping_interval=30, ping_timeout=10)

        _stop_and_join(client)


# --- Halb offene Verbindung und falsches Token: Wiederverbinden auf der Fake-Uhr ---

class _Uhr:
    """Ersetzt das Modul `time` in ha_trigger_client: `sleep` rueckt die Zeit vor, statt zu warten. `grenze`: ab dieser
    Zeit beendet der naechste Schlaf den Client (Ende des Beobachtungsfensters)."""

    def __init__(self, grenze: float):
        self.jetzt = 0.0
        self.schlaefe: list[float] = []
        self.grenze = grenze
        self.client: HaTriggerClient | None = None

    def sleep(self, sekunden: float) -> None:
        self.schlaefe.append(sekunden)
        self.jetzt += sekunden
        if self.jetzt >= self.grenze and self.client is not None:
            self.client.stop()


class _HaGegenstelle:
    """Fake `WebSocketApp`: `run_forever` spielt eine Verbindung zu HA so durch, wie websocket-client sie meldet
    (Rueckrufe mit der App als erstem Argument, `on_close` beim Abbau) und kehrt zurueck, wenn sie endet.

    `token`: das gueltige Token (ein anderes beantwortet HA mit `auth_invalid`). `halb_offen`: Verbindungsnummern
    (ab 1), deren Gegenstelle nach dem Abonnement stumm bleibt (kein Pong). Die Bibliothek bemerkt das nur mit
    gesetzten Ping-Argumenten: nach `ping_interval` + `ping_timeout` meldet sie `on_error` mit
    `WebSocketTimeoutException` und `on_close`; ohne Ping haengt `run_forever` bis zum `close`."""

    def __init__(self, uhr: _Uhr, *, token: str, halb_offen=()):
        self.uhr = uhr
        self.token = token
        self.halb_offen = set(halb_offen)
        self.beginn: list[float] = []  # Zeit je Verbindungsaufbau
        self.geschlossen: list[int] = []  # Nummer der Verbindung je close des Clients

    def __call__(self, url, on_message=None, on_close=None, on_error=None):
        nummer = len(self.beginn) + 1
        self.beginn.append(self.uhr.jetzt)
        app = MagicMock(spec=WebSocketApp)
        zu = threading.Event()

        def close():
            self.geschlossen.append(nummer)
            zu.set()

        def senden(text):
            nachricht = json.loads(text)
            if nachricht["type"] == "auth":
                angemeldet = nachricht["access_token"] == self.token
                antwort = {"type": "auth_ok"} if angemeldet else {"type": "auth_invalid", "message": "Invalid access token"}
                on_message(app, json.dumps(antwort))
            elif nachricht["type"] == "subscribe_trigger":
                on_message(app, json.dumps({"id": nachricht["id"], "type": "result", "success": True, "result": None}))

        def run_forever(ping_interval=0, ping_timeout=None, **_rest):
            on_message(app, json.dumps({"type": "auth_required"}))
            if not zu.is_set() and nummer in self.halb_offen and ping_interval and ping_timeout:
                self.uhr.jetzt += ping_interval + ping_timeout
                on_error(app, websocket.WebSocketTimeoutException("ping/pong timed out"))
            else:
                zu.wait(timeout=5)  # gesunde Verbindung bzw. ohne Ping: bis zum close
            on_close(app, None, None)

        app.close.side_effect = close
        app.send.side_effect = senden
        app.run_forever.side_effect = run_forever
        return app


def test_a_half_open_connection_is_noticed_without_pong_and_the_client_reconnects(monkeypatch):
    uhr = _Uhr(grenze=float("inf"))
    monkeypatch.setattr(ha_trigger_client, "time", uhr)
    ha = _HaGegenstelle(uhr, token="tok", halb_offen={1})
    verbunden = []
    with patch(WEBSOCKET_APP, autospec=True, side_effect=ha):
        client = HaTriggerClient(ws_url="ws://x", token="tok", triggers=[{"platform": "time", "at": "12:00"}],
                                 on_trigger_event=_ignorieren, on_connected=lambda: verbunden.append(uhr.jetzt))
        client.start()
        _wait_until(lambda: len(verbunden) == 2)
        assert client.connected is True
        _stop_and_join(client)

    # Ping alle 30 s, Zeitlimit 10 s: die stumme Verbindung faellt nach 40 s, 1 s spaeter steht die neue.
    assert verbunden == [0, 41]
    assert ha.beginn == [0, 41]
    assert uhr.schlaefe == [1]


def test_a_wrong_token_closes_logs_and_backs_off_without_rapid_reconnects(monkeypatch, caplog):
    uhr = _Uhr(grenze=70)
    monkeypatch.setattr(ha_trigger_client, "time", uhr)
    ha = _HaGegenstelle(uhr, token="richtig")
    verbunden = []
    with patch(WEBSOCKET_APP, autospec=True, side_effect=ha), caplog.at_level(logging.DEBUG):
        client = HaTriggerClient(ws_url="ws://x", token="falsch-geheim", triggers=[{"platform": "time", "at": "12:00"}],
                                 on_trigger_event=_ignorieren, on_connected=lambda: verbunden.append(uhr.jetzt))
        uhr.client = client
        client.start()
        client._thread.join(timeout=5)
        assert not client._thread.is_alive()

    # In 70 s genau ein Versuch je Backoff-Stufe (1, 2, 5, 10, 20, 30 s), kein Dauerfeuer.
    assert ha.beginn == [0, 1, 3, 8, 18, 38, 68]
    assert uhr.schlaefe == [1, 2, 5, 10, 20, 30, 30]
    # Jede abgewiesene Verbindung schliesst der Client selbst; das letzte close kommt von stop() am Fensterende.
    assert ha.geschlossen == [1, 2, 3, 4, 5, 6, 7, 7]
    fehler = [r.getMessage() for r in caplog.records if "WS-Authentifizierung fehlgeschlagen" in r.getMessage()]
    assert len(fehler) == 7
    assert "falsch-geheim" not in caplog.text  # auf keiner Stufe (DEBUG bis ERROR)
    assert verbunden == []  # nie verbunden
