"""Tunnel-Dienst (Spec SHG G2 6.1): bei Transport mosquitto_cloudflared startet er `cloudflared access tcp` mit
denselben Parametern wie das Add-on cloudflared_access_mqtt (Token in der Umgebung, nicht in argv, AU-034) und startet
es bei neuer setup_id neu; bei IoT Core, abgemeldet oder ohne Konfiguration ruht er. Liest /data nur."""
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
    argv = [
        "cloudflared", "access", "tcp", "--hostname", cloudflared["hostname"], "--url", f"127.0.0.1:{transport['port']}",
    ]
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


def main() -> None:  # pragma: no cover - Container-Einstieg
    logging.basicConfig(level=logging.INFO)
    paths = from_env()
    child: subprocess.Popen | None = None
    running_for: tuple | None = None
    while True:
        raw = load_raw(paths)
        command = tunnel_command(raw)
        key = (raw.get("setup_id"), tuple(command[0]) if command else None)
        if key != running_for or (child is not None and child.poll() is not None):
            if child is not None:
                _stop(child)
            child = None
            if command is not None:
                argv, env = command
                logger.info("Starte cloudflared fuer %s", argv[4])
                child = subprocess.Popen(argv, env={"PATH": "/usr/local/bin:/usr/bin:/bin", **env})
            else:
                logger.info("Tunnel ruht (kein mosquitto_cloudflared-Transport)")
            running_for = key
        time.sleep(CHECK_SECONDS)


if __name__ == "__main__":
    main()
