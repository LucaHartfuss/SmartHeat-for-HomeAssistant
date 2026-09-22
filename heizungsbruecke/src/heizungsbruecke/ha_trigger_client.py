import json
import logging
import threading
import time
from typing import Callable

import websocket

logger = logging.getLogger(__name__)

_RECONNECT_DELAYS_SECONDS = (1, 2, 5, 10, 20, 30)


class HaTriggerClient:
    """Persistent WebSocket connection to HA Core's `subscribe_trigger` command -- the
    same internal mechanism HA-YAML automations and the frontend automation editor use
    (see docs/superpowers/specs/2026-09-21-heizungsbruecke-eventgetriebene-trigger-design.md).
    This is NOT a documented/stable public API: if `subscribe_trigger` ever fails or
    changes shape on a future HA Core version, `connected` simply stays False forever
    and the caller's watchdog-loop fallback (see __main__.py) covers the gap -- this
    class must never raise out of its background thread or otherwise take down the
    add-on process.
    """

    def __init__(
        self, ws_url: str, token: str, triggers: list[dict],
        on_trigger_event: Callable[[dict], None],
    ):
        self._ws_url = ws_url
        self._token = token
        self._triggers = triggers
        self._on_trigger_event = on_trigger_event
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._authed = False
        self._subscribed = False
        self._ws_app: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_forever_with_reconnect, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Sets the stop flag AND proactively closes the current connection.

        Real-HA-Core integration test finding (Task 4, 2026-09-22): setting only the
        stop flag left `connected` True for an unbounded time after `stop()` returned,
        because the background thread only re-checks the flag once `ws_app.run_forever()`
        itself returns -- and nothing was telling that blocking call to return. Against a
        real, healthy HA Core connection that can take arbitrarily long (it only returns
        on an actual disconnect). The fake-`WebSocketApp` unit tests (Task 3) never
        caught this since their fake `run_forever()` returns immediately by construction.
        """
        self._stop.set()
        ws_app = self._ws_app
        if ws_app is not None:
            try:
                ws_app.close()
            except Exception:
                logger.exception("HaTriggerClient: Fehler beim Schliessen der WS-Verbindung in stop()")

    def _run_forever_with_reconnect(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            self._authed = False
            self._subscribed = False
            try:
                ws_app = websocket.WebSocketApp(
                    self._ws_url,
                    on_message=self._on_message,
                    on_close=self._on_close,
                    on_error=self._on_error,
                )
                self._ws_app = ws_app
                ws_app.run_forever()
            except Exception:
                logger.exception("HaTriggerClient: unerwarteter Fehler in run_forever")
            self._connected.clear()
            if self._stop.is_set():
                return
            delay = _RECONNECT_DELAYS_SECONDS[min(attempt, len(_RECONNECT_DELAYS_SECONDS) - 1)]
            logger.warning("HaTriggerClient: Verbindung getrennt/fehlgeschlagen, Reconnect in %ss", delay)
            attempt += 1
            time.sleep(delay)

    def _on_message(self, ws, message: str) -> None:
        try:
            payload = json.loads(message)
        except (TypeError, ValueError):
            logger.warning("HaTriggerClient: unparsebare WS-Nachricht ignoriert")
            return

        msg_type = payload.get("type")
        if msg_type == "auth_required":
            ws.send(json.dumps({"type": "auth", "access_token": self._token}))
        elif msg_type == "auth_invalid":
            logger.error("HaTriggerClient: WS-Authentifizierung fehlgeschlagen: %s", payload.get("message"))
            ws.close()
        elif msg_type == "auth_ok":
            self._authed = True
            ws.send(json.dumps({"id": 1, "type": "subscribe_trigger", "trigger": self._triggers}))
        elif msg_type == "result" and self._authed and not self._subscribed:
            self._handle_subscribe_result(ws, payload)
        elif msg_type == "event":
            self._dispatch_event(payload)

    def _handle_subscribe_result(self, ws, payload: dict) -> None:
        if payload.get("success"):
            self._subscribed = True
            self._connected.set()
            logger.info("HaTriggerClient: subscribe_trigger erfolgreich fuer %d Trigger", len(self._triggers))
        else:
            logger.error("HaTriggerClient: subscribe_trigger fehlgeschlagen: %s", payload.get("error"))
            ws.close()

    def _dispatch_event(self, payload: dict) -> None:
        try:
            trigger = payload["event"]["variables"]["trigger"]
        except (KeyError, TypeError):
            logger.warning("HaTriggerClient: Trigger-Event mit unerwarteter Struktur ignoriert: %r", payload)
            return
        try:
            self._on_trigger_event(trigger)
        except Exception:
            logger.exception("HaTriggerClient: on_trigger_event-Callback hat eine Ausnahme geworfen")

    def _on_close(self, ws, close_status_code, close_msg) -> None:
        logger.warning("HaTriggerClient: WS-Verbindung geschlossen (code=%s, msg=%s)", close_status_code, close_msg)
        self._connected.clear()

    def _on_error(self, ws, error) -> None:
        logger.warning("HaTriggerClient: WS-Fehler: %s", error)
