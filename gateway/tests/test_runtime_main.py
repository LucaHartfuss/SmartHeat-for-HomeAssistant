"""Ein Prozess aus Geraetekern und Laufzeit - Neustart bei neuer Konfiguration mit sauberem
Trennen, Aufraeumen nach dem Entfernen, Raum im Prozess, Bedienung beim Start, Lebenszeichen."""
import http.client
import json
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
from configs import THERMOSTAT, bedienung, dokumente_schreiben, konfiguration, leer
from fakes import FakeBus
from geraet_fakes import geraet_fuer
from world import GatewayWorld

from smartheat_core import dateien
from smartheat_device import prozess
from smartheat_gateway import anmeldung, runtime_main
from smartheat_gateway.bus import BUS_PORT
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
    assert world.status()["status"] == "regelt" and world.status()["tenant_id"] == "test-tenant"
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


def test_the_config_error_recheck_restarts_through_the_reload_path(world):
    """Der Ruhezustand Konfigurationsfehler prueft per Neustart neu (app._fail_start, Rueckruf neustart aus
    runtime_main.start); auch der trennt zuerst sauber (sonst DUPLICATE_CLIENTID beim Neuverbinden)."""
    dokumente_schreiben(world.paths, konfiguration(raumfuehler=["test-falsch"]))
    result = world.start_runtime()
    assert isinstance(result, IdleBridge) and result.reason == "konfigurationsfehler"
    world.connect()
    first = world.link
    world.clock.advance(app.CONFIG_RECHECK_SECONDS)
    with pytest.raises(prozess.Neustart):
        world.result.worker.run_pending()
    assert not first.stopped  # der Worker trennt nicht selbst
    prozess.beenden(world.device, None, None)
    assert first.stopped


def test_the_sign_off_retry_stays_a_retry(data_dir, clock):
    """Im Ruhezustand abgemeldet ist EV_RECHECK das erneute Zuruecksetzen (app._sign_off), kein Neustart."""
    worker = RegulationWorker(clock=clock)
    retries = []
    worker.register(EV_RECHECK, retries.append)
    bridge = IdleBridge(worker=worker, status=None, reason=STATUS_ABGEMELDET, neustart=prozess.neustart_der_laufzeit)
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
    dateien.schreiben_json(world.paths.driver_secrets_dir / "vicare.json", {"refresh_token": "test-token"},
                           modus=dateien.LESBAR)
    world.start_runtime()
    world.start_runtime()
    assert (world.paths.driver_secrets_dir / "vicare.json").exists()


def test_a_marker_from_before_a_new_setup_deletes_nothing(world):
    """Zwischen Markierung und Neustart kam schon eine neue Einrichtung: die frische Treiber-Anmeldung bleibt."""
    dateien.schreiben_json(world.paths.driver_secrets_dir / "vicare.json", {"refresh_token": "test-token"},
                           modus=dateien.LESBAR)
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
    """Vor dem Zuruecksetzen bleiben Laufzeit-Ordner, Treiber-Anmeldung (das Zuruecksetzen
    braucht sie) und Markierung."""
    _offenes_zuruecksetzen(world)
    runtime_main.aufraeumen(world.paths)
    assert world.paths.runtime_dir.exists() and world.paths.backup.exists()
    assert (world.paths.driver_secrets_dir / "vicare.json").exists() and world.paths.inventur.exists()
    assert world.paths.aufraeumen.exists()


def test_the_cleanup_reads_the_stored_configuration_without_writing_the_lock(world):
    """Nur-Lese-Aufbau; die Rastung einer gespeicherten Konfiguration ohne Rastung schreibt erst der
    Geraetekern."""
    aufraeumen.markieren(world.paths.aufraeumen, konfiguration())
    dokumente_schreiben(world.paths, konfiguration(anlage="neue-anlage"))
    assert not (world.paths.device_dir / "sicherheitssatz.json").exists()
    runtime_main.aufraeumen(world.paths)
    assert not world.paths.aufraeumen.exists()  # Neueinrichtung: Markierung verworfen
    assert not (world.paths.device_dir / "sicherheitssatz.json").exists()


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
    assert world.status()["status"] == "abgemeldet" and world.status()["tenant_id"] == "test-tenant"
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


