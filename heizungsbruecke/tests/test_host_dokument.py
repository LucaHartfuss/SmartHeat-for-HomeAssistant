"""Spec 5c 1.3 / 5b 5.2: HaHost liest das Konfigurationsdokument; der Raum-Soll kommt aus dem Soll-Speicher."""
import json

import pytest
from fakes import FakeHa
from geraet_helpers import FakeGeraet, konfiguration_aus_optionen
from test_runtime import OPTIONS, env  # noqa: F401 - env ist eine Fixture

from heizungsbruecke import config
from heizungsbruecke.ha_signals import SOLL_REF
from heizungsbruecke.host import HaHost
from smartheat_runtime import app, aufraeumen
from smartheat_runtime.app import StartFailure
from smartheat_runtime.ports import SourceUnavailable
from smartheat_runtime.runtime import Runtime
from smartheat_runtime.runtime_config import BATTERY_LOW_FLAG, BATTERY_PERCENT
from smartheat_runtime.worker import RegulationWorker


def test_load_builds_the_runtime_from_the_document(env):
    geraet = FakeGeraet(konfiguration_aus_optionen(OPTIONS))
    host = HaHost(geraet, env.ha, clock=env.clock)
    loaded = host.load()
    assert loaded.config.tenant_id == OPTIONS["tenant_id"]
    assert loaded.manifest.refs["room_target"] == SOLL_REF
    assert loaded.manifest.refs["curve_current"] == OPTIONS["entity_curve_current"]
    assert host.soll_entity == OPTIONS["entity_room_target"]


def test_room_target_reads_the_store_not_the_entity(env):
    geraet = FakeGeraet(konfiguration_aus_optionen(OPTIONS), bedienung={"raum_soll": 21.5})
    host = HaHost(geraet, env.ha, clock=env.clock)
    host.load()
    env.ha.states[OPTIONS["entity_room_target"]] = 18.0
    assert host.signals.get_state(SOLL_REF) == 21.5


def test_without_document_the_host_waits_unconfigured(env):
    host = HaHost(FakeGeraet(None), env.ha, clock=env.clock)
    boot = host.boot_info()
    assert not boot.configured and not boot.signed_off


def test_sign_off_marker_starts_signed_off(env, tmp_path):
    konfiguration = konfiguration_aus_optionen(OPTIONS)
    config.ABGEMELDET_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.ABGEMELDET_PATH.write_text(json.dumps({"anlage": konfiguration["anlage"]}))
    boot = HaHost(FakeGeraet(konfiguration), env.ha, clock=env.clock).boot_info()
    assert boot.signed_off and not boot.configured


def test_empty_configuration_with_pending_reset_boots_signed_off_with_the_old_one(env):
    """Spec Stufe 3 S3 Abschnitt 4: die Laufzeit setzt mit der Konfiguration der Aufraeum-Markierung zurueck."""
    konfiguration = konfiguration_aus_optionen(OPTIONS)
    aufraeumen.markieren(config.AUFRAEUMEN_PATH, konfiguration)
    host = HaHost(FakeGeraet({**konfiguration, "anlage": None}), env.ha, clock=env.clock)
    boot = host.boot_info()
    assert boot.signed_off and not boot.configured and boot.tenant_id == konfiguration["anlage"]
    assert boot.aufraeumen_path == config.AUFRAEUMEN_PATH
    assert host.sign_off_parts() is not None


def test_empty_configuration_after_the_reset_boots_not_configured(env):
    konfiguration = konfiguration_aus_optionen(OPTIONS)
    aufraeumen.markieren(config.AUFRAEUMEN_PATH, konfiguration)
    aufraeumen.zurueckgesetzt(config.AUFRAEUMEN_PATH, konfiguration["anlage"])
    boot = HaHost(FakeGeraet({**konfiguration, "anlage": None}), env.ha, clock=env.clock).boot_info()
    assert not boot.signed_off and not boot.configured


def test_runtime_starts_on_the_document(env):
    geraet = FakeGeraet(konfiguration_aus_optionen(OPTIONS))
    bridge = app.start(HaHost(geraet, env.ha, clock=env.clock), env.clock, mqtt_factory=geraet.kanal,
                       neustart=geraet.neustart)
    assert isinstance(bridge, Runtime)


def test_the_target_signal_is_unavailable_until_the_store_is_loaded(env):
    host = HaHost(FakeGeraet(konfiguration_aus_optionen(OPTIONS)), env.ha, clock=env.clock)
    with pytest.raises(SourceUnavailable):
        host.signals.get_state(SOLL_REF)
    assert host.signals.device_key(SOLL_REF) == SOLL_REF
    assert host.signals.device_key("climate.a::current_temperature") == "climate.a"


