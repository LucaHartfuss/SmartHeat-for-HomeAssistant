import stat

import pytest
from configs import apply_config, write_runtime_files
from fake_z2m import FakeZigbee2Mqtt
from fakes import FakeBus

from smartheat_gateway import topics
from smartheat_gateway.agent import identity, lifecycle
from smartheat_gateway.agent.commands import Done, Failed, Waiting, execute
from smartheat_gateway.agent.context import AgentContext
from smartheat_gateway.bus import decode
from smartheat_gateway.config import load_raw
from smartheat_gateway.files import write_json
from smartheat_gateway.paths import Paths
from smartheat_gateway.zigbee import ZigbeeMirror


@pytest.fixture
def ctx(data_dir, clock):
    bus = FakeBus()
    z2m = FakeZigbee2Mqtt(bus)
    z2m.bridge(online=True)
    mirror = ZigbeeMirror(bus, clock)
    mirror.start()
    paths = Paths(data_dir)
    context = AgentContext(paths, bus, mirror, identity.load_or_create(paths), clock=clock)
    context.start()
    return context


def _status(ctx, **fields):
    ctx.bus.publish(
        topics.STATUS, {"schema": 2, "status": "regelt", "setup_id": None, "grund": None, **fields}, retain=True,
    )


def test_apply_config_writes_secrets_first_and_waits_for_the_status(ctx, monkeypatch):
    order = []
    real = lifecycle.write_json
    monkeypatch.setattr(
        lifecycle, "write_json", lambda path, data, **kw: (order.append(path.name), real(path, data, **kw)),
    )
    outcome = execute(ctx, "apply_config", {"setup_id": "setup-1", "config": apply_config()})
    assert order == ["runtime.json", "runtime_config.json"]
    assert (topics.CMD_RELOAD, {"setup_id": "setup-1"}) in ctx.bus.decoded()
    assert isinstance(outcome, Waiting) and outcome.check() is None
    _status(ctx, setup_id="setup-1")
    assert outcome.check() == Done({"setup_id": "setup-1"})
    assert "test-password" not in ctx.paths.runtime_config.read_text()
    assert "test-secret" not in ctx.paths.runtime_config.read_text()
    assert stat.S_IMODE(ctx.paths.runtime_secrets.stat().st_mode) == 0o600


def test_apply_config_keeps_missing_secrets(ctx):
    execute(ctx, "apply_config", {"setup_id": "setup-1", "config": apply_config()})
    config = apply_config(setup_id="setup-2")
    del config["mqtt_password"]
    del config["installation_token"]
    config["cloudflared"] = {**config["cloudflared"]}
    del config["cloudflared"]["service_token_secret"]
    assert isinstance(execute(ctx, "apply_config", {"setup_id": "setup-2", "config": config}), Waiting)
    raw = load_raw(ctx.paths)
    assert raw["setup_id"] == "setup-2" and raw["installation_token"] == "test-token"
    assert raw["mqtt_password"] == "test-password" and raw["cloudflared"]["service_token_secret"] == "test-secret"


def test_apply_config_rejections(ctx):
    assert execute(ctx, "apply_config", {"setup_id": "a", "config": apply_config()}).grund == "ungueltige_nutzlast"
    bad = execute(ctx, "apply_config", {"setup_id": "setup-1", "config": apply_config(room_target_start=30.0)})
    assert isinstance(bad, Failed) and bad.grund == "konfiguration_ungueltig"
    assert not ctx.paths.runtime_config.exists()
    iot = apply_config(transport={
        "kind": "iot_core", "host": "x.example.test", "port": 8883, "alpn": None,
        "ca_pem": "-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----", "client_id": "t",
    })
    assert execute(ctx, "apply_config", {"setup_id": "setup-1", "config": iot}).grund == "konfiguration_ungueltig"
    assert not ctx.paths.runtime_config.exists() and not ctx.paths.runtime_secrets.exists()


def test_apply_config_rejection_redacts_secrets(ctx):
    # Die Fehlermeldung zitiert den ungueltigen Wert; stimmt er mit einem Geheimnis ueberein, erscheint *** statt seiner.
    failed = execute(ctx, "apply_config", {"setup_id": "setup-1", "config": apply_config(thermostat="test-password")})
    assert isinstance(failed, Failed) and failed.grund == "konfiguration_ungueltig"
    assert "thermostat" in failed.text and "***" in failed.text and "test-password" not in failed.text


def test_apply_config_waits_until_the_status_shows_the_new_setup(ctx, clock):
    _status(ctx, setup_id="setup-0")
    outcome = execute(ctx, "apply_config", {"setup_id": "setup-1", "config": apply_config()})
    assert isinstance(outcome, Waiting) and outcome.deadline == clock() + lifecycle.APPLY_CONFIRM_SECONDS
    clock.advance(lifecycle.APPLY_CONFIRM_SECONDS - 1)
    assert outcome.check() is None  # der Status zeigt noch die alte Einrichtung
    _status(ctx, setup_id="setup-1")
    assert outcome.check() == Done({"setup_id": "setup-1"})


