"""Tunnel-Dienst (Spec SHG G2 6.1): bei Transport mosquitto_cloudflared startet er `cloudflared access tcp` mit
denselben Parametern wie das Add-on cloudflared_access_mqtt (Token in der Umgebung, nicht in argv, AU-034) und startet
es bei neuer setup_id oder neuem Token neu; bei IoT Core, abgemeldet oder ohne Konfiguration ruht er. Liest /data nur.
Der Dienst endet nie: die Laufzeit teilt seinen Netz-Namensraum (network_mode service:tunnel), ein Neustart des
Containers liesse sie in einem toten Namensraum zurueck. Jeder Fehler eines Durchlaufs wird protokolliert, der naechste
Durchlauf versucht es erneut."""
import hashlib
import json
import logging
import subprocess
import time

from smartheat_gateway.config import load_raw
from smartheat_gateway.paths import from_env

logger = logging.getLogger(__name__)
CHECK_SECONDS = 5
_CLOUDFLARED_KEYS = ("hostname", "service_token_id", "service_token_secret")


def tunnel_command(raw: dict) -> tuple[list[str], dict[str, str]] | None:
    transport, cloudflared = raw.get("transport"), raw.get("cloudflared")
    if raw.get("abgemeldet") is True:
        return None
    if not isinstance(transport, dict) or transport.get("kind") != "mosquitto_cloudflared":
        return None
    if not isinstance(cloudflared, dict) or not all(cloudflared.get(key) for key in _CLOUDFLARED_KEYS):
        return None
    port = transport.get("port")
    if port is None:
        return None
    argv = ["cloudflared", "access", "tcp", "--hostname", cloudflared["hostname"], "--url", f"127.0.0.1:{port}"]
    env = {
        "TUNNEL_SERVICE_TOKEN_ID": cloudflared["service_token_id"],
        "TUNNEL_SERVICE_TOKEN_SECRET": cloudflared["service_token_secret"],
    }
    return argv, env


def _stop(child: subprocess.Popen) -> None:
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def restart_key(raw: dict, command: tuple[list[str], dict[str, str]] | None) -> tuple:
    """Neustart bei anderer setup_id, anderem argv oder anderer Umgebung; die Umgebung (Token) nur als Hash, nie im
    Klartext im Speicher des Schluessels."""
    if command is None:
        return (raw.get("setup_id"), None, None)
    argv, env = command
    env_hash = hashlib.sha256(json.dumps(env, sort_keys=True).encode()).hexdigest()
    return (raw.get("setup_id"), tuple(argv), env_hash)


class TunnelLoop:
    def __init__(self, paths, *, popen=subprocess.Popen) -> None:
        self._paths, self._popen = paths, popen
        self._child: subprocess.Popen | None = None
        self._running_for: tuple | None = None

    def tick(self) -> None:
        """Ein Durchlauf; wirft nie."""
        try:
            self._tick()
        except Exception:
            logger.exception("Tunnel: Fehler im Durchlauf, naechster Versuch in %s s", CHECK_SECONDS)

    def _tick(self) -> None:
        raw = load_raw(self._paths)
        command = tunnel_command(raw)
        key = restart_key(raw, command)
        if key == self._running_for and (self._child is None or self._child.poll() is None):
            return
        if self._child is not None:
            _stop(self._child)
        self._child = None
        if command is not None:
            argv, env = command
            logger.info("Starte cloudflared fuer %s", argv[4])
            self._child = self._popen(argv, env={"PATH": "/usr/local/bin:/usr/bin:/bin", **env})
        else:
            logger.info("Tunnel ruht (kein mosquitto_cloudflared-Transport)")
        self._running_for = key


def main() -> None:  # pragma: no cover - Container-Einstieg
    logging.basicConfig(level=logging.INFO)
    loop = TunnelLoop(from_env())
    while True:
        loop.tick()
        time.sleep(CHECK_SECONDS)


if __name__ == "__main__":
    main()
