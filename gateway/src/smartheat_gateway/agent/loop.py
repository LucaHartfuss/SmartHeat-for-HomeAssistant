"""Agent-Schleife (Spec SHG G2 6.3): registrieren (Backoff bis zur Annahme), Befehle im Takt poll_after holen und
ausfuehren, wartende Befehle jede Runde pruefen, Status bei Aenderung und alle 300 s, Meldungen bei Aenderung,
Aufraeumen nach dem Abmelden, Agent-Zustand schreiben. Erledigte command_ids in /data/agent/done.json (letzte 500):
ein erneut gelieferter Befehl wird nicht noch einmal ausgefuehrt, sein Ergebnis aber erneut gemeldet, falls das
Melden scheiterte (solange der Agent laeuft). Ein kaputtes done.json gilt als leer (Review Focus 5).

Jeder gemeldete `grund` steht in der festen Liste des Befehls (wire.command_errors): Handler-Gruende prueft
commands.execute, den Zeitablauf wartender Befehle (keine_bestaetigung) prueft timed_out."""
import logging
import time
from datetime import UTC, datetime

from smartheat_gateway import topics
from smartheat_gateway.agent import commands, lifecycle, wire
from smartheat_gateway.agent.api_client import ApiUnavailable, NotAuthenticated
from smartheat_gateway.agent.context import AgentContext
from smartheat_gateway.agent.state import AgentStateWriter, derive
from smartheat_gateway.bus import decode
from smartheat_gateway.drivers.registry import driver_ids
from smartheat_gateway.files import read_json, write_json
from smartheat_gateway.version import GATEWAY_VERSION
from smartheat_runtime.notifier import category

logger = logging.getLogger(__name__)

STATUS_EVERY_SECONDS = 300
MAX_BACKOFF_SECONDS = 300
DONE_KEEP = 500
TIMEOUT_GRUND = "keine_bestaetigung"
TIMEOUT_TEXT = "Das Gateway hat die Änderung nicht rechtzeitig bestätigt."
INTERN_TEXT = "Interner Fehler im Gateway."


def _iso(wall: float) -> str:
    return datetime.fromtimestamp(wall, UTC).isoformat()


def timed_out(kind: str) -> commands.Failed:
    """Ergebnis eines wartenden Befehls nach Ablauf seiner Frist; nie ein Grund ausserhalb des Vertrags."""
    if kind in wire.COMMANDS and TIMEOUT_GRUND in wire.command_errors(kind):
        return commands.Failed(TIMEOUT_GRUND, TIMEOUT_TEXT)
    logger.error("Befehl %s: Zeitablauf steht nicht in der Vertragsliste", kind)
    return commands.Failed("intern", INTERN_TEXT)