def test_the_stored_target_wins_over_the_server_start_value_while_a_local_wish_is_open(env):
    config.ROOM_TARGET_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.ROOM_TARGET_PATH.write_text('{"value": 19.5, "source": "lokal", "ts": "x"}')
    host = HaHost(FakeGeraet(konfiguration_aus_optionen(OPTIONS), bedienung={"raum_soll": 22.0}, dirty=True), env.ha,
                  clock=env.clock)
    host.load()
    assert host.signals.get_state(SOLL_REF) == 19.5


def test_without_a_valid_start_value_the_target_defaults(env):
    env.ha.states[OPTIONS["entity_room_target"]] = 30.0  # ausserhalb des Bedienbereichs
    host = HaHost(FakeGeraet(konfiguration_aus_optionen(OPTIONS), bedienung={"raum_soll": 30.0}), env.ha,
                  clock=env.clock)
    host.load()
    assert host.signals.get_state(SOLL_REF) == 20.0


@pytest.mark.parametrize(("angezeigt", "start"), [(22.5, 22.5), (21.3, 21.3), (14.5, 20.0),
                                                  (KeyError("weg"), 20.0)])
def test_without_stored_target_and_operation_the_store_starts_from_the_entity(env, angezeigt, start):
    """Controller-Entscheid 3a: ohne Datei und ohne Bedienung zeigt die Soll-Entity den Start (im Bedienbereich, ohne
    Rasterzwang: sonst wuerde das erste Lesen nach dem Verbinden den Wert als Wunsch melden)."""
    env.ha.states[OPTIONS["entity_room_target"]] = angezeigt
    host = HaHost(FakeGeraet(konfiguration_aus_optionen(OPTIONS)), env.ha, clock=env.clock)
    host.load()
    assert host.store.value == start and host.store.source == "portal"


# --- Angleichen der Soll-Entity beim Start (Controller-Entscheid 3b, wie das Gateway) -----------------------------

CLIMATE = "climate.wz"
CLIMATE_SOLL = f"{CLIMATE}::temperature"


def _climate_start(env, *, angezeigt, bedienung, dirty=False, modus="heat", gespeichert=None):
    env.ha.states.update({CLIMATE: modus, CLIMATE_SOLL: angezeigt})
    if gespeichert is not None:
        config.ROOM_TARGET_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.ROOM_TARGET_PATH.write_text(json.dumps(gespeichert))
    geraet = FakeGeraet(konfiguration_aus_optionen({**OPTIONS, "entity_room_target": CLIMATE_SOLL}),
                        bedienung=bedienung, dirty=dirty)
    host = HaHost(geraet, env.ha, clock=env.clock)
    loaded = host.load()
    worker = RegulationWorker(env.clock)
    client = host.trigger_source(loaded.manifest, worker)
    return host, geraet, worker, client


def _erstes_lesen(env, worker, client, *, angezeigt):
    """HA meldet beim ersten Verbinden noch den alten Wert (das Thermostat hat den neuen noch nicht uebernommen)."""
    env.ha.states[CLIMATE_SOLL] = angezeigt
    client.kwargs["on_connected"]()
    worker.run_pending()


def test_the_operation_is_written_to_the_entity_at_start_and_not_turned_into_a_wish(env):
    host, geraet, worker, client = _climate_start(env, angezeigt=19.0, bedienung={"raum_soll": 21.5})
    assert (CLIMATE_SOLL, 21.5) in env.ha.writes and host.store.echo_rest() > 0
    _erstes_lesen(env, worker, client, angezeigt=19.0)
    assert geraet.wuensche == [] and host.store.value == 21.5


def test_a_failed_start_alignment_is_done_again_on_connect(env):
    """Final-Review I1: scheitert das Angleichen beim Start (HA lehnt ab), holt das erste Verbinden es nach, statt den
    alten Wert der Entity als Wunsch zu nehmen."""
    env.ha.write_error = RuntimeError("HA lehnt ab")
    host, geraet, worker, client = _climate_start(env, angezeigt=19.0, bedienung={"raum_soll": 21.5})
    assert env.ha.writes == [] and host.store.echo_rest() == 0
    env.ha.write_error = None
    _erstes_lesen(env, worker, client, angezeigt=19.0)
    assert env.ha.writes == [(CLIMATE_SOLL, 21.5)] and env.ha.states[CLIMATE_SOLL] == 21.5
    assert geraet.wuensche == [] and host.store.value == 21.5 and host.store.echo_rest() > 0