def _neustart(world) -> None:
    """Prozessende und neuer Start auf denselben Dateien (wie Compose nach Exit 0)."""
    prozess.beenden(world.device, None, None)
    world.start_runtime()


def _soll_software(world):
    return json.loads(world.paths.software_soll.read_text()) if world.paths.software_soll.exists() else "fehlt"


def test_the_offer_of_a_rejected_configuration_is_replayed_after_a_restart(world):
    """software_soll.json ging verloren (Neustart vor dem Schreiben, Stromausfall); der Server
    stellt die abgelehnte Version nicht erneut zu, der naechste Start bietet genau das gemerkte Angebot an."""
    world.start_runtime()
    world.connect()
    world.konfiguration_senden(konfiguration(mindest_software="9.0.0", software=SOFTWARE), 1)
    assert world.link.of("up/result")[-1]["error"]["grund"] == "update_noetig" and _soll_software(world) == SOFTWARE
    world.paths.software_soll.unlink()
    _neustart(world)
    assert _soll_software(world) == SOFTWARE


def test_a_write_error_while_replaying_the_offer_is_logged_and_the_start_goes_on(world, monkeypatch, caplog):
    """F-gateway-host-31: scheitert das Schreiben von software_soll.json beim Nachholen (Datentraeger voll, nur lesbar),
    loggt der Start den Fehler nur mit seiner Art und laeuft weiter; der naechste Start holt das Angebot nach.
    Die Attrappe sitzt an der Dateisystem-Grenze (dateien.schreiben_json)."""
    world.start_runtime()
    world.connect()
    world.konfiguration_senden(konfiguration(mindest_software="9.0.0", software=SOFTWARE), 1)
    world.paths.software_soll.unlink()
    schreiben = dateien.schreiben_json

    def voll(pfad, *args, **kwargs):
        if pfad == world.paths.software_soll:
            raise OSError(28, "No space left on device: GEHEIM")
        return schreiben(pfad, *args, **kwargs)

    monkeypatch.setattr(dateien, "schreiben_json", voll)
    starts_vorher = world.restarts
    with caplog.at_level("ERROR"):
        _neustart(world)
    # der Start ging weiter: neuer Prozess wie ohne Fehler (die Konfiguration verlangt 9.0.0 und ist abgelehnt, daher
    # Ruhezustand statt Regelung)
    assert world.restarts == starts_vorher + 1
    assert isinstance(world.result, IdleBridge) and world.result.reason == IDLE_NOT_CONFIGURED
    assert _soll_software(world) == "fehlt"
    assert "Soll-Software nicht nachgeholt (OSError)" in caplog.text and "GEHEIM" not in caplog.text
    monkeypatch.setattr(dateien, "schreiben_json", schreiben)
    _neustart(world)
    assert _soll_software(world) == SOFTWARE  # der naechste Start holt das Angebot nach


def test_a_later_invalid_offer_or_an_empty_configuration_without_software_keeps_the_replayed_offer(world):
    world.start_runtime()
    world.connect()
    world.konfiguration_senden(konfiguration(mindest_software="9.0.0", software=SOFTWARE), 1)
    world.konfiguration_senden(konfiguration(software={**SOFTWARE, "extra": 1}), 2)
    ohne_software = leer()
    del ohne_software["software"]
    world.konfiguration_senden(ohne_software, 3)
    assert [r["error"]["grund"] for r in world.link.of("up/result")] == ["update_noetig", "ungueltig", "ungueltig"]
    world.paths.software_soll.unlink()
    _neustart(world)
    assert _soll_software(world) == SOFTWARE


