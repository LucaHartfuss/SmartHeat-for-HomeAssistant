"""Spec 5c 1.1/1.2: Start ohne Optionen als Geraet (Geraetekern und Laufzeit in einem Prozess); Neustart erst nach
sauberem Trennen; Aufraeumen nach dem Entfernen. Die frueheren Tests des Optionsmodus stehen noch in test_main.py
(Task 10 ordnet sie ein)."""
import json
import logging
from types import SimpleNamespace

import pytest
from geraet_helpers import konfiguration_aus_optionen
from test_runtime import OPTIONS, env  # noqa: F401 - env ist eine Fixture

import heizungsbruecke.__main__ as main_module
from heizungsbruecke import config
from smartheat_device import documents as device_documents
from smartheat_device.host import Anzeige
from smartheat_device.laufzeit import RuntimeChannel
from smartheat_runtime import abo, app, dokument_config
from smartheat_runtime.runtime import Runtime

DEVICE_ID = "shg-aaaaaaaaaaaaaaaa"
CLAIM_CODE = "ABCDEFGHIJKL"


class FakeThread:
    def __init__(self, order):
        self._order = order

    def join(self, timeout=None):
        self._order.append("join")

    def is_alive(self):
        return False


class FakeDevice:
    """Sicht des Geraetekerns, die HaDeviceHost, HaHost und main brauchen; protokolliert start/stop."""

    def __init__(self, konfiguration=None, bedienung=None, order=None):
        self.konf, self.bed = konfiguration, bedienung
        self.order = order if order is not None else []
        self.identity = SimpleNamespace(device_id=DEVICE_ID, claim_code=CLAIM_CODE)
        self.documents = SimpleNamespace(aktuell=lambda art: None)
        self.sent = []

    def konfiguration(self):
        return self.konf

    def bedienung(self):
        return self.bed

    def abo_status(self):
        return dokument_config.abo_status(self.konf)

    def start(self):
        self.order.append("start")
        return FakeThread(self.order)

    def stop(self):
        self.order.append("stop")

    def laufzeit_kanal(self, worker):
        return RuntimeChannel(lambda name, payload: self.sent.append(name) or len(self.sent), lambda: True,
                              lambda: 0).binden(worker)


class FakeSupervisor:
    def __init__(self):
        self.gemeldet = []

    def host_info(self):
        return {"dt_synchronized": True}

    def discovery_anmelden(self, config_):
        self.gemeldet.append(dict(config_))
        return f"uuid-{len(self.gemeldet)}"

    def discovery_entfernen(self, uuid):
        pass


def _anzeige(zustand="nicht_uebernommen", *, eingerichtet=False):
    return Anzeige(zustand=zustand, server_ok=True, device_state="nicht_uebernommen", eingerichtet=eingerichtet,
                   hinweise=(), meldungen=(), konfiguration_abgelehnt=None)


