"""Plan 5b D2 Task 5: ein Prozess aus Geraetekern und Laufzeit - Neustart bei neuer Konfiguration mit sauberem
Trennen, Aufraeumen nach dem Entfernen, Raum im Prozess, Bedienung beim Start, Lebenszeichen."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from configs import THERMOSTAT, bedienung, dokumente_schreiben, konfiguration
from fakes import FakeBus
from geraet_fakes import geraet_fuer
from world import GatewayWorld

from smartheat_core import dateien
from smartheat_device import prozess
from smartheat_gateway import runtime_main
from smartheat_gateway.paths import Paths
from smartheat_gateway.zigbee import ZigbeeMirror
from smartheat_runtime import app, aufraeumen
from smartheat_runtime.app import IDLE_NOT_CONFIGURED, IdleBridge
from smartheat_runtime.runtime import EV_RECHECK, Runtime
from smartheat_runtime.status import STATUS_ABGEMELDET
from smartheat_runtime.worker import Event, RegulationWorker

SOFTWARE = {"version": "0.6.1", "manifest_url": "https://x.test/m.json", "manifest_sha256": "a" * 64, "signature": "s"}


@pytest.fixture
def world(data_dir, monkeypatch, clock):
    world = GatewayWorld(data_dir, monkeypatch, clock)
    yield world
    world.close()


def _thermostat_writes(world):
    return [body["occupied_heating_setpoint"] for topic, body in world.bus.decoded()
            if topic == f"zigbee2mqtt/{THERMOSTAT}/set"]


def test_not_configured_is_idle_without_status_and_room(world):
    result = world.start_runtime()
    assert isinstance(result, IdleBridge) and result.reason == IDLE_NOT_CONFIGURED
    assert world.status() is None and world.geraet.zustand.raum is None


def test_an_operation_while_not_configured_is_not_posted_to_the_idle_worker(world, caplog):
    world.start_runtime()
    world.connect()
    with caplog.at_level("WARNING"):
        world.bedienung_senden(bedienung(21.0), 1)
    assert world.device.bedienung()["raum_soll"] == 21.0  # gespeichert; gilt beim naechsten Start mit Regelung
    assert "Kein Handler" not in caplog.text


def test_a_configuration_from_the_server_restarts_into_regulation(world):
    world.start_runtime()
    world.connect()
    world.konfiguration_senden(konfiguration(), 1)
    assert world.restarts == 2 and isinstance(world.result, Runtime)
    assert world.links[0].stopped  # alter Prozess hat sauber getrennt
    world.connect()
    assert world.status()["status"] == "regelt" and world.status()["setup_id"] == "test-tenant"
    assert world.link.of("up/hello")[0]["konfiguration_version"] == 1


def test_reload_disconnects_the_link_before_exit(world):
    dokumente_schreiben(world.paths, konfiguration())
    world.start_runtime()
    world.connect()
    first = world.link
    world.geraet.neustart_anfordern()
    with pytest.raises(prozess.Neustart):
        world.result.worker.run_pending()
    assert not first.stopped  # der Worker trennt nicht selbst
    prozess.beenden(world.device, None, None)
    assert first.stopped


def test_the_config_error_recheck_restarts_through_the_reload_path(world, monkeypatch):
    """Der Ruhezustand Konfigurationsfehler prueft per Neustart neu (app._fail_start); auch der trennt zuerst sauber
    statt os.execv (sonst DUPLICATE_CLIENTID beim Neuverbinden)."""
    dokumente_schreiben(world.paths, konfiguration(raumfuehler=["test-falsch"]))
    result = world.start_runtime()
    assert isinstance(result, IdleBridge) and result.reason == "konfigurationsfehler"
    world.connect()
    first = world.link
    execv = []
    monkeypatch.setattr("smartheat_runtime.abo.restart_process", lambda: execv.append(1))
    world.clock.advance(app.CONFIG_RECHECK_SECONDS)
    with pytest.raises(prozess.Neustart):
        world.result.worker.run_pending()
    assert not first.stopped  # der Worker trennt nicht selbst
    prozess.beenden(world.device, None, None)
    assert first.stopped and execv == []


def test_the_sign_off_retry_stays_a_retry(data_dir, clock):
    """Im Ruhezustand abgemeldet ist EV_RECHECK das erneute Zuruecksetzen (app._sign_off), kein Neustart."""
    worker = RegulationWorker(clock=clock)
    retries = []
    worker.register(EV_RECHECK, retries.append)
    bridge = IdleBridge(worker=worker, status=None, reason=STATUS_ABGEMELDET)
    mirror = ZigbeeMirror(FakeBus(), clock)
    geraet = geraet_fuer(Paths(data_dir), mirror, konfiguration())
    runtime_main.attach(bridge, SimpleNamespace(raum=None), geraet)
    worker.post(Event(EV_RECHECK))
    worker.run_pending()
    assert len(retries) == 1 and not geraet.device.stopped


def test_abo_reactivation_restarts_through_the_reload_path(world, monkeypatch):
    """Abo am Fristende wieder aktiv (abo.check_grace_end): Neustart ueber den Neustartweg des Gateways, der Link
    trennt vorher sauber."""
    dokumente_schreiben(world.paths, konfiguration(abo="inaktiv"))
    world.start_runtime()
    world.connect()
    first = world.link
    monkeypatch.setattr(world.device, "abo_status", lambda: "active")
    world.wall.advance(31 * 86400)
    world.clock.advance(31 * 86400)
    with pytest.raises(prozess.Neustart):
        world.result.worker.run_pending()
    assert not first.stopped  # der Worker trennt nicht selbst
    prozess.beenden(world.device, None, None)
    assert first.stopped


def test_a_software_only_configuration_does_not_restart(world):
    dokumente_schreiben(world.paths, konfiguration())
    world.start_runtime()
    world.connect()
    world.konfiguration_senden(konfiguration(software=SOFTWARE), 2)
    assert world.restarts == 1
    assert json.loads(world.paths.software_soll.read_text())["version"] == "0.6.1"


def test_the_cleanup_runs_at_the_next_start_after_an_empty_configuration(world):
    for path in (world.paths.backup, world.paths.abgemeldet, world.paths.inventur,
                 world.paths.driver_secrets_dir / "vicare.json"):
        dateien.schreiben_json(path, {"x": 1}, modus=dateien.LESBAR)
    dateien.schreiben_json(world.paths.aufraeumen, {"anlage": "test-tenant"}, modus=dateien.LESBAR)
    world.start_runtime()
    assert not world.paths.backup.exists() and not world.paths.abgemeldet.exists()
    assert not world.paths.inventur.exists() and not (world.paths.driver_secrets_dir / "vicare.json").exists()
    assert not world.paths.aufraeumen.exists()


def test_a_restart_before_any_setup_keeps_the_driver_login(world):
    dateien.schreiben_json(world.paths.driver_secrets_dir / "vicare.json", {"refresh_token": "test-token"}, modus=dateien.LESBAR)
    world.start_runtime()
    world.start_runtime()
    assert (world.paths.driver_secrets_dir / "vicare.json").exists()


def test_a_marker_from_before_a_new_setup_deletes_nothing(world):
    """Zwischen Markierung und Neustart kam schon eine neue Einrichtung: die frische Treiber-Anmeldung bleibt."""
    dateien.schreiben_json(world.paths.driver_secrets_dir / "vicare.json", {"refresh_token": "test-token"}, modus=dateien.LESBAR)
    dateien.schreiben_json(world.paths.aufraeumen, {"anlage": "test-tenant"}, modus=dateien.LESBAR)
    dokumente_schreiben(world.paths, konfiguration(anlage="neue-anlage"))
    world.start_runtime()
    assert (world.paths.driver_secrets_dir / "vicare.json").exists()
    assert not world.paths.aufraeumen.exists()


def _offenes_zuruecksetzen(world):
    """Zustand und Treiber-Anmeldung der eingerichteten Anlage, gespeichert ist schon die leere Konfiguration."""
    for path in (world.paths.backup, world.paths.inventur, world.paths.driver_secrets_dir / "vicare.json"):
        dateien.schreiben_json(path, {"x": 1}, modus=dateien.LESBAR)
    aufraeumen.markieren(world.paths.aufraeumen, konfiguration())
    dokumente_schreiben(world.paths, {**konfiguration(), "anlage": None})


def test_the_cleanup_waits_while_the_reset_is_pending(world):
    """Spec Stufe 3 S3 Abschnitt 4: vor dem Zuruecksetzen bleiben Laufzeit-Ordner, Treiber-Anmeldung (das Zuruecksetzen
    braucht sie) und Markierung."""
    _offenes_zuruecksetzen(world)
    runtime_main.aufraeumen(world.paths)
    assert world.paths.runtime_dir.exists() and world.paths.backup.exists()
    assert (world.paths.driver_secrets_dir / "vicare.json").exists() and world.paths.inventur.exists()
    assert world.paths.aufraeumen.exists()


def test_the_cleanup_after_the_reset_deletes_everything(world):
    _offenes_zuruecksetzen(world)
    assert aufraeumen.zurueckgesetzt(world.paths.aufraeumen, "test-tenant")
    runtime_main.aufraeumen(world.paths)
    assert not world.paths.runtime_dir.exists() and not world.paths.inventur.exists()
    assert not (world.paths.driver_secrets_dir / "vicare.json").exists() and not world.paths.aufraeumen.exists()


def test_an_empty_configuration_resets_with_the_driver_then_restarts_and_forgets(world):
    """Leer ohne sign_off: der Start setzt mit dem Treiber zurueck (Status abgemeldet), startet einmal neu, und erst
    dieser Start vergisst die Anlage samt Treiber-Anmeldung."""
    _offenes_zuruecksetzen(world)
    result = world.start_runtime()
    assert isinstance(result, IdleBridge) and result.reason == STATUS_ABGEMELDET
    assert world.status()["status"] == "abgemeldet" and world.status()["setup_id"] == "test-tenant"
    assert (world.paths.driver_secrets_dir / "vicare.json").exists()
    world.run()
    assert world.restarts == 2 and world.result.reason == IDLE_NOT_CONFIGURED
    assert not world.paths.runtime_dir.exists() and not world.paths.aufraeumen.exists()
    assert not (world.paths.driver_secrets_dir / "vicare.json").exists()


def test_unchanged_configuration_keeps_the_alive_file_fresh(world):
    dokumente_schreiben(world.paths, konfiguration())
    world.start_runtime()
    world.connect()
    alive = world.paths.data / "alive"
    alive.unlink()
    world.advance(5 * runtime_main.CONFIG_WATCH_SECONDS)
    assert world.restarts == 1 and alive.exists()


def test_a_failing_alive_file_does_not_stop_the_config_watch(world, monkeypatch):
    dokumente_schreiben(world.paths, konfiguration())
    world.start_runtime()
    alive = world.paths.data / "alive"
    alive.unlink()
    touch, kaputt = Path.touch, [True]

    def touch_or_fail(self, *args, **kwargs):
        if kaputt[0]:
            raise OSError("read-only file system")
        return touch(self, *args, **kwargs)

    monkeypatch.setattr("pathlib.Path.touch", touch_or_fail)
    world.advance(runtime_main.CONFIG_WATCH_SECONDS + 1)
    assert not alive.exists()
    kaputt[0] = False
    world.advance(runtime_main.CONFIG_WATCH_SECONDS + 1)  # die Wache laeuft weiter
    assert alive.exists() and world.restarts == 1


def test_the_target_software_of_the_stored_configuration_is_replayed_at_start(world):
    """Ein Neustart kann den Prozess beenden, bevor der Geraete-Thread software_soll.json schrieb (der Server stellt
    eine bestaetigte Version nicht erneut zu); der naechste Start holt es nach."""
    dokumente_schreiben(world.paths, konfiguration(software=SOFTWARE))
    world.start_runtime()
    assert json.loads(world.paths.software_soll.read_text())["version"] == "0.6.1"


def test_no_replay_after_a_rejected_configuration(world):
    """Die zuletzt empfangene Konfiguration wurde abgelehnt (z. B. Mindestversion): ihre Soll-Software steht schon in
    software_soll.json und gilt; die aeltere gespeicherte Konfiguration ueberschreibt sie nicht."""
    dokumente_schreiben(world.paths, konfiguration())
    dateien.schreiben_json(world.paths.device_dir / "dokumente.json",
                           {"update_anlass": None, "abgelehnt": {"konfiguration": [2, "update_noetig", "t"]}},
                           modus=dateien.PRIVAT)
    dateien.schreiben_json(world.paths.software_soll, SOFTWARE, modus=dateien.LESBAR)
    world.start_runtime()
    assert json.loads(world.paths.software_soll.read_text())["version"] == "0.6.1"


def test_no_replay_without_a_stored_configuration(world):
    world.start_runtime()
    assert not world.paths.software_soll.exists()


def test_room_is_kept_in_the_process_on_a_new_operation(world):
    dokumente_schreiben(world.paths, konfiguration())
    world.start_runtime()
    world.connect()
    world.bedienung_senden(bedienung(21.5), 1)
    raum = world.geraet.zustand.raum
    assert (raum["soll"], raum["soll_quelle"], raum["ist"]) == (21.5, "portal", 20.0)
    assert 21.5 in _thermostat_writes(world)


def test_room_is_refreshed_every_tick(world):
    dokumente_schreiben(world.paths, konfiguration())
    world.start_runtime()
    world.connect()
    first = world.geraet.zustand.raum["ts"]
    world.wall.advance(1)
    world.advance(runtime_main.RAUM_SECONDS)
    assert world.geraet.zustand.raum["ts"] != first


def test_the_operation_is_applied_and_the_thermostat_aligned_at_start(world):
    dokumente_schreiben(world.paths, konfiguration(), bedienung(22.0))
    world.start_runtime()
    world.run()
    assert world.host._store.value == 22.0 and _thermostat_writes(world) == [22.0]


def test_a_newer_operation_replaces_the_stored_target_at_start(world):
    dateien.schreiben_json(world.paths.room_target, {"value": 20.0, "source": "portal", "ts": "t"}, modus=dateien.LESBAR)
    dokumente_schreiben(world.paths, konfiguration(), bedienung(22.5))
    world.start_runtime()
    world.run()
    assert world.host._store.value == 22.5 and 22.5 in _thermostat_writes(world)


def test_sigterm_ends_like_a_restart():
    with pytest.raises(SystemExit) as stop:
        runtime_main._terminate(15, None)
    assert stop.value.code == 0
