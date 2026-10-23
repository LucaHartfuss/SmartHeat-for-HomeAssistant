"""Spec 5b 5.1/5.3 (Plan D2 Task 3): der Gateway als Host des Geraetekerns - Faehigkeiten, Uhr, Status und Raum im
Prozess, Meldungen, Neustart bei neuer Konfiguration, Aufraeumen nach dem Entfernen, Software-Bruecke, LED-Datei."""
import errno
import json
import logging
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from configs import dokumente_schreiben, konfiguration
from fakes import FakeBus
from world import GatewayWorld

from smartheat_core import dateien
from smartheat_device import nachrichten, wire
from smartheat_device.documents import DocumentStore
from smartheat_device.geraet_host_basis import EV_RELOAD
from smartheat_device.host import Anzeige
from smartheat_gateway import geraet_host as gh
from smartheat_gateway import runtime_main
from smartheat_gateway.drivers import registry
from smartheat_gateway.paths import Paths
from smartheat_gateway.triggers import EV_TARGET_INPUT
from smartheat_gateway.version import GATEWAY_VERSION
from smartheat_gateway.zigbee import ZigbeeMirror
from smartheat_runtime import dokument_config
from smartheat_runtime.runtime import Runtime
from smartheat_runtime.soll_speicher import SOURCE_PORTAL
from smartheat_runtime.worker import RegulationWorker

WALL = 1_800_000_000.0
LEER = {**konfiguration(), "anlage": None, "profil": None, "hebelsatz": None, "verteilsystem": None,
        "bindung": None, "raumfuehler": [], "tagestick": None, "abo": None}


class FakeDevice:
    def __init__(self) -> None:
        self.konf: dict | None = konfiguration()
        self.bed: dict | None = None
        self.dirty = False
        self.meldungen, self.ergebnisse, self.wuensche, self.aus = [], [], [], []
        self.ergebnis_ok = True
        self.identity = SimpleNamespace(device_id="shg-aaaaaaaaaaaaaaaa", claim_code="ABCDEFGHIJKL")
        self.documents = SimpleNamespace(
            aktuell=lambda art: SimpleNamespace(dirty=self.dirty) if self.bed is not None else None)

    def konfiguration(self):
        return self.konf

    def bedienung(self):
        return self.bed

    def abo_status(self):
        return dokument_config.abo_status(self.konf)

    def wunsch(self, wert, *, herkunft, durch=None):
        self.wuensche.append((wert, herkunft, durch))
        return max(15.0, min(25.0, wert))

    def soll_quelle_aus(self, aus, wirksam=None):
        self.aus.append((aus, wirksam))

    def meldung(self, key, text, *, offen, kritisch=True):
        self.meldungen.append((key, text, offen, kritisch))

    def ergebnis_software(self, version, ok, grund=None):
        self.ergebnisse.append((version, ok, grund))
        return self.ergebnis_ok


class Welt:
    def __init__(self, tmp_path, clock) -> None:
        self.paths = Paths(tmp_path / "data")
        self.host_dir = tmp_path / "host"
        self.clock = clock
        self.wall = {"t": WALL}
        self.mirror = ZigbeeMirror(FakeBus(), clock)
        self.host = gh.GatewayDeviceHost(self.paths, self.mirror, host_dir=self.host_dir, clock=clock,
                                         wall=lambda: self.wall["t"])
        self.device = FakeDevice()
        self.host.verbinden(self.device)
        self.worker = RegulationWorker(clock=clock)
        self.events: list = []
        self.worker.register(EV_RELOAD, self.events.append)
        self.worker.register(EV_TARGET_INPUT, self.events.append)

    def kinds(self):
        self.worker.run_pending()
        return [event.kind for event in self.events]


@pytest.fixture
def welt(tmp_path, clock):
    return Welt(tmp_path, clock)


def _anzeige(zustand="regelt", **changes):
    values = dict(zustand=zustand, server_ok=True, device_state="uebernommen", eingerichtet=True, hinweise=(),
                  meldungen=(), konfiguration_abgelehnt=None)
    values.update(changes)
    return Anzeige(**values)


def test_capabilities_and_version(welt):
    assert (welt.host.host, welt.host.version) == ("gateway", GATEWAY_VERSION)
    assert welt.host.faehigkeiten() == {"drivers": ["simulation", "vicare_cloud"], "zigbee": True, "updater": True}