@pytest.fixture
def geraet_env(env, tmp_path, monkeypatch):  # noqa: F811 - env ist die Fixture aus test_runtime
    """env plus Datenordner des Geraets; start() setzt abo.restart_process global um, monkeypatch stellt es zurueck."""
    monkeypatch.setattr(config, "DEVICE_DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "OPTIONS_PATH", tmp_path / "options.json")
    monkeypatch.setattr(abo, "restart_process", abo.restart_process)
    monkeypatch.setattr(main_module.status_holder, "host", None)
    return env


def _verdrahtet(env, device, supervisor=None):
    geraet = main_module.HaDeviceHost(env.ha, supervisor or FakeSupervisor())
    status = main_module.verdrahten(geraet, device, env.ha, supervisor or FakeSupervisor())
    return geraet, status


# --- Optionen ---


def test_bootstrap_url_defaults_to_production(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OPTIONS_PATH", tmp_path / "options.json")
    monkeypatch.delenv("SMARTHEAT_BOOTSTRAP_URL", raising=False)
    assert main_module.bootstrap_url() == main_module.DEFAULT_BOOTSTRAP_URL == "https://accounts.hartfussha.org"
    (tmp_path / "options.json").write_text(json.dumps({"bootstrap_url": None, "log_level": "info"}))
    assert main_module.bootstrap_url() == main_module.DEFAULT_BOOTSTRAP_URL
    (tmp_path / "options.json").write_text(json.dumps({"bootstrap_url": "https://labor.example.test/"}))
    assert main_module.bootstrap_url() == "https://labor.example.test"
    monkeypatch.setenv("SMARTHEAT_BOOTSTRAP_URL", "http://server:5001")
    assert main_module.bootstrap_url() == "http://server:5001"


def test_bootstrap_url_allows_an_http_lab_like_the_gateway(tmp_path, monkeypatch):
    """Wie SHG_DEVICE_API_URL beim Gateway: http und https mit Host (Labor im LAN per http)."""
    monkeypatch.setattr(config, "OPTIONS_PATH", tmp_path / "options.json")
    monkeypatch.delenv("SMARTHEAT_BOOTSTRAP_URL", raising=False)
    (tmp_path / "options.json").write_text(json.dumps({"bootstrap_url": "http://192.168.1.20:5001"}))
    assert main_module.bootstrap_url() == "http://192.168.1.20:5001"


@pytest.mark.parametrize("value", ["ftp://labor.example.test", "labor.example.test", "http://", 42, ""])
def test_an_invalid_bootstrap_url_never_falls_back_to_production(tmp_path, monkeypatch, caplog, value):
    monkeypatch.setattr(config, "OPTIONS_PATH", tmp_path / "options.json")
    monkeypatch.delenv("SMARTHEAT_BOOTSTRAP_URL", raising=False)
    (tmp_path / "options.json").write_text(json.dumps({"bootstrap_url": value}))
    with caplog.at_level(logging.WARNING):
        assert main_module.bootstrap_url() is None
    assert "bootstrap_url" in caplog.text
    if isinstance(value, str) and value:
        assert value not in caplog.text  # nur der Grund, nie die Adresse (koennte Zugangsdaten tragen)


def test_an_invalid_override_never_falls_back_to_production(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OPTIONS_PATH", tmp_path / "options.json")
    monkeypatch.setenv("SMARTHEAT_BOOTSTRAP_URL", "server:5001")
    assert main_module.bootstrap_url() is None


def test_main_with_an_invalid_bootstrap_url_stays_idle_without_the_device_core(geraet_env, monkeypatch):
    (config.OPTIONS_PATH).write_text(json.dumps({"bootstrap_url": "ftp://labor.example.test"}))
    monkeypatch.delenv("SMARTHEAT_BOOTSTRAP_URL", raising=False)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
    monkeypatch.setattr(main_module, "HomeAssistantApi", lambda **kwargs: geraet_env.ha)
    monkeypatch.setattr(main_module, "SupervisorApi", lambda **kwargs: FakeSupervisor())
    monkeypatch.setattr(main_module, "Device", lambda *args, **kwargs: pytest.fail("kein Geraetekern"))
    monkeypatch.setattr(main_module.signal, "signal", lambda *args: None)
    geruht = []
    monkeypatch.setattr(main_module, "_ruhen", lambda: geruht.append(True))
    main_module.main()
    assert geruht == [True]


def test_log_level_from_options(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OPTIONS_PATH", tmp_path / "options.json")
    assert main_module.log_level() == logging.INFO
    (tmp_path / "options.json").write_text(json.dumps({"log_level": "debug"}))
    assert main_module.log_level() == logging.DEBUG
    (tmp_path / "options.json").write_text("{kaputt")
    assert main_module.log_level() == logging.INFO


# --- Start ---


def test_start_without_document_waits_unconfigured_and_reports_through_the_device(geraet_env):
    """Nicht eingerichtet: Ruhezustand ohne Status-Kanal der Laufzeit (nichts von ihr gesendet). Das Status-Ereignis
    Schema 3 kommt vom Geraete-Thread (anzeigen in jedem Takt), dazu die Discovery mit dem Uebernahme-Code."""
    supervisor = FakeSupervisor()
    device = FakeDevice()
    geraet, status = _verdrahtet(geraet_env, device, supervisor)
    result, _ = main_module.start(geraet, device, geraet_env.ha, status, geraet_env.clock)
    result.worker.run_pending()
    assert isinstance(result, app.IdleBridge) and result.reason == app.IDLE_NOT_CONFIGURED
    assert geraet_env.ha.events == [] and geraet.runtime_status_event is None

    geraet.anzeigen(_anzeige())
    ((typ, event),) = geraet_env.ha.events
    assert typ == "smartheat_status" and event["schema"] == 3
    assert event["geraet_id"] == DEVICE_ID and event["geraet_zustand"] == "nicht_uebernommen"
    assert CLAIM_CODE not in json.dumps(event)
    assert supervisor.gemeldet == [{"geraet_id": DEVICE_ID, "zustand": "nicht_uebernommen", "claim_code": CLAIM_CODE}]


def test_an_operation_while_idle_is_not_posted_to_the_worker(geraet_env, caplog):
    device = FakeDevice()
    geraet, status = _verdrahtet(geraet_env, device)
    result, _ = main_module.start(geraet, device, geraet_env.ha, status, geraet_env.clock)
    with caplog.at_level(logging.WARNING):
        geraet.bedienung_uebernommen({"raum_soll": 22.0})
        result.worker.run_pending()
    assert "Kein Handler" not in caplog.text


def test_start_with_document_regulates_and_wires_status_and_operations(geraet_env):
    device = FakeDevice(konfiguration_aus_optionen(OPTIONS), bedienung={"raum_soll": 21.0})
    geraet, status = _verdrahtet(geraet_env, device)
    result, host = main_module.start(geraet, device, geraet_env.ha, status, geraet_env.clock)
    result.worker.run_pending()
    assert isinstance(result, Runtime)
    # laufzeit_merken: das unveraenderte Laufzeit-Ereignis liegt fuer up/status und sign_off beim Geraete-Host
    assert geraet.runtime_status_event["tenant_id"] == OPTIONS["tenant_id"]

    geraet.bedienung_uebernommen({"raum_soll": 22.5})
    result.worker.run_pending()
    assert host.store.value == 22.5
    geraet.anzeigen(_anzeige("regelt", eingerichtet=True))
    assert geraet_env.ha.events[-1][1]["raum_soll"] == 22.5  # wirksamer Raum-Soll aus dem Soll-Speicher


def test_restarts_of_the_runtime_take_the_clean_restart_path(geraet_env):
    """abo.restart_process (Abo am Fristende wieder aktiv, Neupruefung) beendet den Worker ueber Neustart statt os.execv,
    das den Link ohne DISCONNECT abrisse."""
    device = FakeDevice()
    geraet, status = _verdrahtet(geraet_env, device)
    main_module.start(geraet, device, geraet_env.ha, status, geraet_env.clock)
    with pytest.raises(main_module.Neustart):
        abo.restart_process()


# --- Neustart und Ende (Review Focus 4) ---


def test_reload_disconnects_before_exec(geraet_env, monkeypatch):
    """Echter Ablauf von laufen(): eine neue laufzeitrelevante Konfiguration fordert den Neustart an (EV_RELOAD), der
    Worker endet mit Neustart, dann erst Geraetekern stoppen (MQTT-DISCONNECT), auf den Geraete-Thread warten, exec."""
    order = []
    device = FakeDevice(order=order)
    geraet, status = _verdrahtet(geraet_env, device)
    monkeypatch.setattr(main_module, "_exec", lambda: order.append("exec"))
    geraet.konfiguration_uebernommen(None, {"anlage": "neu", "bindung": {}})
    main_module.laufen(geraet, device, geraet_env.ha, status, geraet_env.clock)
    assert order == ["start", "stop", "join", "stop", "exec"]


def test_sigterm_ends_without_exec(geraet_env, monkeypatch):
    order = []
    device = FakeDevice(order=order)
    geraet, status = _verdrahtet(geraet_env, device)
    monkeypatch.setattr(main_module, "_exec", lambda: order.append("exec"))

    def run():
        raise SystemExit(0)

    monkeypatch.setattr(main_module, "start",
                        lambda *args, **kwargs: (SimpleNamespace(worker=SimpleNamespace(run=run)), None))
    with pytest.raises(SystemExit):
        main_module.laufen(geraet, device, geraet_env.ha, status, geraet_env.clock)
    assert order == ["start", "stop", "join", "stop"]


class FailingDevice(FakeDevice):
    def stop(self):
        self.order.append("stop")
        raise OSError("paho")


class FailingThread(FakeThread):
    def join(self, timeout=None):
        self._order.append("join")
        raise RuntimeError("join")


def test_a_failing_end_still_restarts(geraet_env, monkeypatch, caplog):
    """beenden wirft nie: auch wenn das Trennen scheitert, folgt auf einen angeforderten Neustart das exec."""
    order = []
    device = FailingDevice(order=order)
    device.start = lambda: order.append("start") or FailingThread(order)
    geraet, status = _verdrahtet(geraet_env, device)
    monkeypatch.setattr(main_module, "_exec", lambda: order.append("exec"))
    geraet.konfiguration_uebernommen(None, {"anlage": "neu", "bindung": {}})
    with caplog.at_level(logging.WARNING):
        main_module.laufen(geraet, device, geraet_env.ha, status, geraet_env.clock)
    assert order == ["start", "stop", "join", "stop", "exec"]
    assert "OSError" in caplog.text and "paho" not in caplog.text


def test_an_unexpected_start_error_restarts_after_a_pause(geraet_env, monkeypatch, caplog):
    """Ein unerwarteter Fehler im Start beendet den Prozess nie still: Typ ins Log, sauber trennen, Pause, exec."""
    order = []
    device = FakeDevice(order=order)
    geraet, status = _verdrahtet(geraet_env, device)
    monkeypatch.setattr(main_module, "_exec", lambda: order.append("exec"))
    monkeypatch.setattr(main_module.time, "sleep", lambda seconds: order.append(("pause", seconds)))

    def kaputt(*args, **kwargs):
        raise KeyError("geheim")

    monkeypatch.setattr(main_module, "start", kaputt)
    with caplog.at_level(logging.WARNING):
        main_module.laufen(geraet, device, geraet_env.ha, status, geraet_env.clock)
    assert order == ["start", "stop", "join", "stop", ("pause", main_module.FEHLER_PAUSE_SECONDS), "exec"]
    assert "KeyError" in caplog.text and "geheim" not in caplog.text


# --- Aufraeumen nach dem Entfernen ---


class DeletingHa:
    """HA mit den Hilfssensoren; delete_helper scheitert fuer die Entities in failing."""

    def __init__(self, *, reachable=True, failing=()):
        self.reachable, self.failing, self.deleted = reachable, set(failing), []

    def is_reachable(self):
        return self.reachable

    def entity_exists(self, entity_id):
        return True

    def delete_helper(self, entity_id, tenant_id=None):
        if entity_id in self.failing:
            raise RuntimeError("HA antwortet nicht")
        self.deleted.append((entity_id, tenant_id))


TRACKING = {"room_temperature": {"entity_id": "sensor.smartheat_raum", "source": "a"},
            "outdoor_temperature": {"entity_id": "sensor.smartheat_aussen", "source": "b"}}


def _entfernt_vorbereiten(tracking=TRACKING):
    for path in (config.BACKUP_PATH, config.FAILSAFE_PATH, config.ENTITLEMENT_PATH):
        path.write_text("{}")
    config.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    config.ABGEMELDET_PATH.write_text(json.dumps({"anlage": "alt"}))
    config.DERIVED_SENSORS_PATH.write_text(json.dumps(tracking))
    config.AUFRAEUMEN_PATH.write_text(json.dumps({"anlage": "alt"}))


def _anlage_geloescht() -> bool:
    return not any(p.exists() for p in (config.BACKUP_PATH, config.FAILSAFE_PATH, config.ENTITLEMENT_PATH,
                                        config.RUNTIME_DIR))


def test_cleanup_after_removal_deletes_the_plant_state_and_helpers(geraet_env):
    _entfernt_vorbereiten()
    ha = DeletingHa()
    main_module.aufraeumen(ha)
    assert sorted(ha.deleted) == [("sensor.smartheat_aussen", "alt"), ("sensor.smartheat_raum", "alt")]
    assert _anlage_geloescht()
    assert not config.DERIVED_SENSORS_PATH.exists() and not config.AUFRAEUMEN_PATH.exists()


def test_cleanup_keeps_a_new_setup(geraet_env):
    _entfernt_vorbereiten()
    device_documents.write_json(config.DEVICE_DATA_DIR / "device" / "konfiguration.json",
                                {"version": 2, "inhalt": {"anlage": "neu"}, "dirty": False})
    ha = DeletingHa()
    main_module.aufraeumen(ha)
    assert ha.deleted == [] and config.BACKUP_PATH.exists() and config.DERIVED_SENSORS_PATH.exists()
    assert not config.AUFRAEUMEN_PATH.exists()


def test_cleanup_keeps_helpers_it_could_not_delete_for_the_next_start(geraet_env):
    _entfernt_vorbereiten()
    main_module.aufraeumen(DeletingHa(failing={"sensor.smartheat_aussen"}))
    assert _anlage_geloescht()
    assert json.loads(config.DERIVED_SENSORS_PATH.read_text()) == {
        "outdoor_temperature": TRACKING["outdoor_temperature"]}
    assert config.AUFRAEUMEN_PATH.exists()  # der naechste Start versucht es erneut

    ha = DeletingHa()
    main_module.aufraeumen(ha)
    assert ha.deleted == [("sensor.smartheat_aussen", "alt")]
    assert not config.DERIVED_SENSORS_PATH.exists() and not config.AUFRAEUMEN_PATH.exists()


def test_cleanup_waits_a_limited_time_for_home_assistant(geraet_env, monkeypatch):
    """startup: services - HA kann beim Start des Add-ons noch fehlen. Begrenzt warten, dann ohne die Hilfssensoren
    weiter (Tracking und Markierung bleiben fuer den naechsten Start); der Geraetekern startet trotzdem."""
    _entfernt_vorbereiten()
    now = [0.0]
    monkeypatch.setattr(main_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(main_module.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    ha = DeletingHa(reachable=False)
    main_module.aufraeumen(ha)
    assert ha.deleted == [] and now[0] >= main_module.AUFRAEUMEN_HA_WARTEN_SECONDS
    assert _anlage_geloescht()
    assert json.loads(config.DERIVED_SENSORS_PATH.read_text()) == TRACKING and config.AUFRAEUMEN_PATH.exists()


def test_cleanup_without_helpers_does_not_wait_for_home_assistant(geraet_env, monkeypatch):
    _entfernt_vorbereiten()
    config.DERIVED_SENSORS_PATH.unlink()
    monkeypatch.setattr(main_module.time, "sleep", lambda seconds: pytest.fail("kein Warten ohne Hilfssensoren"))
    main_module.aufraeumen(DeletingHa(reachable=False))
    assert _anlage_geloescht() and not config.AUFRAEUMEN_PATH.exists()


def test_cleanup_logs_the_deletion_only_when_something_was_deleted(geraet_env, caplog):
    config.AUFRAEUMEN_PATH.write_text(json.dumps({"anlage": "alt"}))
    with caplog.at_level(logging.INFO):
        main_module.aufraeumen(DeletingHa())
    assert "Anlagengebundener Zustand geloescht" not in caplog.text
    assert not config.AUFRAEUMEN_PATH.exists()
    _entfernt_vorbereiten()
    with caplog.at_level(logging.INFO):
        main_module.aufraeumen(DeletingHa())
    assert "Anlagengebundener Zustand geloescht" in caplog.text


def test_no_marker_no_cleanup(geraet_env):
    config.BACKUP_PATH.write_text("{}")
    main_module.aufraeumen(DeletingHa())
    assert config.BACKUP_PATH.exists()