def test_a_withdrawn_offer_is_replayed_as_null(world):
    """software null zieht das Angebot zurueck und wird gemerkt: ging das Schreiben von null verloren (die alte
    software_soll.json mit dem Angebot steht noch), schreibt der naechste Start null; der Updater liest null als "kein
    Angebot" (belegt in gateway/host/tests/test_kanal.py::test_no_software_in_the_configuration_offers_nothing, eigener
    pythonpath, daher hier nicht importiert)."""
    world.start_runtime()
    world.connect()
    world.konfiguration_senden(konfiguration(mindest_software="9.0.0", software=SOFTWARE), 1)
    world.konfiguration_senden(konfiguration(mindest_software="9.0.0", software=None), 2)
    assert _soll_software(world) is None  # der Geraete-Thread gab null an den Updater
    dateien.schreiben_json(world.paths.software_soll, SOFTWARE, modus=dateien.LESBAR)  # Schreiben von null verloren
    _neustart(world)
    assert _soll_software(world) is None  # null = kein Angebot fuer den Updater (kanal.desired() -> {})


def test_a_stored_configuration_without_a_remembered_offer_is_not_replayed(world):
    """Kein Kompatibilitaetspfad: eine gespeicherte Konfiguration mit software, aber ohne software_angebot in
    dokumente.json, ruft den Updater beim Start nicht."""
    dokumente_schreiben(world.paths, konfiguration(software=SOFTWARE))
    world.start_runtime()
    assert _soll_software(world) == "fehlt"


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
    # das Thermostat hat vor dem Start gemeldet (retained): bekannter Zustand, an
    world.z2m.report(THERMOSTAT, system_mode="heat", occupied_heating_setpoint=20.0, local_temperature=20.0)
    dokumente_schreiben(world.paths, konfiguration(), bedienung(22.0))
    world.start_runtime()
    world.run()
    assert world.host._store.value == 22.0 and _thermostat_writes(world) == [22.0]


def test_without_a_known_thermostat_state_the_start_aligns_with_its_first_report(world):
    """Ohne bekannten Zustand (noch nie gemeldet, Mosquitto ohne retained Werte) wartet das Angleichen auf die
    erste Live-Meldung des Thermostats."""
    dokumente_schreiben(world.paths, konfiguration(), bedienung(22.0))
    world.start_runtime()
    world.run()
    assert world.host._store.value == 22.0 and _thermostat_writes(world) == []
    world.z2m.report(THERMOSTAT, system_mode="heat", occupied_heating_setpoint=20.0, local_temperature=20.0)
    world.run()
    assert _thermostat_writes(world) == [22.0]


@pytest.mark.parametrize("erste", [None, "heat", "off"], ids=["keine Meldung", "an", "aus"])
def test_a_newer_operation_replaces_the_stored_target_at_start(world, erste):
    """Gespeichert 20, Bedienung 22,5 (kam waehrend des Neustarts, nicht dirty): sie gilt sofort im Soll-Speicher.
    Ohne bekannten Zustand des Thermostats geht sie erst mit seiner ersten Live-Meldung hinaus, wie das Angleichen;
    meldet es aus, erst mit "wieder an"."""
    dateien.schreiben_json(world.paths.room_target, {"value": 20.0, "source": "portal", "ts": "t"},
                           modus=dateien.LESBAR)
    dokumente_schreiben(world.paths, konfiguration(), bedienung(22.5))
    world.start_runtime()
    world.run()
    assert world.host._store.value == 22.5 and _thermostat_writes(world) == []
    if erste is None:
        return
    world.z2m.report(THERMOSTAT, system_mode=erste, occupied_heating_setpoint=20.0, local_temperature=20.0)
    world.run()
    assert _thermostat_writes(world) == ([22.5] if erste == "heat" else [])
    world.z2m.report(THERMOSTAT, system_mode="heat", occupied_heating_setpoint=20.0, local_temperature=20.0)
    world.advance(30)  # nach der Entprellung: das gemeldete 20 ist kein Wunsch
    assert _thermostat_writes(world) == [22.5] and world.host._store.value == 22.5