def test_repeated_apply_config_after_agent_restart_completes(ctx):
    for _ in range(2):  # Server liefert den Befehl nach einem Agent-Neustart erneut
        outcome = execute(ctx, "apply_config", {"setup_id": "setup-1", "config": apply_config()})
    assert load_raw(ctx.paths)["setup_id"] == "setup-1"
    _status(ctx, setup_id="setup-1")
    assert isinstance(outcome, Waiting) and outcome.check() == Done({"setup_id": "setup-1"})


def test_set_room_target(ctx):
    assert execute(ctx, "set_room_target", {"value": 22.0}).grund == "nicht_eingerichtet"
    write_runtime_files(ctx.paths, apply_config())
    assert execute(ctx, "set_room_target", {"value": 25.5}).grund == "ausserhalb_bereich"
    assert execute(ctx, "set_room_target", {"value": 14.5}).grund == "ausserhalb_bereich"
    assert execute(ctx, "set_room_target", {"value": 21.3}).grund == "ausserhalb_bereich"
    assert execute(ctx, "set_room_target", {"value": True}).grund == "ausserhalb_bereich"
    assert not any(topic == topics.CMD_ROOM_TARGET for topic, _ in ctx.bus.decoded())
    outcome = execute(ctx, "set_room_target", {"value": 22.0})
    assert decode(ctx.bus.published[-1][1])["value"] == 22.0
    assert isinstance(outcome, Waiting) and outcome.check() is None
    ctx.bus.publish(topics.RAUM, {"ist": 20.0, "soll": 22.0, "soll_quelle": "portal", "ts": "x"}, retain=True)
    assert outcome.check() == Done({"value": 22.0})


def test_set_room_target_to_the_active_value_is_confirmed(ctx, clock):
    write_runtime_files(ctx.paths, apply_config())
    ctx.bus.publish(topics.RAUM, {"ist": 20.0, "soll": 21.5, "soll_quelle": "portal", "ts": "x"}, retain=True)
    outcome = execute(ctx, "set_room_target", {"value": 21.5})
    assert isinstance(outcome, Waiting) and outcome.deadline == clock() + lifecycle.ROOM_TARGET_CONFIRM_SECONDS
    assert outcome.check() == Done({"value": 21.5})


def test_sign_off_success_then_cleanup(ctx):
    write_runtime_files(ctx.paths, apply_config())
    write_json(ctx.paths.backup, {"restore_point": {"curve": 1.0, "room_setpoint": 20.0}})
    outcome = execute(ctx, "sign_off", {})
    raw = load_raw(ctx.paths)
    assert raw["abgemeldet"] is True and "mqtt_password" not in raw and "mqtt_username" not in raw
    assert "cloudflared" not in raw
    new_setup = raw["setup_id"]
    assert (topics.CMD_RELOAD, {"setup_id": new_setup}) in ctx.bus.decoded()
    assert isinstance(outcome, Waiting) and outcome.check() is None
    assert not lifecycle.cleanup_after_sign_off(ctx)  # Laufzeit ruht noch nicht abgemeldet
    _status(ctx, status="abgemeldet", setup_id=new_setup, grund=None)
    assert outcome.check() == Done({"zurueckgesetzt": True, "werte": {"curve": 1.0, "room_setpoint": 20.0}})
    assert lifecycle.cleanup_after_sign_off(ctx)
    assert not ctx.paths.runtime_dir.exists() and not ctx.paths.runtime_config.exists()
    assert not ctx.paths.runtime_secrets.exists() and ctx.paths.device_dir.exists()
    assert not lifecycle.cleanup_after_sign_off(ctx)  # einmal genuegt


def test_sign_off_repeated_after_restart_keeps_its_setup_id(ctx):
    write_runtime_files(ctx.paths, apply_config())
    execute(ctx, "sign_off", {})
    first = load_raw(ctx.paths)["setup_id"]
    outcome = execute(ctx, "sign_off", {})  # Server liefert den Befehl nach einem Agent-Neustart erneut
    assert load_raw(ctx.paths)["setup_id"] == first
    _status(ctx, status="abgemeldet", setup_id=first, grund=None)
    assert isinstance(outcome, Waiting) and outcome.check() == Done({"zurueckgesetzt": True, "werte": {}})


def test_sign_off_without_setup(ctx):
    assert execute(ctx, "sign_off", {}).grund == "nicht_eingerichtet"
    assert not lifecycle.cleanup_after_sign_off(ctx)


def test_sign_off_with_failed_restore_keeps_the_device(ctx):
    write_runtime_files(ctx.paths, apply_config())
    outcome = execute(ctx, "sign_off", {})
    new_setup = load_raw(ctx.paths)["setup_id"]
    grund = "Zurücksetzen der Anlage scheitert, neuer Versuch in 300 s"
    _status(ctx, status="abgemeldet", setup_id=new_setup, grund=grund)
    assert isinstance(outcome, Waiting)
    result = outcome.check()
    assert isinstance(result, Done) and result.result["zurueckgesetzt"] is False
    assert not lifecycle.cleanup_after_sign_off(ctx)
    assert ctx.paths.runtime_config.exists()