class AgentLoop:
    def __init__(self, ctx: AgentContext, api, *, sleep=time.sleep) -> None:
        self.ctx, self.api, self._sleep = ctx, api, sleep
        self._registered = False
        self._next_try = 0.0
        self._failures = 0
        self._next_poll = 0.0
        self._last_ok: float | None = None
        self._server_seen = False
        self._pending: dict[str, tuple[str, commands.Waiting]] = {}
        self._unsent: dict[str, tuple[bool, dict | None, dict | None]] = {}
        done = read_json(ctx.paths.agent_dir / "done.json")
        self._done: list[str] = [item for item in done if isinstance(item, str)] if isinstance(done, list) else []
        self._last_status_body: dict | None = None
        self._last_status_at: float | None = None
        self._open: dict[str, dict] = {}
        self._changed_notes: dict[str, dict] = {}
        self._state = AgentStateWriter(ctx.paths.agent_state, lambda: _iso(ctx.wall()), ctx.clock)
        ctx.bus.subscribe(topics.NOTIFY_PREFIX + "+", self._on_notify)
        ctx.bus.subscribe(topics.NOTIFY_PUSH, self._on_push)

    @property
    def server_ok(self) -> bool:
        return (
            self._last_ok is not None and self._failures == 0
            and self.ctx.clock() - self._last_ok < 2 * MAX_BACKOFF_SECONDS
        )

    def capabilities(self) -> dict:
        return {"drivers": list(driver_ids()), "zigbee": True}

    # --- Bus-Thread: nur ablegen ---

    def _on_notify(self, topic, raw, retain) -> None:
        key = topic.removeprefix(topics.NOTIFY_PREFIX)
        body = decode(raw)
        if isinstance(body, dict):
            item = {"key": key, "kategorie": body.get("kategorie", category(key)), "kritisch": True,
                    "text": body.get("text", ""), "offen": True, "ts": body.get("ts")}
            self._open[key] = item
        else:
            previous = self._open.pop(key, None)
            item = {"key": key, "kategorie": category(key), "kritisch": True,
                    "text": previous["text"] if previous else "", "offen": False, "ts": _iso(self.ctx.wall())}
        self._changed_notes[key] = item

    def _on_push(self, topic, raw, retain) -> None:
        body = decode(raw)
        if not isinstance(body, dict) or "key" not in body:
            return
        key = body["key"]
        self._changed_notes[key] = {"key": key, "kategorie": category(key), "kritisch": key in self._open,
                                    "text": body.get("text", ""), "offen": key in self._open, "ts": body.get("ts")}

    # --- Schleife ---

    def run(self) -> None:  # pragma: no cover - Endlosschleife
        while True:
            try:
                self.run_once()
            except Exception:
                logger.exception("Fehler in der Agent-Schleife, naechste Runde in 1 s")
            self._sleep(1)

    def run_once(self) -> None:
        now = self.ctx.clock()
        if now >= self._next_try:
            try:
                self._talk(now)
                self._contact_ok()
            except NotAuthenticated:
                logger.warning("Geraete-API: nicht authentifiziert (Uhrzeit? gesperrt?), registriere neu")
                self._registered = False
                self._contact_failed(now)
            except ApiUnavailable as error:
                logger.warning("Geraete-API nicht erreichbar: %s", error)
                self._contact_failed(now)
        self._check_pending()
        try:
            lifecycle.cleanup_after_sign_off(self.ctx)
        except Exception:
            logger.exception("Aufraeumen nach dem Abmelden gescheitert, naechster Versuch in der naechsten Runde")
        down = None if self._last_ok is None else (now - self._last_ok if self._failures else None)
        self._state.update(derive(
            server_seen=self._server_seen, server_down_seconds=down, device_state=self.ctx.device_state,
            runtime_status=self.ctx.runtime_status,
        ))

    def _talk(self, now: float) -> None:
        if not self._registered or self.ctx.register_requested:
            self.api.identity = self.ctx.identity
            answer = self.api.register(GATEWAY_VERSION, self.capabilities())
            self._registered, self.ctx.register_requested = True, False
            self.ctx.device_state = answer.get("device_state")
            self._next_poll = now
        self._send_unsent()
        if now >= self._next_poll:
            answer = self.api.commands()
            self.ctx.device_state = answer.get("device_state")
            self._next_poll = now + float(answer.get("poll_after") or 60)
            for command in answer.get("commands", []):
                self._dispatch(command)
        self._upload_status(now)
        self._upload_notifications()

    def _contact_ok(self) -> None:
        self._last_ok, self._server_seen, self._failures = self.ctx.clock(), True, 0

    def _contact_failed(self, now: float) -> None:
        self._failures += 1
        self._next_try = now + min(MAX_BACKOFF_SECONDS, 2 ** min(self._failures, 9))

    def _dispatch(self, command: dict) -> None:
        command_id = command.get("command_id")
        if not isinstance(command_id, str) or command_id in self._pending or command_id in self._done:
            return
        kind = command.get("kind", "")
        outcome = commands.execute(self.ctx, kind, command.get("payload"))
        self._finish_or_wait(command_id, kind, outcome)

    def _finish_or_wait(self, command_id: str, kind: str, outcome: commands.Outcome) -> None:
        if isinstance(outcome, commands.Waiting):
            self._pending[command_id] = (kind, outcome)
            return
        if isinstance(outcome, commands.Done):
            self._unsent[command_id] = (True, outcome.result, None)
        else:
            self._unsent[command_id] = (False, None, {"grund": outcome.grund, "text": outcome.text})
        self._mark_done(command_id)
        self._send_unsent()

    def _check_pending(self) -> None:
        for command_id, (kind, waiting) in list(self._pending.items()):
            outcome = waiting.check()
            if outcome is None and self.ctx.clock() > waiting.deadline:
                outcome = timed_out(kind)
            if outcome is not None:
                del self._pending[command_id]
                self._finish_or_wait(command_id, kind, outcome)

    def _mark_done(self, command_id: str) -> None:
        self._done = (self._done + [command_id])[-DONE_KEEP:]
        write_json(self.ctx.paths.agent_dir / "done.json", self._done)

    def _send_unsent(self) -> None:
        for command_id, (ok, result, error) in list(self._unsent.items()):
            try:
                self.api.result(command_id, ok, result, error)
            except ApiUnavailable:
                return
            del self._unsent[command_id]

    def _upload_status(self, now: float) -> None:
        body = {
            "agent_state": derive(server_seen=True, server_down_seconds=None, device_state=self.ctx.device_state,
                                  runtime_status=self.ctx.runtime_status),
            "runtime_status": self.ctx.runtime_status, "raum": self.ctx.raum, "version": GATEWAY_VERSION,
            "capabilities": self.capabilities(),
        }
        due = self._last_status_at is None or now - self._last_status_at >= STATUS_EVERY_SECONDS
        if body == self._last_status_body and not due:
            return
        self.api.status({**body, "ts": _iso(self.ctx.wall())})
        self._last_status_body, self._last_status_at = body, now

    def _upload_notifications(self) -> None:
        if not self._changed_notes:
            return
        items = list(self._changed_notes.values())
        self.api.notifications(items)
        for item in items:
            if self._changed_notes.get(item["key"]) is item:
                del self._changed_notes[item["key"]]
