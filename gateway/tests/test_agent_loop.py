import inspect
import json
import logging
import time

import pytest
from configs import apply_config, write_runtime_files
from fake_z2m import FakeZigbee2Mqtt
from fakes import FakeBus

from smartheat_gateway import topics
from smartheat_gateway.agent import commands, identity, wire
from smartheat_gateway.agent import loop as loop_module
from smartheat_gateway.agent.api_client import DeviceApiClient, NotAuthenticated, Rejected
from smartheat_gateway.agent.context import AgentContext
from smartheat_gateway.agent.loop import AgentLoop
from smartheat_gateway.config import load_raw
from smartheat_gateway.paths import Paths
from smartheat_gateway.zigbee import ZigbeeMirror

POLL = 1  # FakeDeviceApi.poll_after


@pytest.fixture
def agent(api, data_dir, clock):
    bus = FakeBus()
    z2m = FakeZigbee2Mqtt(bus)
    z2m.bridge(online=True)
    mirror = ZigbeeMirror(bus, clock)
    mirror.start()
    paths = Paths(data_dir)
    ident = identity.load_or_create(paths)
    ctx = AgentContext(paths, bus, mirror, ident, clock=clock)
    ctx.start()
    ctx.z2m_requests = lambda: z2m.permit_join_requests  # nur Test
    return AgentLoop(ctx, DeviceApiClient(api.url, ident), sleep=lambda seconds: None)


def _done(agent) -> list:
    """command_ids in done.json (Eintraege [command_id, ok, result, error])."""
    entries = json.loads((agent.ctx.paths.agent_dir / "done.json").read_text())
    return [entry if isinstance(entry, str) else entry[0] for entry in entries]


def _restart(agent) -> AgentLoop:
    """Neuer Agent-Prozess auf demselben /data und Bus (nur der Speicher der Schleife ist weg)."""
    return AgentLoop(agent.ctx, agent.api, sleep=lambda seconds: None)


def _reloads(agent) -> int:
    return sum(1 for topic, _ in agent.ctx.bus.decoded() if topic == topics.CMD_RELOAD)


def test_registers_and_reports_unclaimed(agent, api):
    agent.run_once()
    assert api.registrations == 1
    assert json.loads(agent.ctx.paths.agent_state.read_text())["zustand"] == "nicht_uebernommen"


def test_executes_commands_once_and_reports_results(agent, api, clock):
    agent.run_once()
    command_id = api.enqueue("zigbee_permit_join", {"seconds": 30})
    clock.advance(POLL)
    agent.run_once()
    assert api.result_of(command_id) == {"ok": True, "result": {}, "error": None}
    assert agent.ctx.z2m_requests() == [30]
    api.commands[command_id]["result"] = None  # Server liefert erneut aus
    clock.advance(POLL)
    agent.run_once()
    assert command_id in _done(agent)
    assert agent.ctx.z2m_requests() == [30]   # kein zweites Ausfuehren


def test_done_commands_survive_an_agent_restart(agent, api, clock):
    agent.run_once()
    command_id = api.enqueue("zigbee_permit_join", {"seconds": 30})
    clock.advance(POLL)
    agent.run_once()
    api.commands[command_id]["result"] = None  # Server liefert nach dem Neustart erneut aus
    _restart(agent).run_once()
    assert agent.ctx.z2m_requests() == [30]


def test_unsent_result_survives_a_restart_and_is_not_executed_twice(agent, api, clock):
    agent.run_once()
    command_id = api.enqueue("zigbee_permit_join", {"seconds": 30})
    api.result_status = 503  # Ergebnis kommt nicht an
    clock.advance(POLL)
    agent.run_once()
    assert api.result_of(command_id) is None and agent.ctx.z2m_requests() == [30]
    restarted = _restart(agent)  # Neustart, bevor das Ergebnis hochging
    api.result_status = None
    restarted.run_once()  # Server liefert erneut aus
    assert api.result_of(command_id) == {"ok": True, "result": {}, "error": None}
    assert agent.ctx.z2m_requests() == [30]


