"""Spec 5c 1.3 / 5b 5.2: HaHost liest das Konfigurationsdokument; der Raum-Soll kommt aus dem Soll-Speicher."""
import json

import pytest
from geraet_helpers import FakeGeraet, konfiguration_aus_optionen
from test_runtime import OPTIONS, env  # noqa: F401 - env ist eine Fixture

from heizungsbruecke import config, dokument
from heizungsbruecke.host import HaHost
from smartheat_runtime import app
from smartheat_runtime.app import StartFailure
from smartheat_runtime.ports import SourceUnavailable
from smartheat_runtime.runtime import Runtime
from smartheat_runtime.worker import RegulationWorker


def test_load_builds_the_runtime_from_the_document(env):
    geraet = FakeGeraet(konfiguration_aus_optionen(OPTIONS))
    host = HaHost(geraet, env.ha, clock=env.clock)
    loaded = host.load()
    assert loaded.config.tenant_id == OPTIONS["tenant_id"] and loaded.config.descriptor is None
    assert loaded.manifest.refs["room_target"] == dokument.SOLL_REF
    assert loaded.manifest.refs["curve_current"] == OPTIONS["entity_curve_current"]
    assert host.soll_entity == OPTIONS["entity_room_target"]


def test_room_target_reads_the_store_not_the_entity(env):
    geraet = FakeGeraet(konfiguration_aus_optionen(OPTIONS), bedienung={"raum_soll": 21.5})
    host = HaHost(geraet, env.ha, clock=env.clock)
    host.load()
    env.ha.states[OPTIONS["entity_room_target"]] = 18.0
    assert host.signals.get_state(dokument.SOLL_REF) == 21.5


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


def test_runtime_starts_on_the_document(env):
    geraet = FakeGeraet(konfiguration_aus_optionen(OPTIONS))
    bridge = app.start(HaHost(geraet, env.ha, clock=env.clock), env.clock, mqtt_factory=geraet.kanal)
    assert isinstance(bridge, Runtime)


def test_the_target_signal_is_unavailable_until_the_store_is_loaded(env):
    host = HaHost(FakeGeraet(konfiguration_aus_optionen(OPTIONS)), env.ha, clock=env.clock)
    with pytest.raises(SourceUnavailable):
        host.signals.get_state(dokument.SOLL_REF)
    assert host.signals.device_key(dokument.SOLL_REF) == dokument.SOLL_REF
    assert host.signals.device_key("climate.a::current_temperature") == "climate.a"


def test_the_stored_target_wins_over_the_server_start_value_while_a_local_wish_is_open(env):
    config.ROOM_TARGET_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.ROOM_TARGET_PATH.write_text('{"value": 19.5, "source": "lokal", "ts": "x"}')
    host = HaHost(FakeGeraet(konfiguration_aus_optionen(OPTIONS), bedienung={"raum_soll": 22.0}, dirty=True), env.ha,
                  clock=env.clock)
    host.load()
    assert host.signals.get_state(dokument.SOLL_REF) == 19.5


def test_without_a_valid_start_value_the_target_defaults(env):
    env.ha.states[OPTIONS["entity_room_target"]] = 30.0  # ausserhalb des Bedienbereichs
    host = HaHost(FakeGeraet(konfiguration_aus_optionen(OPTIONS), bedienung={"raum_soll": 30.0}), env.ha,
                  clock=env.clock)
    host.load()
    assert host.signals.get_state(dokument.SOLL_REF) == 20.0


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
    with pytest.raises(StartFailure):
        host.load()


def test_the_runtime_idles_with_a_config_error_for_a_missing_role(env):
    geraet = FakeGeraet(konfiguration_aus_optionen({**OPTIONS, "lever_set": "weishaupt_wwp"}))
    bridge = app.start(HaHost(geraet, env.ha, clock=env.clock), env.clock, mqtt_factory=geraet.kanal)
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
