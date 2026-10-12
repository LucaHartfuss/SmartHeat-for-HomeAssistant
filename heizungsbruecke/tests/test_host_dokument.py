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


def test_the_stored_target_wins_over_the_server_start_value(env):
    config.ROOM_TARGET_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.ROOM_TARGET_PATH.write_text('{"value": 19.5, "source": "lokal", "ts": "x"}')
    host = HaHost(FakeGeraet(konfiguration_aus_optionen(OPTIONS), bedienung={"raum_soll": 22.0}), env.ha,
                  clock=env.clock)
    host.load()
    assert host.signals.get_state(dokument.SOLL_REF) == 19.5


def test_without_a_valid_start_value_the_target_defaults(env):
    host = HaHost(FakeGeraet(konfiguration_aus_optionen(OPTIONS), bedienung={"raum_soll": 30.0}), env.ha,
                  clock=env.clock)
    host.load()
    assert host.signals.get_state(dokument.SOLL_REF) == 20.0


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