def test_unsent_apply_config_result_arrives_after_a_restart(agent, api, clock):
    agent.run_once()
    api.claim()
    command_id = api.enqueue("apply_config", {"setup_id": "setup-1", "config": apply_config()})
    clock.advance(POLL)
    agent.run_once()
    api.result_status = 503
    agent.ctx.bus.publish(topics.STATUS, {"schema": 2, "status": "regelt", "setup_id": "setup-1"}, retain=True)
    agent.run_once()  # bestaetigt, Melden scheitert
    assert api.result_of(command_id) is None and _reloads(agent) == 1
    restarted = _restart(agent)
    api.result_status = None
    restarted.run_once()
    assert api.result_of(command_id) == {"ok": True, "result": {"setup_id": "setup-1"}, "error": None}
    assert _reloads(agent) == 1  # nicht erneut ausgefuehrt


def test_old_done_format_counts_as_done_without_result(agent, api, clock):
    agent.run_once()
    command_id = api.enqueue("zigbee_permit_join", {"seconds": 30})
    agent.ctx.paths.agent_dir.mkdir(parents=True, exist_ok=True)
    (agent.ctx.paths.agent_dir / "done.json").write_text(json.dumps([command_id, 7, ["kaputt"]]))
    restarted = _restart(agent)
    clock.advance(POLL)
    restarted.run_once()
    assert agent.ctx.z2m_requests() == [] and api.result_of(command_id) is None
    other = api.enqueue("zigbee_permit_join", {"seconds": 5})
    clock.advance(POLL)
    restarted.run_once()
    assert json.loads((agent.ctx.paths.agent_dir / "done.json").read_text()) == [command_id, [other, True, {}, None]]


def test_rejected_result_is_dropped_and_does_not_block_later_results(agent, api, clock, monkeypatch, caplog):
    agent.run_once()
    first = api.enqueue("zigbee_permit_join", {"seconds": 30})
    second = api.enqueue("zigbee_permit_join", {"seconds": 40})
    real_result = agent.api.result

    def result(command_id, *args):
        if command_id == first:
            raise Rejected("result: HTTP 422")
        return real_result(command_id, *args)

    monkeypatch.setattr(agent.api, "result", result)
    clock.advance(POLL)
    with caplog.at_level(logging.ERROR):
        agent.run_once()
    assert api.result_of(second)["ok"] is True and api.result_of(first) is None
    assert "abgelehnt" in caplog.text
    clock.advance(POLL)
    agent.run_once()  # erneut geliefert: gespeichertes Ergebnis geht noch einmal hoch, kein zweites Ausfuehren
    assert agent.ctx.z2m_requests() == [30, 40]


def test_unauthenticated_result_while_checking_waiting_commands_does_not_escape(agent, api, clock, monkeypatch):
    agent.run_once()
    api.claim()
    command_id = api.enqueue("apply_config", {"setup_id": "setup-1", "config": apply_config()})
    clock.advance(POLL)
    agent.run_once()
    real_result = agent.api.result
    calls = []

    def unauthenticated(*args):
        calls.append(args)
        raise NotAuthenticated("result")

    monkeypatch.setattr(agent.api, "result", unauthenticated)
    agent.ctx.bus.publish(topics.STATUS, {"schema": 2, "status": "regelt", "setup_id": "setup-1"}, retain=True)
    agent.run_once()  # _check_pending -> Done -> 401 beim Melden: Runde laeuft bis zum Agent-Zustand durch
    assert calls
    assert json.loads(agent.ctx.paths.agent_state.read_text())["zustand"] == "regelt"
    monkeypatch.setattr(agent.api, "result", real_result)
    registrations = api.registrations
    agent.run_once()
    assert api.registrations == registrations + 1  # registriert neu, meldet das gehaltene Ergebnis
    assert api.result_of(command_id) == {"ok": True, "result": {"setup_id": "setup-1"}, "error": None}