def test_an_operation_that_arrived_during_a_restart_wins_over_the_stored_value(env):
    host, geraet, worker, client = _climate_start(
        env, angezeigt=19.5, bedienung={"raum_soll": 22.0},
        gespeichert={"value": 19.5, "source": "portal", "ts": "x"})
    assert (host.store.value, host.store.source) == (22.0, "portal")
    assert (CLIMATE_SOLL, 22.0) in env.ha.writes
    _erstes_lesen(env, worker, client, angezeigt=19.5)
    assert geraet.wuensche == [] and host.store.value == 22.0


def test_a_pending_local_wish_keeps_the_stored_value_and_writes_nothing(env):
    host, _, _, _ = _climate_start(env, angezeigt=19.5, bedienung={"raum_soll": 22.0}, dirty=True,
                                   gespeichert={"value": 19.5, "source": "lokal", "ts": "x"})
    assert (host.store.value, host.store.source) == (19.5, "lokal") and env.ha.writes == []


def test_an_entity_that_already_shows_the_target_is_not_written(env):
    host, _, _, _ = _climate_start(env, angezeigt=21.5, bedienung={"raum_soll": 21.5})
    assert env.ha.writes == [] and host.store.echo_rest() == 0


def test_a_switched_off_thermostat_is_not_written(env):
    """Soll-Quelle aus (Spec 5b 3.2): keine Bedienung an die Quelle; beim Wiedereinschalten gleicht der Soll-Eingang an."""
    host, geraet, worker, client = _climate_start(env, angezeigt=19.0, bedienung={"raum_soll": 21.5}, modus="off")
    assert env.ha.writes == [] and host.store.echo_rest() == 0
    _erstes_lesen(env, worker, client, angezeigt=19.0)
    assert geraet.wuensche == [] and host.store.value == 21.5 and geraet.aus[-1] == (True, 21.5)


def test_a_thermostat_in_frost_protection_is_not_written(env):
    """Ein Soll bis FROSTSCHUTZ_BIS (7 °C) ist die Frostschutz-Stellung (Spec 5b 3.2): ein Neustart dreht den bewusst
    auf 5 °C gestellten Heizkoerper nicht wieder hoch."""
    host, geraet, worker, client = _climate_start(env, angezeigt=5.0, bedienung={"raum_soll": 21.5})
    assert env.ha.writes == [] and host.store.echo_rest() == 0
    _erstes_lesen(env, worker, client, angezeigt=5.0)
    assert geraet.wuensche == [] and host.store.value == 21.5 and geraet.aus[-1] == (True, 21.5)


def test_a_sensor_target_is_never_written(env):
    env.ha.states[OPTIONS["entity_room_target"]] = 19.0
    geraet = FakeGeraet(konfiguration_aus_optionen(OPTIONS), bedienung={"raum_soll": 21.5})
    host = HaHost(geraet, env.ha, clock=env.clock)
    loaded = host.load()
    host.trigger_source(loaded.manifest, RegulationWorker(env.clock))
    assert env.ha.writes == [] and host.store.echo_rest() == 0


def test_a_document_without_a_required_role_fails_the_start_instead_of_raising_a_key_error(env):
    options = {**OPTIONS, "lever_set": "weishaupt_wwp"}  # Weishaupt braucht mode_select und Sollwerte, die fehlen
    host = HaHost(FakeGeraet(konfiguration_aus_optionen(options)), env.ha, clock=env.clock)
    with pytest.raises(StartFailure) as error:
        host.load()
    assert "fehlt die Zuordnung" in error.value.grund


def test_an_unknown_lever_set_fails_the_start(env):
    host = HaHost(FakeGeraet(konfiguration_aus_optionen({**OPTIONS, "lever_set": "gibt_es_nicht"})), env.ha,
                  clock=env.clock)
    with pytest.raises(StartFailure, match="Hebelsatz 'gibt_es_nicht' ist für Home Assistant unbekannt"):
        host.load()


def test_without_binding_parameters_the_start_fails_and_with_them_it_runs(env):
    """Spec Stufe 3 S4b 4.3: eine gespeicherte Konfiguration ohne Parameter startet nicht (Konfigurationsfehler statt
    Absturz); sobald die Konfiguration mit Parametern kommt, laeuft der Start."""
    ohne = konfiguration_aus_optionen({**OPTIONS, "bindungsparameter": None})
    with pytest.raises(StartFailure, match="Bindungsparameter"):
        HaHost(FakeGeraet(ohne), env.ha, clock=env.clock).load()
    mit = {**ohne, "bindungsparameter": konfiguration_aus_optionen(OPTIONS)["bindungsparameter"]}
    assert HaHost(FakeGeraet(mit), env.ha, clock=env.clock).load().binding.description.settle_seconds == 2100