def test_capabilities_are_built_against_the_contract(welt, monkeypatch):
    monkeypatch.setattr(wire, "CAPABILITY_OPTIONAL", ("programm",))  # updater kennt der Vertrag dann nicht
    with pytest.raises(nachrichten.Vertragsfehler, match="faehigkeiten"):
        welt.host.faehigkeiten()


def test_clock_sync_comes_from_the_host_status(welt):
    assert welt.host.uhr_synchron() is None
    status = {"netz": True, "dns": True, "zeit_synchron": True, "ts": datetime.fromtimestamp(WALL, UTC).isoformat()}
    dateien.schreiben_json(welt.host_dir / "status.json", status, modus=dateien.LESBAR)
    assert welt.host.uhr_synchron() is True
    dateien.schreiben_json(welt.host_dir / "status.json", {**status, "zeit_synchron": False}, modus=dateien.LESBAR)
    assert welt.host.uhr_synchron() is False
    welt.wall["t"] = WALL + gh.UHR_MAX_ALTER_SECONDS + 1
    assert welt.host.uhr_synchron() is None  # veraltet
    dateien.schreiben_json(welt.host_dir / "status.json", {**status, "ts": "gestern"}, modus=dateien.LESBAR)
    assert welt.host.uhr_synchron() is None


def test_status_and_room_stay_in_the_process(welt):
    welt.host.status_sink.publish({"schema": 2, "status": "regelt"})
    welt.host.zustand.raum = {"ist": 20.0, "soll": 21.0, "soll_quelle": "portal", "ts": "t"}
    assert welt.host.runtime_status() == {"schema": 2, "status": "regelt"}
    assert welt.host.raum()["soll"] == 21.0


def test_runtime_messages_become_notifications(welt):
    sink = welt.host.notify_sink
    sink.show("notbetrieb", "Notbetrieb aktiv")
    sink.push("notbetrieb", "Notbetrieb aktiv")
    sink.push("batterie:0x1", "Batterie schwach")
    sink.withdraw("notbetrieb")
    assert welt.device.meldungen == [
        ("notbetrieb", "Notbetrieb aktiv", True, True), ("notbetrieb", "Notbetrieb aktiv", True, True),
        ("batterie:0x1", "Batterie schwach", False, False), ("notbetrieb", "Notbetrieb aktiv", False, True)]


def test_a_runtime_relevant_configuration_requests_a_restart(welt):
    welt.host.worker_binden(welt.worker)
    welt.host.konfiguration_uebernommen(konfiguration(), konfiguration(software={"version": "0.6.1"}))
    assert welt.kinds() == []
    welt.host.konfiguration_uebernommen(konfiguration(), konfiguration(abo="inaktiv"))
    assert welt.kinds() == [EV_RELOAD]


def test_remembered_originals_are_handed_to_the_device_as_a_copy(welt):
    assert welt.host.ursprungswerte() == {}
    werte = {"heat_limit": 16.0}
    welt.host.ursprungswerte_merken(werte)
    werte["mode_select"] = "x"  # die Laufzeit aendert ihr Woerterbuch weiter
    gelesen = welt.host.ursprungswerte()
    assert gelesen == {"heat_limit": 16.0}
    gelesen["x"] = 1.0
    assert welt.host.ursprungswerte() == {"heat_limit": 16.0}


def test_a_restart_requested_before_the_worker_exists_is_posted_on_binding(welt):
    welt.host.neustart_anfordern()
    welt.host.worker_binden(welt.worker)
    assert welt.kinds() == [EV_RELOAD]


def test_an_empty_configuration_after_a_setup_marks_the_cleanup(welt):
    """Die Markierung schreibt konfiguration_vorbereiten (vor dem Speichern, Nr. 38), den Neustart fordert
    konfiguration_uebernommen an."""
    welt.host.konfiguration_vorbereiten(konfiguration(), LEER)
    assert json.loads(welt.paths.aufraeumen.read_text()) == {
        "anlage": "test-tenant", "konfiguration": konfiguration(), "zurueckgesetzt": False}
    assert stat.S_IMODE(welt.paths.aufraeumen.stat().st_mode) == 0o600
    welt.host.worker_binden(welt.worker)
    assert welt.kinds() == []
    welt.host.konfiguration_uebernommen(konfiguration(), LEER)
    assert welt.kinds() == [EV_RELOAD]


def test_the_first_empty_configuration_marks_nothing(welt):
    welt.host.konfiguration_vorbereiten(None, LEER)
    welt.host.konfiguration_uebernommen(None, LEER)
    assert not welt.paths.aufraeumen.exists()