def test_wrong_clock_at_boot_recovers_without_intervention(api, data_dir, clock):
    bus = FakeBus()
    FakeZigbee2Mqtt(bus).bridge(online=True)
    mirror = ZigbeeMirror(bus, clock)
    mirror.start()
    paths = Paths(data_dir)
    ident = identity.load_or_create(paths)
    ctx = AgentContext(paths, bus, mirror, ident, clock=clock)
    ctx.start()
    offset = [-3 * 24 * 3600.0]  # Pi ohne Echtzeituhr: Wanduhr Tage daneben, bis NTP synchronisiert
    agent = AgentLoop(ctx, DeviceApiClient(api.url, ident, now=lambda: time.time() + offset[0]), sleep=lambda s: None)
    for _ in range(40):
        agent.run_once()
        clock.advance(1)
    assert api.registrations == 0 and not agent.server_ok
    assert json.loads(paths.agent_state.read_text())["zustand"] == "startet"
    offset[0] = 0.0  # NTP synchron
    for _ in range(70):  # laengstes Backoff bis hierhin: 64 s
        agent.run_once()
        clock.advance(1)
    assert api.registrations == 1 and agent.server_ok
    assert json.loads(paths.agent_state.read_text())["zustand"] == "nicht_uebernommen"


def test_poll_waits_for_poll_after(agent, api):
    agent.run_once()
    command_id = api.enqueue("zigbee_permit_join", {"seconds": 30})
    agent.run_once()  # Uhr steht: noch nicht faellig
    assert api.result_of(command_id) is None


def test_waiting_command_does_not_block_and_fails_after_its_deadline(agent, api, clock):
    agent.run_once()
    api.claim()
    command_id = api.enqueue("apply_config", {"setup_id": "setup-1", "config": apply_config()})
    clock.advance(POLL)
    agent.run_once()
    assert api.result_of(command_id) is None
    other = api.enqueue("zigbee_permit_join", {"seconds": 10})
    clock.advance(POLL)
    agent.run_once()  # der wartende Befehl blockiert weder Abholen noch Ausfuehren
    assert api.result_of(other)["ok"] is True and api.result_of(command_id) is None
    clock.advance(121)
    agent.run_once()
    assert api.result_of(command_id)["error"]["grund"] == "keine_bestaetigung"
    assert command_id in _done(agent)


def test_waiting_command_completes_when_confirmed(agent, api, clock):
    agent.run_once()
    api.claim()
    command_id = api.enqueue("apply_config", {"setup_id": "setup-1", "config": apply_config()})
    clock.advance(POLL)
    agent.run_once()
    agent.ctx.bus.publish(topics.STATUS, {"schema": 2, "status": "startet", "setup_id": "setup-1"}, retain=True)
    agent.run_once()
    assert api.result_of(command_id) == {"ok": True, "result": {"setup_id": "setup-1"}, "error": None}


def test_redelivered_sign_off_after_a_restart_reports_the_reset_before_cleanup(agent, api, clock, monkeypatch):
    agent.run_once()
    api.claim()
    write_runtime_files(agent.ctx.paths, apply_config())
    command_id = api.enqueue("sign_off", {})
    clock.advance(POLL)
    agent.run_once()  # sign_off wartet auf die Laufzeit
    assert api.result_of(command_id) is None
    setup_id = load_raw(agent.ctx.paths)["setup_id"]
    restarted = _restart(agent)  # Agent-Neustart, der wartende Befehl lebt nur im Speicher
    agent.ctx.bus.publish(
        topics.STATUS, {"schema": 2, "status": "abgemeldet", "setup_id": setup_id, "grund": None}, retain=True,
    )

    def unavailable(*args, **kwargs):
        raise loop_module.ApiUnavailable("test")

    monkeypatch.setattr(restarted.api, "register", unavailable)
    restarted.run_once()  # erster Kontakt scheitert: noch nicht aufraeumen
    assert agent.ctx.paths.runtime_config.exists()
    monkeypatch.undo()
    clock.advance(loop_module.MAX_BACKOFF_SECONDS)
    restarted.run_once()  # Server liefert sign_off erneut, Ergebnis zuerst, dann aufraeumen
    assert api.result_of(command_id) == {"ok": True, "result": {"zurueckgesetzt": True, "werte": {}}, "error": None}
    assert not agent.ctx.paths.runtime_config.exists()


