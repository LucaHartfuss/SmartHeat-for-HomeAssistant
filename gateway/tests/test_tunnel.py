from configs import apply_config

from smartheat_gateway.tunnel import tunnel_command


def test_mosquitto_transport_starts_cloudflared_with_the_token_in_the_environment():
    argv, env = tunnel_command(apply_config())
    assert argv == ["cloudflared", "access", "tcp", "--hostname", "mqtt.example.test", "--url", "127.0.0.1:18830"]
    assert env == {"TUNNEL_SERVICE_TOKEN_ID": "test-id", "TUNNEL_SERVICE_TOKEN_SECRET": "test-secret"}
    assert "test-secret" not in " ".join(argv)


def test_rests_for_iot_core_or_without_configuration():
    iot = apply_config(
        transport={"kind": "iot_core", "host": "x", "port": 8883, "alpn": None, "ca_pem": "x", "client_id": "t"},
    )
    assert tunnel_command(iot) is None
    assert tunnel_command({}) is None
    assert tunnel_command(apply_config(cloudflared=None)) is None
    assert tunnel_command(apply_config(abgemeldet=True)) is None
