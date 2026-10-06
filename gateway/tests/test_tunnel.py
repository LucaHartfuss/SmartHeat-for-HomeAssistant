import logging

from configs import apply_config, write_runtime_files

from smartheat_gateway import tunnel
from smartheat_gateway.paths import Paths
from smartheat_gateway.tunnel import TunnelLoop, tunnel_command


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


def test_rests_without_a_port():
    assert tunnel_command(apply_config(transport={"kind": "mosquitto_cloudflared", "host": "127.0.0.1"})) is None


class _Child:
    def __init__(self) -> None:
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


class _Popen:
    def __init__(self, failures: int = 0) -> None:
        self.started: list[tuple[list[str], dict]] = []
        self.children: list[_Child] = []
        self._failures = failures

    def __call__(self, argv, env):
        if self._failures:
            self._failures -= 1
            raise FileNotFoundError("cloudflared")
        self.started.append((argv, env))
        self.children.append(_Child())
        return self.children[-1]


def test_loop_survives_errors_and_retries(data_dir, monkeypatch, caplog):
    paths = Paths(data_dir)
    write_runtime_files(paths, apply_config())
    popen = _Popen(failures=1)
    loop = TunnelLoop(paths, popen=popen)
    real_load_raw = tunnel.load_raw
    calls = []

    def flaky_load_raw(p):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("kaputt")
        return real_load_raw(p)

    monkeypatch.setattr(tunnel, "load_raw", flaky_load_raw)
    with caplog.at_level(logging.ERROR):
        loop.tick()  # load_raw wirft
        loop.tick()  # Popen wirft
    assert popen.started == [] and caplog.text.count("Tunnel: Fehler") == 2
    loop.tick()
    assert len(popen.started) == 1
    loop.tick()
    assert len(popen.started) == 1  # laeuft, keine Aenderung
    popen.children[0].returncode = 1  # cloudflared beendet sich
    loop.tick()
    assert len(popen.started) == 2


def test_loop_restarts_on_a_new_token_with_the_same_setup_id(data_dir):
    paths = Paths(data_dir)
    write_runtime_files(paths, apply_config())
    popen = _Popen()
    loop = TunnelLoop(paths, popen=popen)
    loop.tick()
    cloudflared = {**apply_config()["cloudflared"], "service_token_secret": "test-secret-2"}
    write_runtime_files(paths, apply_config(cloudflared=cloudflared))
    loop.tick()
    assert len(popen.started) == 2 and popen.children[0].returncode is not None
    assert popen.started[1][1]["TUNNEL_SERVICE_TOKEN_SECRET"] == "test-secret-2"
    assert "test-secret-2" not in repr(loop._running_for)
