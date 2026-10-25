"""Einstieg des Updaters fuer den Docker-Test (tests/test_gateway_updater.sh): derselbe `smartheat_host.updater.main`
wie auf dem Geraet, aber mit eigenen `Einstellungen` (http erlaubt, Gesundheitsseite im Test-Netz, eigener Schluessel,
eigenes Compose-Projekt). Die Gesundheitsadresse ist die des Geraets (`Einstellungen().health_url`) mit dem Dienst des
Test-Netzes statt `127.0.0.1`: der Test prueft so den Standardwert, nicht eine eigene Adresse. Nur hier ist `allow_http` einzuschalten: der Dienst (`python3 -m smartheat_host.updater`)
ruft `main` ohne Einstellungen und liest keine Umgebungsvariable dafuer; `install.sh` kopiert nur `smartheat_host/*.py`,
nie `host/tests`. `SHG_ROOT` bleibt Umgebung. Aufruf: python3 updater_testlauf.py --release-pub ... --once"""
import argparse
import sys
from pathlib import Path

from smartheat_host import updater


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-pub", type=Path, required=True)
    parser.add_argument("--health-host", required=True, help="Host im Test-Netz statt 127.0.0.1 in der Standardadresse")
    parser.add_argument("--health-seconds", type=float, required=True)
    parser.add_argument("--compose-project", required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    standard = updater.Einstellungen().health_url
    health_url = standard.replace("127.0.0.1", args.health_host, 1)
    if health_url == standard:
        parser.error("die Standardadresse enthaelt 127.0.0.1 nicht mehr")
    updater.main(
        ["--once"] if args.once else [],
        einstellungen=updater.Einstellungen(
            release_pub=args.release_pub, allow_http=True, health_url=health_url,
            health_seconds=args.health_seconds, compose_project=args.compose_project),
    )


if __name__ == "__main__":
    main()