def test_sign_off_without_binding_parameters_does_not_reset_and_logs_it(env, caplog):
    """Spec Stufe 3 S4b 8: Abmelden ohne Bindungsparameter setzt nicht zurueck (kein Schreiben an HA) und loggt den
    Grund; Gegenstueck zum Gateway (gateway/tests/test_host.py)."""
    ohne = konfiguration_aus_optionen({**OPTIONS, "bindungsparameter": None})
    config.ABGEMELDET_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.ABGEMELDET_PATH.write_text(json.dumps({"anlage": ohne["anlage"]}))
    host = HaHost(FakeGeraet(ohne), env.ha, clock=env.clock)
    assert host.boot_info().signed_off
    assert host.sign_off_parts() is None
    assert "Abmelden ohne Zuruecksetzen: Konfiguration: Die Bindungsparameter fehlen." in caplog.text
    assert env.ha.writes == []


def test_the_runtime_idles_with_a_config_error_for_a_missing_role(env):
    geraet = FakeGeraet(konfiguration_aus_optionen({**OPTIONS, "lever_set": "weishaupt_wwp"}))
    bridge = app.start(HaHost(geraet, env.ha, clock=env.clock), env.clock, mqtt_factory=geraet.kanal,
                       neustart=geraet.neustart)
    assert not isinstance(bridge, Runtime)


def test_trigger_source_is_built_on_the_real_entity_and_wishes_reach_the_device(env):
    geraet = FakeGeraet(konfiguration_aus_optionen(OPTIONS))
    host = HaHost(geraet, env.ha, clock=env.clock)
    loaded = host.load()
    worker = RegulationWorker(env.clock)
    client = host.trigger_source(loaded.manifest, worker)
    assert {"platform": "state", "entity_id": OPTIONS["entity_room_target"],
            "for": {"seconds": 10}} in client.kwargs["triggers"]
    client.kwargs["on_trigger_event"]({"platform": "event", "event": {
        "event_type": "smartheat_wunsch", "data": {"raum_soll": 22.0}, "context": {"parent_id": "p"}}})
    worker.run_pending()
    assert geraet.wuensche == [(22.0, "smartheat_entity", "automation")]


def test_a_climate_target_without_attribute_is_a_start_failure(env):
    konfiguration = konfiguration_aus_optionen({**OPTIONS, "entity_room_target": "climate.wz"})
    with pytest.raises(StartFailure, match="climate.wz::temperature"):
        HaHost(FakeGeraet(konfiguration), env.ha, clock=env.clock).load()


def _rastung_schreiben(verteilsystem):
    from smartheat_core import rastung
    rastung.schreiben(config.RASTUNG_PATH, rastung.Rastung("vaillant_vrc720", verteilsystem),
                      "2026-10-14T00:00:00+00:00")


def test_rastung_path_is_the_device_lock():
    from smartheat_core import rastung
    from smartheat_device.device import DEVICE_DIR
    assert config.RASTUNG_PATH == config.DEVICE_DATA_DIR / DEVICE_DIR / rastung.DATEI


def test_load_refuses_a_configuration_that_does_not_fit_the_lock(env):
    _rastung_schreiben("Fussbodenheizung")
    host = HaHost(FakeGeraet(konfiguration_aus_optionen(OPTIONS)), env.ha, clock=env.clock)
    with pytest.raises(StartFailure, match="festgelegt"):
        host.load()


def test_load_accepts_a_configuration_that_fits_the_lock(env):
    _rastung_schreiben(OPTIONS["verteilsystem"])
    HaHost(FakeGeraet(konfiguration_aus_optionen(OPTIONS)), env.ha, clock=env.clock).load()


def test_sign_off_parts_are_none_with_a_broken_lock(env):
    config.RASTUNG_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.RASTUNG_PATH.write_text("{")
    host = HaHost(FakeGeraet(konfiguration_aus_optionen(OPTIONS)), env.ha, clock=env.clock)
    assert host.sign_off_parts() is None


def test_ursprungswerte_server_reads_the_current_configuration_fresh(env):
    konfiguration = konfiguration_aus_optionen(OPTIONS)
    geraet = FakeGeraet({**konfiguration, "ursprungswerte": {"heat_limit": 16.0}})
    host = HaHost(geraet, env.ha, clock=env.clock)
    assert host.ursprungswerte_server() == {"heat_limit": 16.0}
    geraet._konfiguration = {**konfiguration, "ursprungswerte": {"heat_limit": 15.0}}  # neue Konfiguration trifft ein
    assert host.ursprungswerte_server() == {"heat_limit": 15.0}