def test_a_raise_of_the_wish_at_start_triggers_the_boost(world):
    """Eine Erhoehung der Bedienung waehrend des Neustarts (20 -> 22,5) loest den Boost aus,
    nicht nur das Angleichen des Thermostats: der Stable-Target-Cache haelt noch 20, das Soll im Speicher ist 22,5."""
    world.z2m.report(THERMOSTAT, system_mode="heat", occupied_heating_setpoint=20.0, local_temperature=20.0)
    dokumente_schreiben(world.paths, konfiguration(), bedienung(20.0))
    world.start_runtime()
    world.run()
    assert world.host._store.value == 20.0 and not world.result.store.state.boost_active  # Gegenprobe: erster Start
    dokumente_schreiben(world.paths, konfiguration(), bedienung(22.5))
    _neustart(world)
    world.run()
    assert world.result.store.state.boost_active
    assert world.host._store.value == 22.5 and _thermostat_writes(world).count(22.5) == 1


def test_an_unexpected_error_reports_the_gateway_unhealthy_during_the_pause(world, monkeypatch):
    """Abschlussreview S6 I-2: in der 60-s-Pause nach einem unerwarteten Fehler ist das Lebenszeichen weg (der
    Compose-Health-Check scheitert) und /healthz meldet die Laufzeit "startet" (health_ok des Updaters lehnt ab). Sonst
    behielte der Updater ein Update, dessen Laufzeit gleich nach dem Start wirft. Echter Ablauf ueber
    runtime_main.laufen mit Geraete-Thread; nur der Worker wirft."""
    dokumente_schreiben(world.paths, konfiguration())
    world.start_runtime()
    world.connect()
    alive = world.paths.data / "alive"
    vorher = world.geraet.snapshot()
    assert alive.exists() and vorher["server_ok"] is True and vorher["laufzeit"] not in (None, "startet")

    def kaputt():
        raise RuntimeError("Testfehler")

    monkeypatch.setattr(world.result.worker, "run", kaputt)
    monkeypatch.setattr(runtime_main, "_starten", lambda *args: world.result)
    in_der_pause = []
    echt = prozess.laufen

    def laufen(*args, **kwargs):
        echt(*args, **kwargs, sleep=lambda s: in_der_pause.append((s, alive.exists(), world.geraet.snapshot())))

    monkeypatch.setattr(prozess, "laufen", laufen)
    runtime_main.laufen(world.paths, world.bus, None, world.geraet, world.device)
    [(sekunden, lebenszeichen, snapshot)] = in_der_pause
    assert sekunden == 60.0 and not lebenszeichen and snapshot["laufzeit"] == "startet"


def test_before_the_pause_a_missing_alive_file_is_no_error(world, monkeypatch):
    geraet = SimpleNamespace(zustand=SimpleNamespace(status={"status": "regelt"}))
    runtime_main.vor_der_pause(geraet)  # world setzt SHG_ALIVE_FILE, die Datei gibt es noch nicht
    assert geraet.zustand.status == {"status": "startet"}

    def unlink(self, missing_ok=False):
        raise PermissionError("read-only file system")

    monkeypatch.setattr("pathlib.Path.unlink", unlink)
    geraet.zustand.status = None
    runtime_main.vor_der_pause(geraet)
    assert geraet.zustand.status == {"status": "startet"}


# --- Fehler vor laufen ---


