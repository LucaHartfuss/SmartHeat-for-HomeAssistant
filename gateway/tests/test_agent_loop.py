import inspect
import json

import pytest
from configs import apply_config
from fake_device_api import FakeDeviceApi
from fake_z2m import FakeZigbee2Mqtt
from fakes import FakeBus

from smartheat_gateway import topics
from smartheat_gateway.agent import commands, identity, wire
from smartheat_gateway.agent import loop as loop_module
from smartheat_gateway.agent.api_client import DeviceApiClient
from smartheat_gateway.agent.context import AgentContext
from smartheat_gateway.agent.loop import AgentLoop
from smartheat_gateway.paths import Paths
from smartheat_gateway.zigbee import ZigbeeMirror

POLL = 1  # FakeDeviceApi.poll_after


@pytest.fixture
def api():
    server = FakeDeviceApi()
    server.start()
    yield server
    server.stop()


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
    return json.loads((agent.ctx.paths.agent_dir / "done.json").read_text())


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
    restarted = AgentLoop(agent.ctx, agent.api, sleep=lambda seconds: None)
    restarted.run_once()
    assert agent.ctx.z2m_requests() == [30]


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