def test_vergessen_removes_the_plant_state_but_keeps_device_and_simulation(welt):
    for path in (welt.paths.backup, welt.paths.failsafe, welt.paths.abgemeldet, welt.paths.room_target,
                 welt.paths.inventur, welt.paths.driver_secrets_dir / "vicare.json",
                 welt.paths.device_dir / "konfiguration.json", welt.paths.sim_dir / "plant.json"):
        dateien.schreiben_json(path, {"x": 1}, modus=dateien.LESBAR)
    assert gh.vergessen(welt.paths) is True
    assert not welt.paths.runtime_dir.exists() and not welt.paths.inventur.exists()
    assert not (welt.paths.driver_secrets_dir / "vicare.json").exists()
    assert (welt.paths.device_dir / "konfiguration.json").exists() and (welt.paths.sim_dir / "plant.json").exists()


def _dokument(inhalt: dict, version: int) -> dict:
    return {"schema": 1, "version": version, **inhalt}


@pytest.mark.parametrize("anders", [{"verteilsystem": "Fussbodenheizung"}, {"hebelsatz": "vaillant_vrc720"}])
def test_vergessen_keeps_the_lever_set_lock(welt, anders):
    """G-1, Angriff (ck F-client-kern-47): das Aufraeumen nach "leer ohne sign_off" (runtime_main._vergessen) loescht
    den anlagengebundenen Zustand, aber nicht device/sicherheitssatz.json; ein neuer Dokumentspeicher lehnt ein anderes
    Paar weiter ab, dasselbe Paar nimmt er an."""
    store = DocumentStore(welt.paths.device_dir, host="gateway", software_version=GATEWAY_VERSION)
    assert store.empfangen("konfiguration", _dokument(konfiguration(), 1)).uebernommen
    assert store.empfangen("konfiguration", _dokument(LEER, 2)).uebernommen
    dateien.schreiben_json(welt.paths.backup, {"x": 1}, modus=dateien.LESBAR)
    assert runtime_main._vergessen(welt.paths)
    assert not welt.paths.runtime_dir.exists()  # das Aufraeumen lief
    store = DocumentStore(welt.paths.device_dir, host="gateway", software_version=GATEWAY_VERSION)
    ergebnis = store.empfangen("konfiguration", _dokument(konfiguration(**anders), 3))
    assert (ergebnis.ok, ergebnis.grund) == (False, "hebelsatz_eingerastet")
    assert store.empfangen("konfiguration", _dokument(konfiguration(), 4)).uebernommen