@pytest.fixture
def vor_dem_start(world, monkeypatch):
    """Umgebung von main() bis zum Start; die Pause wird aufgezeichnet. Das Lebenszeichen ist noch frisch wie aus einem
    vorigen Lauf im selben Container (in Compose leert der tmpfs auf /tmp es beim Neustart ohnehin; ohne tmpfs bliebe
    es)."""
    monkeypatch.setenv("SHG_HOST_DIR", str(world.host_dir))
    monkeypatch.setenv("SHG_DEVICE_API_URL", "https://accounts.example.test")
    monkeypatch.setattr(runtime_main, "DIAG_PORT", 0)  # Eine Konstante, kein Umgebungsschalter
    monkeypatch.setattr(runtime_main, "HEALTH_PORT", 0)
    pausen = []
    monkeypatch.setattr(prozess.time, "sleep", pausen.append)
    alive = world.paths.data / "alive"
    alive.touch()
    return SimpleNamespace(mirror=ZigbeeMirror(world.bus, world.clock), pausen=pausen, alive=alive)


def test_an_unwritable_data_dir_during_cleanup_pauses_unhealthy_and_exits(world, vor_dem_start, monkeypatch, caplog):
    """Die Markierung einer neuen Einrichtung (VERWERFEN) laesst sich im nicht beschreibbaren Datenordner nicht
    loeschen. Es endet nicht mit dem Traceback (Compose startete sonst schnell neu); stattdessen
    ERROR mit Fehlerart, Lebenszeichen weg (Compose-Health-Check und health_ok scheitern; /healthz antwortet ohne
    Diagnoseserver gar nicht), 60 s Pause, None (main endet mit Exit 0, Compose startet neu)."""
    dateien.schreiben_json(world.paths.aufraeumen, {"anlage": "test-tenant"}, modus=dateien.LESBAR)
    dokumente_schreiben(world.paths, konfiguration(anlage="neue-anlage"))
    echt = Path.unlink

    def unlink(self, missing_ok=False):
        if self == world.paths.aufraeumen:
            raise PermissionError(13, "geheim")
        return echt(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    assert vor_dem_start.alive.exists()
    with caplog.at_level("WARNING"):
        assert runtime_main.vorbereiten(world.paths, vor_dem_start.mirror) is None
    assert vor_dem_start.pausen == [60.0] and not vor_dem_start.alive.exists()
    assert world.paths.aufraeumen.exists()
    assert "PermissionError" in caplog.text and "geheim" not in caplog.text


def test_a_failing_document_store_during_cleanup_pauses_unhealthy_and_exits(world, vor_dem_start, monkeypatch,
                                                                             caplog):
    dateien.schreiben_json(world.paths.aufraeumen, {"anlage": "test-tenant"}, modus=dateien.LESBAR)

    def kaputt(*args, **kwargs):
        raise OSError(5, "geheim")

    monkeypatch.setattr(runtime_main, "DocumentStore", kaputt)
    with caplog.at_level("WARNING"):
        assert runtime_main.vorbereiten(world.paths, vor_dem_start.mirror) is None
    assert vor_dem_start.pausen == [60.0] and not vor_dem_start.alive.exists()
    assert world.paths.aufraeumen.exists()
    assert "OSError" in caplog.text and "geheim" not in caplog.text


def test_a_start_error_after_the_device_host_reports_the_runtime_starting(world, vor_dem_start, monkeypatch):
    """Scheitert der Start erst nach dem Geraete-Host (hier: Diagnoseport belegt), meldet /healthz die Laufzeit als
    "startet" (health_ok des Updaters lehnt ab), falls die Seite antwortet; das Lebenszeichen ist weg."""
    seiten = []

    def belegt(port, snapshot_fn, *args, **kwargs):
        seiten.append(snapshot_fn)
        raise OSError(98, "Address already in use")

    monkeypatch.setattr(runtime_main.diagnose, "start_server", belegt)
    assert runtime_main.vorbereiten(world.paths, vor_dem_start.mirror) is None
    [seite] = seiten
    assert vor_dem_start.pausen == [60.0] and not vor_dem_start.alive.exists()
    assert seite()["laufzeit"] == "startet"


def test_vorbereiten_returns_host_and_device_with_the_diagnosis_page_up(world, vor_dem_start, monkeypatch):
    """Ohne Fehler: Geraete-Host und Geraetekern verbunden, die Diagnoseseite antwortet, keine Pause, das Lebenszeichen
    bleibt (erst die Wache in laufen erneuert es)."""
    server = []
    echt = runtime_main.diagnose.start_server
    monkeypatch.setattr(runtime_main.diagnose, "start_server",
                        lambda *args, **kwargs: server.append(echt(*args, **kwargs)) or server[-1])
    try:
        geraet, device = runtime_main.vorbereiten(world.paths, vor_dem_start.mirror)
        assert geraet.device is device and device.host is geraet
        port = server[0].server_address[1]
        # Die Seite auf Port 80 verlangt die Anmeldung mit dem Passwort aus werk.json der Host-Kopie (GW 8, echtes scrypt)
        with pytest.raises(urllib.error.HTTPError) as ohne_sitzung:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5)
        assert ohne_sitzung.value.code == 401
        eintrag = anmeldung.passwort_eintrag("Etikett-Passwort", bytes(16))
        dateien.schreiben_json(world.host_dir / "werk.json", {
            "format": "smartheat-werk-1", "seriennummer": "SHG-0001", "hostname": "smartheat-0001",
            "passwort": eintrag, "bespielt": "2026-10-27", "hardware": "Raspberry Pi 5"}, modus=dateien.LESBAR)
        verbindung = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        verbindung.request("POST", "/anmelden", "passwort=Etikett-Passwort",
                           {"Content-Type": "application/x-www-form-urlencoded"})
        antwort = verbindung.getresponse()
        antwort.read()
        assert antwort.status == 303
        cookie = antwort.getheader("Set-Cookie", "").split(";")[0]
        verbindung.close()
        sitzung = {"Cookie": cookie}
        anfrage = urllib.request.Request(f"http://127.0.0.1:{port}/healthz", headers=sitzung)
        with urllib.request.urlopen(anfrage, timeout=5) as antwort:
            assert json.loads(antwort.read())["server_ok"] is False  # Geraete-Thread laeuft noch nicht
        seite = urllib.request.Request(f"http://127.0.0.1:{port}/", headers=sitzung)
        with urllib.request.urlopen(seite, timeout=5) as antwort:
            assert "SHG-0001" in antwort.read().decode()
        health_port = server[1].server_address[1]  # der zweite Server ist der Gesundheitsserver (nur /healthz)
        assert health_port != port
        with urllib.request.urlopen(f"http://127.0.0.1:{health_port}/healthz", timeout=5) as antwort:
            assert json.loads(antwort.read())["server_ok"] is False
        with pytest.raises(urllib.error.HTTPError) as fehler:
            urllib.request.urlopen(f"http://127.0.0.1:{health_port}/", timeout=5)
        assert fehler.value.code == 404
    finally:
        for s in server:
            s.shutdown()
            s.server_close()
    assert vor_dem_start.pausen == [] and vor_dem_start.alive.exists()


