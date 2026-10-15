"""Gateway-Szenario ab 0.6.0 (Spec SHG G2 7.3, Spec 5b 5.3): uebernommen, nicht eingerichtet -> Konfiguration ->
regelt -> Bedienung 22 °C -> Comfort-Boost -> Thermostat-Wunsch 14 °C (begrenzt 15) -> zwei Ack-Timeouts ->
Notbetrieb -> Rueckkehr -> sign_off mit Zuruecksetzen -> leere Konfiguration -> aufgeraeumt -> neu eingerichtet
(die Treiber-Anmeldung davor bleibt). Feste Uhr, FakeBus als lokaler Broker, Fake-Link (der Test spielt den Dienst),
Simulations-Treiber; Neustarts wie unter Compose."""
import json

import pytest
from configs import THERMOSTAT, bedienung, konfiguration, leer
from world import GatewayWorld

from smartheat_core import dateien
from smartheat_core.binding import BINDINGS
from smartheat_core.safety import resolve_local_safety
from smartheat_gateway.drivers.simulation import START_LEVERS
from smartheat_runtime.app import IDLE_NOT_CONFIGURED
from smartheat_runtime.soll_speicher import ECHO_WINDOW_SECONDS

# Gelernte Werte des Dienstes; bewusst ungleich den Ursprungswerten der Simulation (START_LEVERS), auf die sign_off
# die Hebel aus restore_originals zuruecksetzt: sonst bliebe Schritt 7 gruen, auch wenn nichts zurueckgesetzt wuerde.
LEVERS = {"curve": 1.4, "level": 3.0, "room_setpoint": 20.0}
#: Ursprungswerte = Anlagenwerte vor dem ersten eigenen Schreiben (viessmann_vicare: curve, level, room_setpoint)
URSPRUNG = {lever: START_LEVERS[lever] for lever in BINDINGS["viessmann_vicare"].restore_originals}


@pytest.fixture
def world(data_dir, monkeypatch, clock):
    world = GatewayWorld(data_dir, monkeypatch, clock)
    yield world
    world.close()


def _led(world: GatewayWorld) -> str:
    return json.loads(world.paths.agent_state.read_text())["zustand"]


def _plant(world: GatewayWorld) -> dict:
    return json.loads((world.paths.sim_dir / "plant.json").read_text())["hebel"]


def _thermostat(world: GatewayWorld) -> list[float]:
    return [body["occupied_heating_setpoint"] for topic, body in world.bus.decoded()
            if topic == f"zigbee2mqtt/{THERMOSTAT}/set"]


def _ergebnis(world: GatewayWorld, **match) -> dict:
    treffer = [body for link in world.links for body in link.of("up/result")
               if all(body.get(key) == value for key, value in match.items())]
    assert treffer, match
    return treffer[-1]


def _befehl(world: GatewayWorld, command_id: str, kind: str, payload: dict | None = None) -> None:
    world.server("down/command", {"schema": 1, "command_id": command_id, "kind": kind, "payload": payload or {},
                                  "expires_at": "2026-01-16T08:00:00+01:00"})


