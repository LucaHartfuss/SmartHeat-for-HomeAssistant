"""Hoststatus (Spec G2b-1 5, Plan-Praezisierung 1): schreibt alle 60 s SHG_ROOT/host/status.json mit Netz (Default-
Route), DNS (Host der Geraete-API aufloesbar), Zeitsynchronisation und Zeitstempel - die Felder, die die
Diagnoseseite des Agenten liest (HOST_FIELDS: netz, dns, zeit_synchron)."""
import os
import socket
import subprocess
import time
import urllib.parse
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from smartheat_gateway.files import write_json

PERIOD_SECONDS = 60


def _default_route(route_file: Path) -> bool:
    try:
        lines = route_file.read_text().splitlines()[1:]
    except OSError:
        return False
    return any(len(parts) > 2 and parts[1] == "00000000" for parts in (line.split() for line in lines))


def _timedatectl() -> bool | None:
    try:
        result = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                                capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() == "yes" if result.returncode == 0 else None


def collect(*, route_file: Path = Path("/proc/net/route"), resolve: Callable = socket.getaddrinfo,
            api_host: str | None, sync_flag: Path = Path("/run/systemd/timesync/synchronized"),
            timedatectl: Callable[[], bool | None] = _timedatectl,
            now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> dict:
    dns = False
    if api_host:
        try:
            dns = bool(resolve(api_host, 443))
        except OSError:
            dns = False
    synced = sync_flag.exists() or timedatectl() is True
    return {"netz": _default_route(route_file), "dns": dns, "zeit_synchron": synced, "ts": now().isoformat()}


def write(path: Path, data: dict) -> None:
    """Atomar und 0644: der Dienst laeuft als root, der Agent (uid 1000) liest die Datei."""
    write_json(path, data)


def main() -> None:  # pragma: no cover - systemd-Einstieg
    root = Path(os.environ.get("SHG_ROOT", "/var/lib/smartheat"))
    api_host = urllib.parse.urlsplit(os.environ.get("SHG_DEVICE_API_URL", "")).hostname
    while True:
        write(root / "host" / "status.json", collect(api_host=api_host))
        time.sleep(PERIOD_SECONDS)


if __name__ == "__main__":
    main()