def _waiting_kinds() -> set[str]:
    """Befehle, deren Handler Waiting liefern kann (nur diese laufen in den Zeitablauf der Schleife)."""
    return {kind for kind, handler in commands.HANDLERS.items() if "Waiting(" in inspect.getsource(handler)}


def test_waiting_kinds_are_the_known_ones():
    assert _waiting_kinds() == {"driver_inventory", "apply_config", "set_room_target", "sign_off"}


@pytest.mark.parametrize("kind", sorted(wire.COMMANDS))
def test_every_reason_the_loop_can_emit_is_in_the_contract(kind):
    # Handler-Gruende prueft commands._guarded (test_commands); die Schleife selbst meldet ungueltige_nutzlast
    # (unbekannter Befehl, execute) und bei wartenden Befehlen den Zeitablauf.
    emitted = {"ungueltige_nutzlast", "intern"} | ({loop_module.TIMEOUT_GRUND} if kind in _waiting_kinds() else set())
    assert emitted <= set(wire.command_errors(kind))


def test_timeout_reason_outside_the_contract_becomes_intern():
    assert loop_module.timed_out("zigbee_devices").grund == "intern"
    assert loop_module.timed_out("apply_config").grund == "keine_bestaetigung"


def test_status_and_notifications_are_uploaded(agent, api, clock):
    agent.run_once()
    agent.ctx.bus.publish(topics.STATUS, {"schema": 2, "status": "regelt", "setup_id": "s"}, retain=True)
    agent.ctx.bus.publish(
        topics.NOTIFY_PREFIX + "zugang", {"kategorie": "zugang", "kritisch": True, "text": "T", "ts": "x"}, retain=True,
    )
    agent.run_once()
    status = api.last_status()
    assert status["runtime_status"]["status"] == "regelt" and status["agent_state"] in ("nicht_uebernommen",)
    assert api.notifications[(agent.ctx.identity.device_id, "zugang")]["offen"] is True
    agent.ctx.bus.publish(topics.NOTIFY_PREFIX + "zugang", None, retain=True)
    agent.run_once()
    assert api.notifications[(agent.ctx.identity.device_id, "zugang")]["offen"] is False


def test_status_is_repeated_every_five_minutes(agent, api, clock):
    agent.run_once()
    count = len(api.statuses[agent.ctx.identity.device_id])
    clock.advance(10)
    agent.run_once()
    assert len(api.statuses[agent.ctx.identity.device_id]) == count
    clock.advance(loop_module.STATUS_EVERY_SECONDS)
    agent.run_once()
    assert len(api.statuses[agent.ctx.identity.device_id]) == count + 1


def test_server_down_backs_off_and_corrupt_done_file_is_ignored(agent, api, clock):
    agent.ctx.paths.agent_dir.mkdir(parents=True, exist_ok=True)
    (agent.ctx.paths.agent_dir / "done.json").write_text("{kaputt")
    api.stop()
    for _ in range(5):
        agent.run_once()
        clock.advance(1)
    assert not agent.server_ok
    clock.advance(301)
    agent.run_once()
    assert json.loads(agent.ctx.paths.agent_state.read_text())["zustand"] == "startet"  # nie Kontakt gehabt


def test_backoff_doubles_up_to_the_maximum(agent, api, clock, monkeypatch):
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(clock())
        raise loop_module.ApiUnavailable("test")

    monkeypatch.setattr(agent.api, "register", unavailable)
    for _ in range(2000):
        agent.run_once()
        clock.advance(1)
    gaps = [b - a for a, b in zip(calls, calls[1:], strict=False)]
    assert gaps[:4] == [2, 4, 8, 16]
    assert max(gaps) == loop_module.MAX_BACKOFF_SECONDS


def test_lost_connection_is_reported_after_five_minutes(agent, api, clock):
    agent.run_once()
    api.stop()
    clock.advance(1)
    agent.run_once()
    assert json.loads(agent.ctx.paths.agent_state.read_text())["zustand"] == "nicht_uebernommen"
    for _ in range(320):
        clock.advance(1)
        agent.run_once()
    assert json.loads(agent.ctx.paths.agent_state.read_text())["zustand"] == "keine_verbindung"