def _unlink_scheitert_fuer(monkeypatch, pfad, fehler):
    """Fehlerinjektion: Path.unlink scheitert fuer pfad mit fehler (per monkeypatch, nie per chmod)."""
    echt = Path.unlink

    def unlink(self, missing_ok=False):
        if self == pfad:
            raise OSError(fehler, os.strerror(fehler))
        echt(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    return echt


def _entfernt_vorbereiten(paths) -> bytes:
    """Eingerichtet, dann leer (Rastung liegt), Zustand der Anlage, Markierung wie nach "leer ohne sign_off" nach dem
    Zuruecksetzen; gibt den Inhalt der Rastung zurueck."""
    store = DocumentStore(paths.device_dir, host="gateway", software_version=GATEWAY_VERSION)
    assert store.empfangen("konfiguration", _dokument(konfiguration(), 1)).uebernommen
    assert store.empfangen("konfiguration", _dokument(LEER, 2)).uebernommen
    for path in (paths.backup, paths.failsafe, paths.entitlement, paths.room_target, paths.abgemeldet,
                 paths.inventur, paths.driver_secrets_dir / "vicare.json"):
        dateien.schreiben_json(path, {"x": 1}, modus=dateien.LESBAR)
    dateien.schreiben_json(paths.aufraeumen, {"anlage": "test-tenant"}, modus=dateien.LESBAR)
    return paths.rastung.read_bytes()


def _alles_weg(paths) -> bool:
    return (not paths.runtime_dir.exists() and not paths.inventur.exists()
            and not (paths.driver_secrets_dir / "vicare.json").exists() and not paths.aufraeumen.exists())


@pytest.mark.parametrize("fehler, art", [(errno.EACCES, "PermissionError"), (errno.EIO, "OSError")])
def test_a_state_file_that_cannot_be_deleted_keeps_the_marker_and_the_binding_and_the_start_goes_on(
        world, monkeypatch, caplog, fehler, art):
    """Wie im Add-on (Task 11): scheitert das Loeschen einer Zustandsdatei, startet das Gateway trotzdem; die Datei,
    die Markierung und runtime/backup.json (der Bindungsanker, zuletzt und nur nach allem anderen geloescht) bleiben,
    der uebrige Zustand ist weg, im Log Datei und Fehlerart, keine Erfolgsmeldung. Der naechste Start loescht den Rest
    samt Markierung. Die Rastung bleibt in jedem Fall (G-1)."""
    rastung = _entfernt_vorbereiten(world.paths)
    echt = _unlink_scheitert_fuer(monkeypatch, world.paths.failsafe, fehler)
    with caplog.at_level(logging.INFO):
        world.start_runtime()  # wirft nicht
    assert world.result is not None
    assert world.paths.failsafe.exists() and world.paths.backup.exists() and world.paths.aufraeumen.exists()
    assert not world.paths.entitlement.exists() and not world.paths.room_target.exists()
    assert not world.paths.abgemeldet.exists() and not world.paths.inventur.exists()
    assert not (world.paths.driver_secrets_dir / "vicare.json").exists()
    assert f"{world.paths.failsafe.name} nicht geloescht ({art})" in caplog.text
    assert "Anlagengebundener Zustand geloescht" not in caplog.text
    assert world.paths.rastung.read_bytes() == rastung

    monkeypatch.setattr(Path, "unlink", echt)
    world.start_runtime()
    assert _alles_weg(world.paths)
    assert world.paths.rastung.read_bytes() == rastung


@pytest.mark.parametrize("pfad", ["inventur", "zugang"])
def test_leftover_inventory_or_driver_login_keeps_the_marker_and_the_binding(welt, monkeypatch, caplog, pfad):
    """Inventur-Proben und Treiber-Anmeldung der alten Anlage zaehlen mit: bleiben sie liegen, meldet vergessen keinen
    Erfolg, backup.json und die Markierung bleiben; ohne Fehler raeumt der naechste Start alles weg."""
    rastung = _entfernt_vorbereiten(welt.paths)
    ziel = welt.paths.inventur if pfad == "inventur" else welt.paths.driver_secrets_dir / "vicare.json"
    echt = _unlink_scheitert_fuer(monkeypatch, ziel, errno.EIO)
    with caplog.at_level(logging.INFO):
        assert runtime_main._vergessen(welt.paths) is False
        runtime_main.aufraeumen(welt.paths)
    assert ziel.exists() and welt.paths.backup.exists() and welt.paths.aufraeumen.exists()
    assert not welt.paths.failsafe.exists() and not welt.paths.entitlement.exists()
    assert not welt.paths.room_target.exists() and not welt.paths.abgemeldet.exists()
    assert "Anlagengebundener Zustand geloescht" not in caplog.text
    if pfad == "inventur":
        # Zwei Aufrufe (vergessen, aufraeumen), je Aufruf genau eine Zeile zur Datei: bisher ERROR und WARNING
        fehler = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert [r.levelno for r in fehler] == [logging.ERROR] * 2, [r.getMessage() for r in fehler]
        assert all(ziel.name in r.getMessage() for r in fehler)
    monkeypatch.setattr(Path, "unlink", echt)
    with caplog.at_level(logging.INFO):
        runtime_main.aufraeumen(welt.paths)
    assert _alles_weg(welt.paths) and welt.paths.rastung.read_bytes() == rastung
    assert "Anlagengebundener Zustand geloescht" in caplog.text  # Gegenprobe: bei vollem Erfolg steht die Zeile da


def test_a_binding_that_cannot_be_deleted_keeps_the_marker(welt, monkeypatch, caplog):
    """Scheitert nur das Loeschen von backup.json, ist der uebrige Zustand weg und die Markierung bleibt."""
    rastung = _entfernt_vorbereiten(welt.paths)
    _unlink_scheitert_fuer(monkeypatch, welt.paths.backup, errno.EIO)
    runtime_main.aufraeumen(welt.paths)
    assert welt.paths.backup.exists() and welt.paths.aufraeumen.exists()
    assert not welt.paths.failsafe.exists() and not welt.paths.inventur.exists()
    assert not (welt.paths.driver_secrets_dir / "vicare.json").exists()
    assert f"{welt.paths.backup.name} nicht geloescht (OSError)" in caplog.text
    assert welt.paths.rastung.read_bytes() == rastung


def test_a_runtime_folder_that_cannot_be_removed_keeps_the_marker(welt, monkeypatch, caplog):
    """Laesst sich der Laufzeit-Ordner zuletzt nicht entfernen (EACCES), bleibt die Markierung: der naechste Start
    versucht es erneut, statt den Ordner fuer immer liegen zu lassen."""
    _entfernt_vorbereiten(welt.paths)
    echt = gh.shutil.rmtree

    def rmtree(path, *args, **kwargs):
        if path == welt.paths.runtime_dir:
            raise PermissionError(errno.EACCES, "verweigert")
        echt(path, *args, **kwargs)

    monkeypatch.setattr(gh.shutil, "rmtree", rmtree)
    runtime_main.aufraeumen(welt.paths)
    assert welt.paths.runtime_dir.exists() and welt.paths.aufraeumen.exists()
    assert f"{welt.paths.runtime_dir.name} nicht geloescht (PermissionError)" in caplog.text
    monkeypatch.setattr(gh.shutil, "rmtree", echt)
    runtime_main.aufraeumen(welt.paths)
    assert _alles_weg(welt.paths)


def test_an_unreadable_runtime_folder_keeps_the_marker_and_the_binding(welt, monkeypatch, caplog):
    """Ist der Laufzeit-Ordner nicht lesbar (EIO beim Auflisten), wird dort nichts geloescht und kein Erfolg
    gemeldet."""
    _entfernt_vorbereiten(welt.paths)
    echt = Path.iterdir

    def iterdir(self):
        if self == welt.paths.runtime_dir:
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        return echt(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    runtime_main.aufraeumen(welt.paths)
    assert welt.paths.failsafe.exists() and welt.paths.backup.exists() and welt.paths.aufraeumen.exists()
    assert f"{welt.paths.runtime_dir.name} nicht geloescht (OSError)" in caplog.text
    monkeypatch.setattr(Path, "iterdir", echt)
    runtime_main.aufraeumen(welt.paths)
    assert _alles_weg(welt.paths)


@pytest.fixture
def world(data_dir, monkeypatch, clock):
    world = GatewayWorld(data_dir, monkeypatch, clock)
    yield world
    world.close()


def test_the_sign_off_ends_only_with_a_new_version_of_the_same_plant(world):
    """5b-D2-7, Review Focus 2, ganzer Weg (Geraetekern, Dokumentspeicher, Host): abgemeldet bleibt bei erneuter
    Zustellung derselben Version (kein Neustart); eine neue Version mit gleichem Inhalt hebt die Markierung auf, und der
    Neustart regelt wieder."""
    dokumente_schreiben(world.paths, konfiguration(), version=1)
    dokument_config.abmelden(world.paths.abgemeldet, "test-tenant")
    world.start_runtime()
    world.connect()
    assert world.status()["status"] == "abgemeldet"

    world.konfiguration_senden(konfiguration(), 1)  # erneute Zustellung nach dem Neuverbinden
    assert world.restarts == 1 and world.status()["status"] == "abgemeldet"
    assert json.loads(world.paths.abgemeldet.read_text()) == {"anlage": "test-tenant"}

    world.konfiguration_senden(konfiguration(), 2)
    assert not world.paths.abgemeldet.exists()
    assert world.restarts == 2 and isinstance(world.result, Runtime)
    world.connect()
    assert world.status()["status"] == "regelt" and world.status()["tenant_id"] == "test-tenant"


def test_an_operation_goes_to_the_worker_as_target_input(welt):
    welt.host.worker_binden(welt.worker)
    welt.host.bedienung_uebernommen({"modus": "manuell", "raum_soll": 22.5})
    welt.worker.run_pending()
    (event,) = welt.events
    assert (event.kind, event.data) == (EV_TARGET_INPUT, {"value": 22.5, "source": SOURCE_PORTAL})


def test_an_operation_before_the_worker_exists_is_posted_on_binding(welt):
    # main(): der Geraete-Thread laeuft vor dem Worker; eine Bedienung gleich nach hello geht nicht verloren (Spec 3.3)
    welt.host.bedienung_uebernommen({"modus": "manuell", "raum_soll": 21.0})
    welt.host.bedienung_uebernommen({"modus": "manuell", "raum_soll": 22.5})
    welt.host.worker_binden(welt.worker)
    welt.worker.run_pending()
    (event,) = welt.events  # nur die letzte
    assert (event.kind, event.data) == (EV_TARGET_INPUT, {"value": 22.5, "source": SOURCE_PORTAL})


def test_operation_and_abo_for_the_runtime_host(welt):
    assert welt.host.bedienung() == (None, False)
    welt.device.bed, welt.device.dirty = {"raum_soll": 21.0}, True
    assert welt.host.bedienung() == ({"raum_soll": 21.0}, True)
    assert welt.host.abo_status() == "active" and welt.host.konfiguration()["anlage"] == "test-tenant"


def test_the_software_target_is_written_for_the_updater(welt):
    software = {"version": "0.6.1", "manifest_url": "https://example.test/m.json", "manifest_sha256": "a" * 64,
                "signature": "sig"}
    welt.host.software_soll(software)
    assert json.loads(welt.paths.software_soll.read_text()) == software
    assert oct(welt.paths.software_soll.stat().st_mode & 0o777) == "0o644"  # der Updater (root) liest
    welt.host.software_soll(None)
    assert json.loads(welt.paths.software_soll.read_text()) is None


def test_software_results_are_sent_in_order_and_acknowledged(welt, clock):
    dateien.schreiben_json(welt.paths.software_meldungen, {"meldungen": [
        {"seq": 2, "version": "0.6.2", "result": "rollback", "reason": "ungesund"},
        {"seq": 1, "version": "0.6.1", "result": "ok", "reason": ""}]}, modus=dateien.LESBAR)
    welt.host.anzeigen(_anzeige())
    assert welt.device.ergebnisse == [("0.6.1", True, None), ("0.6.2", False, "ungesund")]
    assert json.loads(welt.paths.software_quittung.read_text()) == {"seq": 2}
    clock.advance(gh.SOFTWARE_SECONDS)
    welt.host.anzeigen(_anzeige())
    assert len(welt.device.ergebnisse) == 2  # quittiert: nichts doppelt


def test_unsent_software_results_wait_for_the_link(welt, clock):
    dateien.schreiben_json(welt.paths.software_meldungen, {"meldungen": [
        {"seq": 1, "version": "0.6.1", "result": "rollback", "reason": "server_unerreichbar"}]}, modus=dateien.LESBAR)
    welt.device.ergebnis_ok = False
    welt.host.anzeigen(_anzeige())
    assert not welt.paths.software_quittung.exists()
    welt.device.ergebnis_ok = True
    welt.host.anzeigen(_anzeige())  # binnen SOFTWARE_SECONDS: noch nicht erneut
    assert len(welt.device.ergebnisse) == 1
    clock.advance(gh.SOFTWARE_SECONDS)
    welt.host.anzeigen(_anzeige())
    assert len(welt.device.ergebnisse) == 2 and json.loads(welt.paths.software_quittung.read_text()) == {"seq": 1}


def test_software_results_go_up_once_per_seq_across_a_runtime_restart(welt, clock):
    meldungen = [{"seq": 1, "version": "0.6.1", "result": "ok", "reason": ""},
                 {"seq": 2, "version": "0.6.2", "result": "rollback", "reason": "ungesund"}]
    dateien.schreiben_json(welt.paths.software_meldungen, {"meldungen": meldungen}, modus=dateien.LESBAR)
    welt.host.anzeigen(_anzeige())
    assert [e[0] for e in welt.device.ergebnisse] == ["0.6.1", "0.6.2"]
    meldungen.append({"seq": 3, "version": "0.6.3", "result": "ok", "reason": ""})
    dateien.schreiben_json(welt.paths.software_meldungen, {"meldungen": meldungen}, modus=dateien.LESBAR)
    welt.device.ergebnis_ok = False  # Link weg: seq 3 bleibt offen
    clock.advance(gh.SOFTWARE_SECONDS)
    welt.host.anzeigen(_anzeige())
    # Neustart der Laufzeit: neuer Host und neues Geraet auf denselben Dateien
    neu = gh.GatewayDeviceHost(welt.paths, welt.mirror, host_dir=welt.host_dir, clock=clock,
                               wall=lambda: welt.wall["t"])
    geraet = FakeDevice()
    neu.verbinden(geraet)
    neu.anzeigen(_anzeige())
    clock.advance(gh.SOFTWARE_SECONDS)
    neu.anzeigen(_anzeige())
    assert geraet.ergebnisse == [("0.6.3", True, None)]
    assert json.loads(welt.paths.software_quittung.read_text()) == {"seq": 3}


def test_the_led_file_follows_the_state(welt, clock):
    welt.host.anzeigen(_anzeige("gesperrt"))
    first = json.loads(welt.paths.agent_state.read_text())
    assert first["zustand"] == "gesperrt"
    mtime = os.stat(welt.paths.agent_state).st_mtime_ns
    welt.host.anzeigen(_anzeige("gesperrt"))
    assert os.stat(welt.paths.agent_state).st_mtime_ns == mtime
    welt.host.anzeigen(_anzeige("regelt"))
    assert json.loads(welt.paths.agent_state.read_text())["zustand"] == "regelt"


def test_snapshot_has_no_claim_code_and_only_known_host_fields(welt):
    dateien.schreiben_json(welt.host_dir / "status.json", {"netz": True, "dns": False, "zeit_synchron": True, "geheim": "x"}, modus=dateien.LESBAR)
    welt.host.anzeigen(_anzeige(hinweise=({"key": "k", "stufe": "info", "text": "t"},)))
    snap = welt.host.snapshot()
    assert snap["geraet"] == "shg-aaaaaaaaaaaaaaaa" and snap["host"] == {"netz": True, "dns": False,
                                                                        "zeit_synchron": True}
    assert "ABCDEFGHIJKL" not in json.dumps(snap) and snap["hinweise"] == [{"key": "k", "stufe": "info", "text": "t"}]


def test_wish_and_setpoint_source_go_to_the_device(welt):
    assert welt.host.wunsch(14.0) == 15.0 and welt.device.wuensche == [(14.0, "thermostat", None)]
    welt.host.soll_quelle_aus(True, 21.0)
    assert welt.device.aus == [(True, 21.0)]


def test_the_driver_check_and_commands(welt, tmp_path):
    assert welt.host.treiber_pruefen({"treiber": {"id": "erfunden", "parameter": {}}})
    from smartheat_device.commands import CommandRegister

    register = CommandRegister(tmp_path / "reg", lambda body: True)
    welt.host.befehle_eintragen(register)
    assert {"sign_off", "inventory", "diagnostics"} <= register.kinds


SIM_BINDUNG = {"treiber": {"id": "simulation", "parameter": {"lever_set": "viessmann_vicare", "heizkreis": 0}}}
FUEHLER, THERMO, FERN = "0x00124b0000000001", "0x00124b0000000002", "0x00124b0000000099"


def _z2m_dazu(welt):
    from fake_z2m import FakeZigbee2Mqtt

    bus = FakeBus()
    welt.mirror = ZigbeeMirror(bus, welt.clock)
    welt.mirror.start()
    welt.host.mirror = welt.mirror
    z2m = FakeZigbee2Mqtt(bus)
    z2m.add_sensor(FUEHLER)
    z2m.add_thermostat(THERMO)
    z2m.add_ohne_definition(FERN)
    return z2m


def test_a_bound_thermostat_that_the_mirror_knows_as_a_sensor_is_refused(welt, caplog):
    _z2m_dazu(welt)
    with caplog.at_level("DEBUG"):
        assert welt.host.treiber_pruefen({**SIM_BINDUNG, "thermostat": FUEHLER}) == {
            "feld": "thermostat", "regel": "nicht_erlaubt"}
    assert FUEHLER not in caplog.text


def test_a_bound_thermostat_is_accepted_when_it_is_a_thermostat_unknown_undefined_or_not_bound(welt):
    _z2m_dazu(welt)
    assert welt.host.treiber_pruefen({**SIM_BINDUNG, "thermostat": THERMO}) is None
    assert welt.host.treiber_pruefen({**SIM_BINDUNG, "thermostat": "0x00124b00000000ee"}) is None  # unbekannt
    assert welt.host.treiber_pruefen({**SIM_BINDUNG, "thermostat": FERN}) is None  # kein definiertes Geraet
    assert welt.host.treiber_pruefen({**SIM_BINDUNG, "thermostat": None}) is None
    assert welt.host.treiber_pruefen(SIM_BINDUNG) is None


def test_the_thermostat_check_comes_before_the_driver_check(welt):
    _z2m_dazu(welt)
    kaputt = {"treiber": {"id": "erfunden", "parameter": {}}, "thermostat": FUEHLER}
    assert welt.host.treiber_pruefen(kaputt) == {"feld": "thermostat", "regel": "nicht_erlaubt"}
    assert welt.host.treiber_pruefen({**kaputt, "thermostat": THERMO}) == {"feld": "id", "regel": "unbekannt"}


def test_a_sensor_bound_as_thermostat_is_refused_by_the_config_check_as_rolle_unzulaessig(welt):
    from smartheat_core import config_check

    _z2m_dazu(welt)
    neu = konfiguration(bindung={**SIM_BINDUNG, "thermostat": FUEHLER})
    ablehnung = config_check.pruefen(neu, None, host=config_check.HOST_GATEWAY,
                                     treiber_pruefen=welt.host.treiber_pruefen)
    assert ablehnung is not None and ablehnung.grund == config_check.ROLLE_UNZULAESSIG


def test_an_unexpected_driver_error_becomes_a_rejection(welt, caplog, monkeypatch):
    # Ein Fehler, den config.treiber_pruefen nicht kennt (hier ein OverflowError aus dem Treiber): Ablehnung statt keiner
    # Antwort; im Log nur der Typname, nie der Text oder die Parameter (Regel 6).
    class Kaputt:
        IDENTITY_PARAMETERS = ()

        def __init__(self, parameter, *args, **kwargs):
            raise OverflowError(f"geheim: {parameter}")

    monkeypatch.setitem(registry.DRIVERS, "simulation", Kaputt)
    bindung = {"treiber": {"id": "simulation", "parameter": {"lever_set": "viessmann_vicare", "token": "geheim-wert-1"}}}
    details = welt.host.treiber_pruefen(bindung)
    assert details == {"feld": "parameter", "regel": "ungueltig"}
    assert "OverflowError" in caplog.text and "geheim" not in caplog.text


def test_a_failing_led_write_does_not_stop_the_software_results(welt):
    (welt.paths.agent_state / "belegt").mkdir(parents=True)  # Ziel ist ein Ordner: das Schreiben scheitert
    dateien.schreiben_json(welt.paths.software_meldungen, {"meldungen": [
        {"seq": 1, "version": "0.6.1", "result": "ok", "reason": ""}]}, modus=dateien.LESBAR)
    welt.host.anzeigen(_anzeige())
    assert welt.device.ergebnisse == [("0.6.1", True, None)]
    assert welt.host.letzte_anzeige is not None


def test_a_persistently_failing_led_write_is_logged_once_per_error_type_and_on_recovery(welt, caplog, monkeypatch):
    """Die Anzeige kommt jeden Takt: ein dauerhaft scheiternder Schreibversuch loggt nur beim Wechsel der Fehlerart
    einen ERROR und bei der Erholung eine INFO (nur Typnamen, Regel 6)."""
    fehler: list[Exception] = [OSError("platte voll")]

    def update(zustand):
        if fehler:
            raise fehler[0]

    monkeypatch.setattr(welt.host._datei, "update", update)
    with caplog.at_level("INFO", logger="smartheat_gateway.geraet_host"):
        for _ in range(3):
            welt.host.anzeigen(_anzeige())
        fehler[0] = PermissionError("nur lesbar")
        for _ in range(2):
            welt.host.anzeigen(_anzeige())
        fehler.clear()
        for _ in range(2):
            welt.host.anzeigen(_anzeige())
        fehler.append(OSError("wieder"))
        welt.host.anzeigen(_anzeige())
    led = [(r.levelname, r.getMessage()) for r in caplog.records if "LED-Datei" in r.getMessage()]
    assert led == [
        ("ERROR", "LED-Datei nicht geschrieben (OSError)"),
        ("ERROR", "LED-Datei nicht geschrieben (PermissionError)"),
        ("INFO", "LED-Datei wieder geschrieben"),
        ("ERROR", "LED-Datei nicht geschrieben (OSError)"),
    ]
    assert "platte voll" not in caplog.text


def test_an_operation_in_the_idle_state_is_not_posted_to_the_worker(welt, caplog):
    """Ruhezustand (IdleBridge): der Worker hat keinen Soll-Eingang; die Bedienung bleibt im Geraetekern gespeichert
    und gilt beim naechsten Start mit Regelung. Kein "Kein Handler ... verworfen" je Dokument."""
    leerlauf = RegulationWorker(clock=welt.clock)
    welt.host.bedienung_uebernommen({"modus": "manuell", "raum_soll": 21.0})  # vor dem Binden
    with caplog.at_level("WARNING"):
        welt.host.worker_binden(leerlauf, soll_eingang=False)
        welt.host.bedienung_uebernommen({"modus": "manuell", "raum_soll": 22.5})
        leerlauf.run_pending()
    assert "Kein Handler" not in caplog.text
