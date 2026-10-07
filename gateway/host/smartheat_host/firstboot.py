"""Erststart eines Gateway-Images (Plan G2b-2 Task 7): laedt die im Image abgelegten Container-Images
(images/*.tar, docker load, kein Pull), startet das laufende Bundle (updater/state.json "current") und loescht danach
die Archive. Die Unit smartheat-firstboot laeuft nur, solange images/ nicht leer ist (ConditionDirectoryNotEmpty).
Fehlt ein Geraet aus devices: (Zigbee-Stick), endet sie mit EXIT_RETRY, bevor etwas geladen wird, und systemd versucht
es nach 30 s erneut. Ein offener Updater-Auftrag (in_progress, auch erst waehrend des Ladens eroeffnet) oder ein
Bundle-Wechsel gehoert dem Updater: dann nur laden, nicht starten."""
import logging
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from smartheat_host import bundles, updater

logger = logging.getLogger(__name__)
EXIT_RETRY = 75


class LoadError(Exception):
    pass


def docker_load(archive: Path) -> None:
    try:
        result = subprocess.run(["docker", "load", "-i", str(archive)], capture_output=True, text=True, check=False)
    except OSError as error:
        raise LoadError(f"{archive.name}: {type(error).__name__}") from None
    if result.returncode != 0:
        raise LoadError(f"{archive.name}: {(result.stderr or result.stdout).strip()[-300:]}")


def run(root: Path, compose, load: Callable[[Path], None] = docker_load,
        path_exists: Callable[[str], bool] = os.path.exists) -> int:
    archives = sorted((root / "images").glob("*.tar"))
    if not archives:
        return 0
    state = updater.State.load(root / "updater" / "state.json")
    version = None if state.in_progress else state.current
    if version is not None:
        text = (bundles.BundleStore(root).dir(version) / "docker-compose.yml").read_text()
        missing = [path for path in bundles.compose_devices(text) if not path_exists(path)]
        if missing:
            logger.warning("Erststart wartet: Geraet fehlt (%s), Zigbee-Stick einstecken", ", ".join(missing))
            return EXIT_RETRY
    for archive in archives:
        load(archive)
        logger.info("Image geladen: %s", archive.name)
    if version is not None:
        # Der Updater (startet parallel nach dem Laden der Unit) kann waehrend des Ladens einen Auftrag eroeffnet oder
        # das Bundle gewechselt haben: dann gehoert ihm der Stack, der Erststart startet nichts mehr.
        latest = updater.State.load(root / "updater" / "state.json")
        if latest.in_progress or latest.current != version:
            logger.info("Updater hat uebernommen (Auftrag oder Bundle-Wechsel), Erststart startet nichts")
        else:
            compose.up(version)
            logger.info("Bundle %s gestartet", version)
    for archive in archives:
        archive.unlink()
    return 0


def main() -> None:  # pragma: no cover - systemd-Einstieg
    logging.basicConfig(level=logging.INFO)
    root = Path(os.environ.get("SHG_ROOT", "/var/lib/smartheat"))
    compose = bundles.ComposeRunner(root, project=os.environ.get("SHG_COMPOSE_PROJECT", "smartheat"))
    try:
        code = run(root, compose)
    except (LoadError, bundles.ComposeError, OSError) as error:
        logger.error("Erststart gescheitert: %s", error)
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
