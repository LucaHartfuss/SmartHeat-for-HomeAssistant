"""Gateway-Szenario (Spec SHG G2 7.3): nicht eingerichtet -> apply_config -> regelt -> Portal-Soll -> Comfort-Boost ->
zwei Ack-Timeouts -> Notbetrieb -> Rueckkehr -> sign_off mit Zuruecksetzen -> nicht_uebernommen. Feste Uhr, FakeBus als
lokaler Broker, echte HTTP-Fake-Geraete-API, Simulations-Treiber; Laufzeit-Neustarts wie unter Compose (SystemExit 0)."""
import json

from configs import THERMOSTAT, apply_config
from fake_device_api import FakeDeviceApi
from world import GatewayWorld

from smartheat_core.safety import resolve_local_safety
from smartheat_gateway.agent import identity
from smartheat_gateway.agent.api_client import DeviceApiClient
from smartheat_gateway.agent.context import AgentContext
from smartheat_gateway.agent.loop import AgentLoop
from smartheat_gateway.zigbee import ZigbeeMirror
from smartheat_runtime.app import IDLE_NOT_CONFIGURED

LEVERS = {"curve": 1.0, "level": 0.0, "room_setpoint": 20.0}


def _agent(world: GatewayWorld, api: FakeDeviceApi) -> AgentLoop:
    mirror = ZigbeeMirror(world.bus, world.clock)
    mirror.start()
    ident = identity.load_or_create(world.paths)
    ctx = AgentContext(world.paths, world.bus, mirror, ident, clock=world.clock)
    ctx.start()
    return AgentLoop(ctx, DeviceApiClient(api.url, ident), sleep=lambda seconds: None)


def _agent_state(agent: AgentLoop) -> str:
    return json.loads(agent.ctx.paths.agent_state.read_text())["zustand"]


def _plant(world: GatewayWorld) -> dict:
    return json.loads((world.paths.sim_dir / "plant.json").read_text())["hebel"]


def test_gateway_scenario(data_dir, monkeypatch, clock, api):
    world = GatewayWorld(data_dir, monkeypatch, clock)
    agent = _agent(world, api)
    claim_code = agent.ctx.identity.claim_code

    # 1. nicht eingerichtet, nicht uebernommen
    world.start_runtime()
    assert world.result.reason == IDLE_NOT_CONFIGURED
    agent.run_once()
    assert _agent_state(agent) == "nicht_uebernommen"
    api.claim()
    clock.advance(2)
    agent.run_once()
    assert _agent_state(agent) == "wartet_auf_einrichtung"

    # 2. apply_config -> Laufzeit startet neu -> regelt
    command = api.enqueue("apply_config", {"setup_id": "setup-1", "config": apply_config()})
    clock.advance(2)
    agent.run_once()
    world.run()                      # shg/cmd/reload -> SystemExit 0 -> Neustart mit Konfiguration
    world.connect()
    clock.advance(2)
    agent.run_once()
    assert api.result_of(command) == {"ok": True, "result": {"setup_id": "setup-1"}, "error": None}
    assert world.status()["status"] == "regelt" and _agent_state(agent) == "regelt"
    world.answer(LEVERS)             # Antwort auf den Erststart-Tick

    # 3. Portal-Soll 22 -> Thermostat, Entprellung, Comfort-Boost
    command = api.enqueue("set_room_target", {"value": 22.0})
    clock.advance(2)
    agent.run_once()
    world.run()
    assert ("zigbee2mqtt/" + THERMOSTAT + "/set", {"occupied_heating_setpoint": 22.0}) in world.bus.decoded()
    world.advance(10)
    clock.advance(1)
    agent.run_once()
    assert api.result_of(command)["result"] == {"value": 22.0}
    assert world.result.store.state.boost_active
    boost = resolve_local_safety("viessmann_vicare", "Heizkoerper").comfort_boost
    assert {lever: _plant(world)[lever] for lever in boost} == dict(boost)

    # 4. zwei Ack-Timeouts -> Notbetrieb, Agent zeigt Stoerung
    world.advance(30)
    world.advance(30)
    assert world.result.store.state.delivery.notbetrieb
    clock.advance(1)
    agent.run_once()
    assert _agent_state(agent) == "stoerung"

    # 5. Rueckkehr: naechster Zustellversuch wird beantwortet
    world.advance(300)
    world.answer(LEVERS)
    assert not world.result.store.state.delivery.notbetrieb

    # 6. sign_off -> Zuruecksetzen -> aufgeraeumt -> wieder frei, gleicher Code
    command = api.enqueue("sign_off", {})
    clock.advance(2)
    agent.run_once()
    world.run()                      # Neustart in den Abmelde-Pfad
    assert world.status()["status"] == "abgemeldet" and world.status()["grund"] is None
    clock.advance(2)
    agent.run_once()                 # Ergebnis, dann Aufraeumen
    result = api.result_of(command)["result"]
    assert result["zurueckgesetzt"] is True
    assert result["werte"]           # nicht leer, sonst pruefte die Schleife unten nichts
    plant = _plant(world)
    assert all(plant[lever] == value for lever, value in result["werte"].items())
    assert not world.paths.runtime_config.exists() and not world.paths.runtime_dir.exists()
    world.run()                      # Neustart ohne Konfiguration
    assert world.result.reason == IDLE_NOT_CONFIGURED and world.status() is None
    api.release()
    clock.advance(61)
    agent.run_once()
    assert _agent_state(agent) == "nicht_uebernommen"
    assert agent.ctx.identity.claim_code == claim_code