def test_the_health_server_accepts_the_host_names_of_the_environment_too(world, vor_dem_start, monkeypatch):
    """Der Docker-Test des Updaters fragt den Gesundheitsserver unter dem Dienstnamen (SHG_DIAG_HOSTNAMES=runtime)."""
    monkeypatch.setenv("SHG_DIAG_HOSTNAMES", "runtime")
    server = []
    echt = runtime_main.diagnose.start_server
    monkeypatch.setattr(runtime_main.diagnose, "start_server",
                        lambda *args, **kwargs: server.append(echt(*args, **kwargs)) or server[-1])
    try:
        runtime_main.vorbereiten(world.paths, vor_dem_start.mirror)
        port = server[1].server_address[1]
        for host, erwartet in (("runtime", 200), ("fremd.example", 403)):
            anfrage = urllib.request.Request(f"http://127.0.0.1:{port}/healthz", headers={"Host": host})
            try:
                with urllib.request.urlopen(anfrage, timeout=5) as antwort:
                    status = antwort.status
            except urllib.error.HTTPError as fehler:
                status = fehler.code
            assert status == erwartet, host
    finally:
        for s in server:
            s.shutdown()
            s.server_close()


def test_the_diagnosis_page_listens_on_diag_port_whatever_the_environment_says(world, vor_dem_start, monkeypatch):
    """Der Port ist `runtime_main.DIAG_PORT`; `SHG_DIAG_PORT` aus der Umgebung gilt nicht mehr."""
    monkeypatch.setenv("SHG_DIAG_PORT", "1")
    ports = []
    monkeypatch.setattr(runtime_main.diagnose, "start_server",
                        lambda port, *args, **kwargs: ports.append((port, kwargs.get("nur_health", False))))
    monkeypatch.setattr(runtime_main, "DIAG_PORT", 11)
    monkeypatch.setattr(runtime_main, "HEALTH_PORT", 22)
    runtime_main.vorbereiten(world.paths, vor_dem_start.mirror)
    assert ports == [(11, False), (22, True)]


