"""Einstieg des Updaters fuer den Docker-Test (tests/test_gateway_updater.sh): derselbe `smartheat_host.updater.main`
wie auf dem Geraet, aber mit eigenen `Einstellungen` (http erlaubt, Gesundheitsseite im Test-Netz, eigener Schluessel,
eigenes Compose-Projekt). Nur hier ist `allow_http` einzuschalten: der Dienst (`python3 -m smartheat_host.updater`)
ruft `main` ohne Einstellungen und liest keine Umgebungsvariable dafuer; `install.sh` kopiert nur `smartheat_host/*.py`,
nie `host/tests`. `SHG_ROOT` bleibt Umgebung. Aufruf: python3 updater_testlauf.py --release-pub ... --once"""
import argparse
import sys
from pathlib import Path

from smartheat_host import updater


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-pub", type=Path, required=True)
    parser.add_argument("--health-url", required=True)
    parser.add_argument("--health-seconds", type=float, required=True)
    parser.add_argument("--compose-project", required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    updater.main(
        ["--once"] if args.once else [],
        einstellungen=updater.Einstellungen(
            release_pub=args.release_pub, allow_http=True, health_url=args.health_url,
            health_seconds=args.health_seconds, compose_project=args.compose_project),
    )


if __name__ == "__main__":
    main()