def test_gateway_scenario(world):
    # 1. uebernommen, nicht eingerichtet: Ruhezustand, hello mit Version 0, LED wartet
    world.start_runtime()
    assert world.result.reason == IDLE_NOT_CONFIGURED
    world.connect()
    hello = world.link.of("up/hello")[-1]
    assert hello["host"] == "gateway" and hello["konfiguration_version"] == 0
    assert hello["faehigkeiten"]["zigbee"] is True and "simulation" in hello["faehigkeiten"]["drivers"]
    assert _led(world) == "wartet_auf_einrichtung"
    code_vorher = world.device.identity.claim_code

    # 2. Konfiguration -> Ergebnis ok -> Neustart mit sauberem Trennen -> regelt
    world.konfiguration_senden(konfiguration(), 1)
    assert _ergebnis(world, dokument="konfiguration", version=1)["ok"] is True
    assert world.links[0].stopped and world.restarts == 2
    world.connect()
    assert world.status()["status"] == "regelt" and _led(world) == "regelt"
    assert world.link.of("up/hello")[-1]["konfiguration_version"] == 1
    world.answer(LEVERS)  # Antwort auf den Erststart-Tick

    # 3. Bedienung 22 -> Thermostat, Entprellung, Comfort-Boost
    world.bedienung_senden(bedienung(22.0), 1)
    assert _ergebnis(world, dokument="bedienung", version=1)["ok"] is True
    assert 22.0 in _thermostat(world)
    world.advance(10)
    assert world.result.store.state.boost_active
    boost = resolve_local_safety("viessmann_vicare", "Heizkoerper").comfort_boost
    assert {lever: _plant(world)[lever] for lever in boost} == dict(boost)
    world.answer(LEVERS)  # der Dienst beantwortet den Tick der Soll-Aenderung

    # 4. Thermostat nachts auf 14 gedreht: ein Wunsch, begrenzt auf 15 (roh 14), das Thermostat zeigt 15. Erst nach dem
    # Echo-Fenster des Soll-Speichers (60 s nach dem eigenen Schreiben von 22 gilt jede Meldung als Echo).
    world.advance(ECHO_WINDOW_SECONDS)
    for wert in (18.0, 16.0, 14.0):
        world.z2m.report(THERMOSTAT, occupied_heating_setpoint=wert, local_temperature=20.0)
        world.advance(3)
    world.advance(10)
    wuensche = world.link.of("up/wish")
    assert len(wuensche) == 1
    assert wuensche[0]["werte"] == {"raum_soll": 15.0} and wuensche[0]["roh"] == 14.0
    assert wuensche[0]["herkunft"] == "thermostat" and wuensche[0]["basis_version"] == 1
    assert _thermostat(world)[-1] == 15.0
    assert any(item["key"] == "wunsch_begrenzt" and item["offen"]
               for body in world.link.of("up/notifications") for item in body["items"])
    # Der Dienst nimmt den Wunsch an und gibt ihn als Bedienung zurueck (quelle geraet, wire.QUELLEN).
    world.bedienung_senden({**bedienung(15.0), "quelle": "geraet"}, 2)
    assert _ergebnis(world, dokument="bedienung", version=2)["ok"] is True
    assert world.device.bedienung()["raum_soll"] == 15.0
    assert json.loads(world.paths.room_target.read_text())["value"] == 15.0 and _thermostat(world)[-1] == 15.0

    # 5. zwei Ack-Timeouts -> Notbetrieb, LED stoerung
    world.advance(30)
    world.advance(30)
    assert world.result.store.state.delivery.notbetrieb
    world.advance(1)
    assert _led(world) == "stoerung"

    # 6. Rueckkehr: der naechste Zustellversuch wird beantwortet
    world.advance(300)
    world.answer(LEVERS)
    assert not world.result.store.state.delivery.notbetrieb
    world.advance(1)
    assert _led(world) == "regelt"

    # 7. sign_off -> Neustart in den Abmelde-Pfad -> Ergebnis mit den zurueckgesetzten Werten (Ursprungswerte)
    assert any(_plant(world)[lever] != wert for lever, wert in URSPRUNG.items())  # die Anlage steht auf Gelerntem
    _befehl(world, "c-1", "sign_off")
    assert world.restarts == 3
    assert not [body for link in world.links for body in link.of("up/result") if body.get("command_id") == "c-1"]
    world.connect()
    _befehl(world, "c-1", "sign_off")  # der Dienst stellt nach hello erneut zu; kein zweiter Neustart
    world.advance(1)
    assert world.restarts == 3 and world.status()["status"] == "abgemeldet"
    assert len([body for body in world.link.of("up/result") if body.get("command_id") == "c-1"]) == 1
    ergebnis = _ergebnis(world, command_id="c-1")
    assert ergebnis["ok"] is True and ergebnis["result"]["zurueckgesetzt"] is True
    assert {lever: ergebnis["result"]["werte"].get(lever) for lever in URSPRUNG} == URSPRUNG
    assert {lever: _plant(world)[lever] for lever in URSPRUNG} == URSPRUNG

    # 8. leere Konfiguration (entfernt, erst nach dem sign_off-Ergebnis, Spec 5b 5) -> Neustart -> der Start setzt mit
    # der Konfiguration der Markierung zurueck (nach dem sign_off nichts mehr zu tun) -> zweiter Neustart -> dieser
    # Start raeumt auf (Spec Stufe 3 S3 Abschnitt 4)
    dateien.schreiben_json(world.paths.driver_secrets_dir / "vicare.json", {"refresh_token": "test-token"}, modus=dateien.LESBAR)
    world.konfiguration_senden(leer(), 2)
    assert _ergebnis(world, dokument="konfiguration", version=2)["ok"] is True
    assert world.restarts == 5 and world.result.reason == IDLE_NOT_CONFIGURED
    assert not world.paths.runtime_dir.exists() and not world.paths.aufraeumen.exists()
    assert not (world.paths.driver_secrets_dir / "vicare.json").exists()
    world.connect()
    assert world.status() is None and world.geraet.zustand.raum is None
    assert _led(world) == "wartet_auf_einrichtung"
    assert world.device.identity.claim_code == code_vorher  # Identitaet bleibt (der Server gibt frei, Plan S2)

    # 9. neu einrichten: eine Treiber-Anmeldung im Assistenten vor der Einrichtung ueberlebt deren Neustart
    dateien.schreiben_json(world.paths.driver_secrets_dir / "vicare.json", {"refresh_token": "test-token"}, modus=dateien.LESBAR)
    world.konfiguration_senden(konfiguration(), 3)
    assert _ergebnis(world, dokument="konfiguration", version=3)["ok"] is True
    assert world.restarts == 6 and (world.paths.driver_secrets_dir / "vicare.json").exists()
    world.connect()
    assert world.status()["status"] == "regelt" and _led(world) == "regelt"


def test_a_configuration_below_the_minimum_version_still_reaches_the_updater(world):
    # Review Focus 4: update_noetig, aber die Soll-Software liegt fuer den Updater bereit.
    world.start_runtime()
    world.connect()
    software = {"version": "0.7.0", "manifest_url": "https://x.test/m.json", "manifest_sha256": "a" * 64,
                "signature": "s"}
    world.konfiguration_senden(konfiguration(mindest_software="0.7.0", software=software), 1)
    ergebnis = _ergebnis(world, dokument="konfiguration", version=1)
    assert ergebnis["ok"] is False and ergebnis["error"]["grund"] == "update_noetig"
    assert json.loads(world.paths.software_soll.read_text()) == software
    assert world.restarts == 1 and _led(world) == "update_noetig"