def test_ursprungswerte_server_falls_back_to_the_marker_while_the_reset_is_pending(env):
    konfiguration = {**konfiguration_aus_optionen(OPTIONS), "ursprungswerte": {"heat_limit": 16.0}}
    aufraeumen.markieren(config.AUFRAEUMEN_PATH, konfiguration)
    host = HaHost(FakeGeraet({**konfiguration, "anlage": None, "ursprungswerte": {}}), env.ha, clock=env.clock)
    assert host.ursprungswerte_server() == {"heat_limit": 16.0}


def test_ursprungswerte_server_ignores_a_new_setup_while_the_reset_is_pending(env):
    """Minor 3: eine neue Einrichtung (andere Anlage) trifft ein, bevor das Zuruecksetzen gelang: die Werte der alten
    Anlage aus der Markierung gelten weiter."""
    alt = {**konfiguration_aus_optionen(OPTIONS), "ursprungswerte": {"heat_limit": 16.0}}
    aufraeumen.markieren(config.AUFRAEUMEN_PATH, alt)
    geraet = FakeGeraet({**alt, "anlage": None, "ursprungswerte": {}})
    host = HaHost(geraet, env.ha, clock=env.clock)
    geraet._konfiguration = {**alt, "anlage": "andere_anlage", "ursprungswerte": {"heat_limit": 12.0}}
    assert host.ursprungswerte_server() == {"heat_limit": 16.0}


@pytest.fixture
def host_mit_klima_zone(env):
    """Vaillant mit einer climate-Zone als Parallelverschiebung (shift_current)."""
    konfiguration = konfiguration_aus_optionen({**OPTIONS, "entity_shift_current": "climate.zone"})
    return HaHost(FakeGeraet(konfiguration), env.ha, clock=env.clock)


def test_abmelden_stellt_dieselben_rollen_zurueck_wie_der_hebelsatz(host_mit_klima_zone):
    """Spec Stufe 3 S5 Review: sign_off_parts nach dem Umbau, auch fuer eine climate-Zone (::temperature)."""
    parts = host_mit_klima_zone.sign_off_parts()
    assert parts is not None
    binding = parts.binding
    gesendet = [lever for lever in binding.description.lever_set.levers if binding.has(lever)]
    assert gesendet == list(binding.description.lever_set.levers) == ["curve", "room_setpoint", "heat_limit"]
    assert binding.ref("room_setpoint") == "climate.zone::temperature"
    assert binding.ref("curve") == OPTIONS["entity_curve_current"]
    assert binding.ref("heat_limit") == OPTIONS["entity_heat_limit"]


def test_host_ohne_bekannten_hebelsatz_wirft_im_konstruktor_nicht(env):
    """Meldedienste und Hinweise kommen aus der Bindung; ein unbekannter Hebelsatz meldet erst load()."""
    konfiguration = konfiguration_aus_optionen({**OPTIONS, "lever_set": "gibt_es_nicht"})
    host = HaHost(FakeGeraet(konfiguration), env.ha, clock=env.clock)
    assert host.boot_info().notify_hints_off == ()


class _DiensteHa(FakeHa):
    """Fake-HA, die zu jedem Push den Notify-Dienst festhaelt (Grenze HA)."""

    def __init__(self):
        super().__init__()
        self.dienste: list[tuple[str, str]] = []

    def send_notification(self, service, message):
        self.dienste.append((service, message))


def test_hinweise_batterien_und_meldedienste_kommen_aus_der_bindung(env):
    options = {**OPTIONS, "notify_hints_off": ["batterie"],
               "battery_entities": ["sensor.thermostat_battery", "binary_sensor.thermostat_battery_low"]}
    ha = _DiensteHa()
    host = HaHost(FakeGeraet(konfiguration_aus_optionen(options), ha=ha), ha, clock=env.clock)
    host.notify_sink.push("test", "Text")  # der Sink des Hosts schickt an die Meldedienste der Bindung
    assert ha.dienste == [("notify.handy", "Text")]
    assert host.boot_info().notify_hints_off == ("batterie",)
    runtime = host.load().config
    assert runtime.notify_hints_off == ("batterie",)
    assert [(ref.ref, ref.art) for ref in runtime.battery_refs] == [
        ("sensor.thermostat_battery", BATTERY_PERCENT), ("binary_sensor.thermostat_battery_low", BATTERY_LOW_FLAG)]