def test_main_connects_to_the_local_bus_on_the_bus_port_whatever_the_environment_says(monkeypatch):
    """Der Port des lokalen Busses ist `bus.BUS_PORT` (1883, Mosquitto in der Compose-Datei); `SHG_BUS_PORT` aus
    der Umgebung gilt nicht mehr."""
    monkeypatch.setenv("SHG_BUS_PORT", "1")
    monkeypatch.delenv("SHG_BUS_HOST", raising=False)
    monkeypatch.delenv("SHG_BUS_CREDENTIALS", raising=False)
    monkeypatch.setattr(runtime_main.signal, "signal", lambda *args: None)
    seen = []

    def local_bus(host, port, *args, **kwargs):
        seen.append((host, port))
        raise _Stopp

    monkeypatch.setattr(runtime_main, "LocalBus", local_bus)
    with pytest.raises(_Stopp):
        runtime_main.main()
    assert seen == [("mosquitto", BUS_PORT)] and BUS_PORT == 1883


class _Stopp(Exception):
    pass


def test_vorbereiten_reicht_die_ausstehende_markierung_an_den_geraete_host(world, vor_dem_start, monkeypatch):
    """Das Aufraeumen liest die Markierung einmal; steht das Zuruecksetzen aus, bekommt sie der
    Geraete-Host als start_markierung (die Laufzeit liest die Datei nicht erneut); sonst bleibt sie leer."""
    monkeypatch.setattr(runtime_main.diagnose, "start_server", lambda *args, **kwargs: None)
    geraet, _ = runtime_main.vorbereiten(world.paths, vor_dem_start.mirror)
    assert geraet.start_markierung is None
    aufraeumen.markieren(world.paths.aufraeumen, konfiguration())
    geraet, _ = runtime_main.vorbereiten(world.paths, vor_dem_start.mirror)
    assert geraet.start_markierung == aufraeumen.Markierung("test-tenant", konfiguration(), False)
    assert world.paths.aufraeumen.exists()  # nichts geloescht, das Zuruecksetzen steht aus


def test_sigterm_ends_with_exit_0():
    with pytest.raises(SystemExit) as stop:
        runtime_main._terminate(15, None)
    assert stop.value.code == 0


def test_an_operation_without_a_valid_target_does_not_align_the_thermostat_at_start(world):
    """Wie der HA-Host gleicht das Gateway nur mit einem gueltigen raum_soll an; sonst gilt
    der Standard, und das Thermostat bleibt unberuehrt."""
    dokumente_schreiben(world.paths, konfiguration(), {**bedienung(), "raum_soll": 30.0})
    world.start_runtime()
    world.run()
    assert world.host._store.value == 20.0 and _thermostat_writes(world) == []
