import datetime
import stat

import pytest
from configs import apply_config, write_runtime_files
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
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


def _sign_csr(csr_pem: str) -> tuple[str, str]:
    """(Zertifikat fuer den Schluessel des CSR, CA-PEM): kleine Test-CA, die den CSR des Gateways signiert (nur Tests)."""
    csr = x509.load_pem_x509_csr(csr_pem.encode())
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "SmartHeat Test-CA")])
    now = datetime.datetime.now(datetime.UTC)
    validity = {"not_valid_before": now - datetime.timedelta(minutes=5), "not_valid_after": now + datetime.timedelta(days=1)}

    def build(subject, issuer, public_key) -> x509.CertificateBuilder:
        return (
            x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(public_key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(validity["not_valid_before"]).not_valid_after(validity["not_valid_after"])
        )

    key_usage = {
        "digital_signature": False, "content_commitment": False, "key_encipherment": False,
        "data_encipherment": False, "key_agreement": False, "key_cert_sign": True, "crl_sign": True,
        "encipher_only": False, "decipher_only": False,
    }
    ca = (
        build(ca_name, ca_name, ca_key.public_key())
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(**key_usage), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    leaf = (
        build(csr.subject, ca_name, csr.public_key())
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(csr.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    pem = serialization.Encoding.PEM
    return leaf.public_bytes(pem).decode(), ca.public_bytes(pem).decode()


def test_iot_core_setup_creates_the_key_with_the_csr_and_never_stores_it_in_the_config(ctx):
    created = execute(ctx, "create_csr", {"tenant_id": "test-tenant"})
    assert isinstance(created, Done) and ctx.paths.transport_key.exists()
    certificate, ca = _sign_csr(created.result["csr"])
    transport = {"kind": "iot_core", "host": "x.example.test", "port": 8883, "alpn": None, "ca_pem": ca,
                 "client_id": "test-tenant"}
    config = apply_config(transport=transport, mqtt_username=None, mqtt_password=None, tls_certificate=certificate)
    outcome = execute(ctx, "apply_config", {"setup_id": "setup-1", "config": config})
    assert isinstance(outcome, Waiting) and outcome.check() is None
    _status(ctx, setup_id="setup-1")
    assert outcome.check() == Done({"setup_id": "setup-1"})
    key = ctx.paths.transport_key.read_text()
    assert load_raw(ctx.paths)["tls_private_key"] == key  # fuer die Laufzeit gelesen, nicht gespeichert
    assert "tls_private_key" not in ctx.paths.runtime_config.read_text()
    assert "tls_private_key" not in ctx.paths.runtime_secrets.read_text()
    assert "PRIVATE KEY" not in ctx.paths.runtime_config.read_text() + ctx.paths.runtime_secrets.read_text()


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


def test_apply_config_ignores_a_stale_transport_key_for_mosquitto(ctx):
    # create_csr, Einrichten abgebrochen: der Schluessel liegt noch da, die neue Konfiguration nutzt mosquitto_cloudflared
    assert isinstance(execute(ctx, "create_csr", {"tenant_id": "test-tenant"}), Done)
    outcome = execute(ctx, "apply_config", {"setup_id": "setup-1", "config": apply_config()})
    assert isinstance(outcome, Waiting)
    assert "tls_private_key" not in load_raw(ctx.paths)


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
    # Die Fehlermeldung zitiert den ungueltigen Wert; gleicht er einem Geheimnis, erscheint *** statt seiner.
    failed = execute(ctx, "apply_config", {"setup_id": "setup-1", "config": apply_config(thermostat="test-password")})
    assert isinstance(failed, Failed) and failed.grund == "konfiguration_ungueltig"
    assert "thermostat" in failed.text and "***" in failed.text and "test-password" not in failed.text


def test_apply_config_redacts_unexpected_errors(ctx, monkeypatch, caplog):
    def broken(raw, paths):
        raise RuntimeError(f"kaputt bei {raw['mqtt_password']}")

    monkeypatch.setattr(lifecycle, "parse", broken)
    failed = execute(ctx, "apply_config", {"setup_id": "setup-1", "config": apply_config()})
    assert isinstance(failed, Failed) and failed.grund == "konfiguration_ungueltig"
    assert "***" in failed.text and "test-password" not in failed.text
    assert "RuntimeError" in caplog.text and "test-password" not in caplog.text
    assert not ctx.paths.runtime_config.exists()


def _signed_off_with_failed_restore(ctx):
    write_runtime_files(ctx.paths, apply_config())
    write_json(ctx.paths.backup, {"restore_point": {"curve": 1.0, "room_setpoint": 20.0}})
    execute(ctx, "sign_off", {})
    _status(ctx, status="abgemeldet", setup_id=load_raw(ctx.paths)["setup_id"], grund="Zurücksetzen scheitert")


def test_apply_config_after_failed_sign_off_starts_like_a_fresh_install(ctx):
    _signed_off_with_failed_restore(ctx)
    config = apply_config(setup_id="setup-2", tenant_id="test-tenant-2")
    del config["installation_token"]
    rejected = execute(ctx, "apply_config", {"setup_id": "setup-2", "config": config})
    assert isinstance(rejected, Failed) and rejected.grund == "konfiguration_ungueltig"  # alter Token nicht uebernommen
    assert ctx.paths.backup.exists() and load_raw(ctx.paths)["abgemeldet"] is True  # Ablehnung aendert nichts
    # Neue Einrichtung: der Treiber-Login (driver_login) liegt schon vor apply_config bereit und bleibt.
    write_json(ctx.paths.driver_secrets_dir / "simulation.json", {"token": "test-driver-token"}, private=True)
    config = apply_config(setup_id="setup-2", tenant_id="test-tenant-2", installation_token="test-token-2")
    assert isinstance(execute(ctx, "apply_config", {"setup_id": "setup-2", "config": config}), Waiting)
    raw = load_raw(ctx.paths)
    assert raw["setup_id"] == "setup-2" and raw["abgemeldet"] is False and raw["installation_token"] == "test-token-2"
    assert not ctx.paths.runtime_dir.exists()  # keine Wiederherstellungspunkte der alten Anlage
    assert (ctx.paths.driver_secrets_dir / "simulation.json").exists()


def test_remove_setup_for_a_new_setup_keeps_the_new_key_and_driver_login(ctx):
    write_runtime_files(ctx.paths, apply_config(abgemeldet=True))
    write_json(ctx.paths.backup, {"restore_point": {}})
    assert isinstance(execute(ctx, "create_csr", {"tenant_id": "test-tenant-2"}), Done)
    write_json(ctx.paths.driver_secrets_dir / "simulation.json", {"token": "test-driver-token"}, private=True)
    lifecycle.remove_setup(ctx.paths, keep_new_credentials=True)
    assert not ctx.paths.runtime_dir.exists() and not ctx.paths.runtime_secrets.exists()
    assert not ctx.paths.runtime_config.exists()
    assert ctx.paths.transport_key.exists() and (ctx.paths.driver_secrets_dir / "simulation.json").exists()


def test_apply_config_after_failed_sign_off_never_takes_old_secrets(ctx):
    _signed_off_with_failed_restore(ctx)
    config = apply_config(setup_id="setup-2", installation_token="test-token-2")
    del config["mqtt_password"]
    config["cloudflared"] = {k: v for k, v in config["cloudflared"].items() if k != "service_token_secret"}
    rejected = execute(ctx, "apply_config", {"setup_id": "setup-2", "config": config})
    assert isinstance(rejected, Failed) and rejected.grund == "konfiguration_ungueltig"
    config["mqtt_password"] = "test-password-2"
    config["cloudflared"]["service_token_secret"] = "test-secret-2"
    assert isinstance(execute(ctx, "apply_config", {"setup_id": "setup-2", "config": config}), Waiting)
    secrets = ctx.paths.runtime_secrets.read_text()
    assert "test-password-2" in secrets and "test-secret-2" in secrets
    assert "test-password\"" not in secrets and "test-secret\"" not in secrets and "test-token\"" not in secrets


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
    ctx.paths.transport_key.write_text("test-key")  # Abbruch zwischen write_config und dem Loeschen des Schluessels
    outcome = execute(ctx, "sign_off", {})  # Server liefert den Befehl nach einem Agent-Neustart erneut
    assert load_raw(ctx.paths)["setup_id"] == first
    assert not ctx.paths.transport_key.exists()
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


def test_cleanup_interrupted_by_power_loss_completes_on_the_next_pass(ctx, monkeypatch):
    write_runtime_files(ctx.paths, apply_config())
    write_json(ctx.paths.backup, {"restore_point": {}})
    execute(ctx, "sign_off", {})
    _status(ctx, status="abgemeldet", setup_id=load_raw(ctx.paths)["setup_id"], grund=None)
    ctx.paths.transport_key.write_text("test-key")
    write_json(ctx.paths.driver_secrets_dir / "simulation.json", {"token": "test-driver-token"}, private=True)
    real_rmtree = lifecycle.shutil.rmtree

    def power_loss(path, **kwargs):
        if path == ctx.paths.driver_secrets_dir:
            raise OSError("Stromausfall")
        real_rmtree(path, **kwargs)

    monkeypatch.setattr(lifecycle.shutil, "rmtree", power_loss)
    with pytest.raises(OSError):
        lifecycle.cleanup_after_sign_off(ctx)
    # runtime/, Geheimnisse und Schluessel sind weg, die Markierung `abgemeldet` steht noch.
    assert not ctx.paths.runtime_dir.exists() and not ctx.paths.runtime_secrets.exists()
    assert not ctx.paths.transport_key.exists() and load_raw(ctx.paths)["abgemeldet"] is True
    monkeypatch.setattr(lifecycle.shutil, "rmtree", real_rmtree)
    assert lifecycle.cleanup_after_sign_off(ctx)
    assert not ctx.paths.driver_secrets_dir.exists() and not ctx.paths.runtime_config.exists()
    assert ctx.paths.device_dir.exists()
