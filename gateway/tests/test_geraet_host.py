"""Spec 5b 5.1/5.3 (Plan D2 Task 3): der Gateway als Host des Geraetekerns - Faehigkeiten, Uhr, Status und Raum im
Prozess, Meldungen, Neustart bei neuer Konfiguration, Aufraeumen nach dem Entfernen, Software-Bruecke, LED-Datei."""
import json
import os
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from configs import konfiguration
from fakes import FakeBus

from smartheat_device import wire
from smartheat_device.host import Anzeige
from smartheat_gateway import geraet_host as gh
from smartheat_gateway.files import write_json
from smartheat_gateway.paths import Paths
from smartheat_gateway.target_store import SOURCE_PORTAL
from smartheat_gateway.triggers import EV_TARGET_INPUT
from smartheat_gateway.version import GATEWAY_VERSION
from smartheat_gateway.zigbee import ZigbeeMirror
from smartheat_runtime import dokument_config
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

    def ergebnis_software(self, version, ok, grund=None, text=""):
        self.ergebnisse.append((version, ok, grund, text))
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
        self.worker.register(gh.EV_RELOAD, self.events.append)
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


def test_clock_sync_comes_from_the_host_status(welt):
    assert welt.host.uhr_synchron() is None
    status = {"netz": True, "dns": True, "zeit_synchron": True, "ts": datetime.fromtimestamp(WALL, UTC).isoformat()}
    write_json(welt.host_dir / "status.json", status)
    assert welt.host.uhr_synchron() is True
    write_json(welt.host_dir / "status.json", {**status, "zeit_synchron": False})
    assert welt.host.uhr_synchron() is False
    welt.wall["t"] = WALL + gh.UHR_MAX_ALTER_SECONDS + 1
    assert welt.host.uhr_synchron() is None  # veraltet
    write_json(welt.host_dir / "status.json", {**status, "ts": "gestern"})
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
    assert welt.kinds() == [gh.EV_RELOAD]


def test_a_restart_requested_before_the_worker_exists_is_posted_on_binding(welt):
    welt.host.neustart_anfordern()
    welt.host.worker_binden(welt.worker)
    assert welt.kinds() == [gh.EV_RELOAD]


def test_an_empty_configuration_after_a_setup_marks_the_cleanup(welt):
    welt.host.konfiguration_uebernommen(konfiguration(), LEER)
    assert json.loads(welt.paths.aufraeumen.read_text()) == {"anlage": "test-tenant"}
    welt.host.worker_binden(welt.worker)
    assert welt.kinds() == [gh.EV_RELOAD]


def test_the_first_empty_configuration_marks_nothing(welt):
    welt.host.konfiguration_uebernommen(None, LEER)
    assert not welt.paths.aufraeumen.exists()


def test_vergessen_removes_the_plant_state_but_keeps_device_and_simulation(welt):
    for path in (welt.paths.backup, welt.paths.failsafe, welt.paths.abgemeldet, welt.paths.room_target,
                 welt.paths.inventur, welt.paths.driver_secrets_dir / "vicare.json",
                 welt.paths.device_dir / "konfiguration.json", welt.paths.sim_dir / "plant.json"):
        write_json(path, {"x": 1})
    gh.vergessen(welt.paths)
    assert not welt.paths.runtime_dir.exists() and not welt.paths.inventur.exists()
    assert not (welt.paths.driver_secrets_dir / "vicare.json").exists()
    assert (welt.paths.device_dir / "konfiguration.json").exists() and (welt.paths.sim_dir / "plant.json").exists()


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
    write_json(welt.paths.software_meldungen, {"meldungen": [
        {"seq": 2, "version": "0.6.2", "result": "rollback", "reason": "ungesund"},
        {"seq": 1, "version": "0.6.1", "result": "ok", "reason": ""}]})
    welt.host.anzeigen(_anzeige())
    assert welt.device.ergebnisse == [("0.6.1", True, None, ""), ("0.6.2", False, "ungesund",
                                                                  gh.SOFTWARE_TEXTE["ungesund"])]
    assert json.loads(welt.paths.software_quittung.read_text()) == {"seq": 2}
    clock.advance(gh.SOFTWARE_SECONDS)
    welt.host.anzeigen(_anzeige())
    assert len(welt.device.ergebnisse) == 2  # quittiert: nichts doppelt


def test_unsent_software_results_wait_for_the_link(welt, clock):
    write_json(welt.paths.software_meldungen, {"meldungen": [
        {"seq": 1, "version": "0.6.1", "result": "rollback", "reason": "server_unerreichbar"}]})
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
    write_json(welt.paths.software_meldungen, {"meldungen": meldungen})
    welt.host.anzeigen(_anzeige())
    assert [e[0] for e in welt.device.ergebnisse] == ["0.6.1", "0.6.2"]
    meldungen.append({"seq": 3, "version": "0.6.3", "result": "ok", "reason": ""})
    write_json(welt.paths.software_meldungen, {"meldungen": meldungen})
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
    assert geraet.ergebnisse == [("0.6.3", True, None, "")]
    assert json.loads(welt.paths.software_quittung.read_text()) == {"seq": 3}


def test_every_software_error_has_a_text():
    assert set(gh.SOFTWARE_TEXTE) == set(wire.SOFTWARE_ERRORS)


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
    write_json(welt.host_dir / "status.json", {"netz": True, "dns": False, "zeit_synchron": True, "geheim": "x"})
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


def test_an_unexpected_driver_error_becomes_a_rejection(welt, caplog):
    # float(10**400) im Simulations-Treiber: OverflowError, den config.treiber_pruefen nicht kennt
    bindung = {"treiber": {"id": "simulation", "parameter": {"lever_set": "viessmann_vicare", "poll_seconds": 10**400}}}
    text = welt.host.treiber_pruefen(bindung)
    assert text == gh.gateway_config.DRIVER_PARAMETER_TEXT.format(art="OverflowError")
    assert "OverflowError" in caplog.text and str(10**400)[:20] not in caplog.text


def test_a_failing_led_write_does_not_stop_the_software_results(welt):
    (welt.paths.agent_state / "belegt").mkdir(parents=True)  # Ziel ist ein Ordner: das Schreiben scheitert
    write_json(welt.paths.software_meldungen, {"meldungen": [
        {"seq": 1, "version": "0.6.1", "result": "ok", "reason": ""}]})
    welt.host.anzeigen(_anzeige())
    assert welt.device.ergebnisse == [("0.6.1", True, None, "")]
    assert welt.host.letzte_anzeige is not None
