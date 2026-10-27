"""Ablage und Start des Gateways aus dem Image (GPC31). Das Image legt unter /opt/smartheat/werkseinstellung/ nur ab, was
das Geraet braucht (erstes Bundle `bundle/`, Vorlage `state.json`, Container-Archive `images/<SHA-256>.tar`); unter
SHG_ROOT (= `daten`, ueberdeckt alles, was das Image dort haette) steht beim ersten Start nichts.

Zwei Schritte, zwei Units (R10: der Updater darf nicht hinter dem Laden der Archive warten):
- `ablage` (smartheat-ablage.service, jeder Start, nur lokale Dateien, Sekunden): `ordner_angleichen` setzt Ordner,
  Besitzer und Modi unter SHG_ROOT, `anlegen` legt beim allerersten Start das erste Bundle und `updater/state.json` aus der
  Werkseinstellung an (nur ohne `updater/state.json`; danach gehoert der Zustand dem Updater).
- `start` (smartheat-firstboot.service, jeder Start): laedt die Archive, die dieses Geraet noch nicht geladen hat (Merker
  `host/geladen/<SHA-256>`, je Archiv einmal; Archive werden nie geloescht), und startet das laufende Bundle
  (`updater/state.json` "current") mit `compose.up`. Fehlt ein Geraet aus devices: (Zigbee-Stick), endet der Schritt mit
  EXIT_RETRY, bevor etwas geladen wird, und systemd versucht es nach 30 s erneut. Ein offener Updater-Auftrag
  (in_progress oder ein Systemwechsel `system`, auch erst waehrend des Ladens eroeffnet) oder ein Bundle-Wechsel gehoert
  dem Updater: dann nur laden, nicht starten."""
import contextlib
import errno
import logging
import os
import re
import stat
import sys
from collections.abc import Callable
from pathlib import Path

from smartheat_core import dateien

from smartheat_host import bundles, updater

logger = logging.getLogger(__name__)
EXIT_RETRY = 75
WERKSEINSTELLUNG = Path("/opt/smartheat/werkseinstellung")
CONTAINER_UID = 1000  # die Laufzeit und Mosquitto/Zigbee2MQTT schreiben als uid 1000
#: (Ordner unter SHG_ROOT, Modus, Besitzer-uid, Besitzer-gid); die Reihenfolge legt Eltern vor Kindern an. Bind-Quellen der
#: Container muessen vor dem ersten Start mit uid 1000 bestehen: fehlende Quellen legte Docker als root an, der Init-Schritt
#: (uid 1000) koennte dort nichts schreiben.
ORDNER: tuple[tuple[str, int, int, int], ...] = (
    ("", 0o755, 0, 0),
    ("data", 0o755, CONTAINER_UID, CONTAINER_UID),
    ("zigbee2mqtt", 0o700, CONTAINER_UID, CONTAINER_UID),
    ("bus", 0o700, CONTAINER_UID, CONTAINER_UID),
    ("bus/mosquitto", 0o700, CONTAINER_UID, CONTAINER_UID),
    ("bus/mosquitto-daten", 0o700, CONTAINER_UID, CONTAINER_UID),
    ("bus/credentials", 0o700, CONTAINER_UID, CONTAINER_UID),
    ("bus/credentials/runtime", 0o700, CONTAINER_UID, CONTAINER_UID),
    ("bus/credentials/zigbee2mqtt", 0o700, CONTAINER_UID, CONTAINER_UID),
    ("host", 0o755, 0, 0),
    ("host/geladen", 0o755, 0, 0),
    ("bundles", 0o755, 0, 0),
    ("updater", 0o755, 0, 0),
)
_ARCHIV_NAME = re.compile(r"[0-9a-f]{64}\.tar")


class AblageFehler(Exception):
    pass


def _unterordner(eltern_fd: int, name: str) -> int:
    """Oeffnet den Unterordner `name` von `eltern_fd` (legt ihn an, wenn er fehlt), ohne einem Symlink zu folgen."""
    with contextlib.suppress(FileExistsError):
        os.mkdir(name, 0o700, dir_fd=eltern_fd)
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=eltern_fd)
    except OSError as fehler:
        if fehler.errno not in (errno.ELOOP, errno.ENOTDIR):
            raise
        if stat.S_ISLNK(os.lstat(name, dir_fd=eltern_fd).st_mode):
            raise AblageFehler(f"{name} ist ein Symlink") from None
        raise AblageFehler(f"{name} ist kein Ordner") from None


def _wurzel_oeffnen(root: Path) -> int:
    root.mkdir(parents=True, exist_ok=True)
    try:
        return os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as fehler:
        if fehler.errno in (errno.ELOOP, errno.ENOTDIR):
            raise AblageFehler(f"{root} ist ein Symlink oder kein Ordner") from None
        raise


def ordner_angleichen(root: Path, *, fchown: Callable[[int, int, int], None] = os.fchown) -> None:
    """Setzt jeden Ordner aus ORDNER unter `root` auf Modus und Besitzer (legt fehlende an); bei jedem Start, idempotent.
    Die Ordner unter `root` gehoeren zum Teil uid 1000 (Container): ein dort untergeschobener Symlink duerfte den Dienst
    (root) nie auf einen Systempfad umlenken, darum geht jeder Schritt per Dateideskriptor mit O_NOFOLLOW. Ein Ordner mit
    Symlink oder Datei statt Ordner wird nicht angefasst; die uebrigen werden trotzdem angeglichen, danach meldet
    AblageFehler alle Verstoesse."""
    verstoesse = []
    wurzel_fd = _wurzel_oeffnen(root)
    try:
        for rel, modus, uid, gid in ORDNER:
            offen = []
            try:
                fd = wurzel_fd
                for teil in (rel.split("/") if rel else []):
                    fd = _unterordner(fd, teil)
                    offen.append(fd)
                os.fchmod(fd, modus)
                fchown(fd, uid, gid)
            except AblageFehler as fehler:
                verstoesse.append(f"{rel or '.'}: {fehler}")
            finally:
                for fd in offen:
                    os.close(fd)
    finally:
        os.close(wurzel_fd)
    if verstoesse:
        raise AblageFehler("; ".join(verstoesse))


def anlegen(root: Path, werkseinstellung: Path = WERKSEINSTELLUNG) -> bool:
    """Legt beim ersten Start das erste Bundle (`bundles/<Version>/`) und `updater/state.json` aus der Werkseinstellung an.
    Nur ohne `root/updater/state.json`: ein bestehender Zustand bleibt unberuehrt (True: angelegt). Erst das Bundle
    (atomar), zuletzt der Zustand: bricht der Strom dazwischen ab, holt der naechste Start beides nach."""
    ziel = root / "updater" / "state.json"
    if os.path.lexists(ziel):
        return False
    bundle, vorlage = werkseinstellung / "bundle", werkseinstellung / "state.json"
    if not bundle.is_dir() or not vorlage.is_file():
        logger.warning("Keine Werkseinstellung unter %s, kein erstes Bundle angelegt", werkseinstellung)
        return False
    manifest = bundles.parse_manifest((bundle / bundles.MANIFEST_NAME).read_bytes())
    if updater.State.load(vorlage).current != manifest.version:
        raise AblageFehler(f"{vorlage.name}: current nennt nicht die Version {manifest.version} des ersten Bundles")
    namen = [*bundles.MANIFEST_FILES, bundles.MANIFEST_NAME]
    if (bundle / bundles.SIGNATURE_NAME).is_file():  # ein Release-Bundle hat sie, Test-Fixtures nicht
        namen.append(bundles.SIGNATURE_NAME)
    bundles.BundleStore(root).install(manifest.version, {name: (bundle / name).read_bytes() for name in namen})
    dateien.schreiben(ziel, vorlage.read_bytes(), modus=dateien.LESBAR)
    logger.info("Erstes Bundle %s angelegt", manifest.version)
    return True


def _archive(ordner: Path) -> list[Path]:
    """Die Container-Archive der Werkseinstellung, nach Namen sortiert; der Name ist die SHA-256 des Inhalts (der Bau
    prueft das), so braucht der Start kein Hashen. Andere Dateien werden uebergangen."""
    if not ordner.is_dir():
        return []
    gefunden = []
    for pfad in sorted(ordner.iterdir()):
        if _ARCHIV_NAME.fullmatch(pfad.name) and pfad.is_file():
            gefunden.append(pfad)
        else:
            logger.warning("Archiv-Ordner: %s uebergangen (erwartet <SHA-256>.tar)", pfad.name)
    return gefunden


def run(root: Path, compose, werkseinstellung: Path = WERKSEINSTELLUNG, load: Callable[[Path], None] = bundles.docker_load,
        path_exists: Callable[[str], bool] = os.path.exists) -> int:
    archives = _archive(werkseinstellung / "images")
    state = updater.State.load(root / "updater" / "state.json")
    version = None if state.in_progress or state.system else state.current
    if version is not None:
        text = (bundles.BundleStore(root).dir(version) / "docker-compose.yml").read_text()
        missing = [path for path in bundles.compose_devices(text) if not path_exists(path)]
        if missing:
            logger.warning("Start wartet: Geraet fehlt (%s), Zigbee-Stick einstecken", ", ".join(missing))
            return EXIT_RETRY
    for archive in archives:
        merker = root / "host" / "geladen" / archive.stem
        if merker.exists():
            continue
        load(archive)
        dateien.schreiben_text(merker, "geladen\n", modus=dateien.LESBAR)
        logger.info("Image geladen: %s", archive.name)
    if version is not None:
        # Der Updater (startet parallel, nicht hinter diesem Schritt) kann waehrend des Ladens einen Auftrag eroeffnet oder
        # das Bundle gewechselt haben: dann gehoert ihm der Stack, dieser Schritt startet nichts mehr.
        latest = updater.State.load(root / "updater" / "state.json")
        if latest.in_progress or latest.system or latest.current != version:
            logger.info("Updater hat uebernommen (Auftrag oder Bundle-Wechsel), Start startet nichts")
        else:
            compose.up(version)
            logger.info("Bundle %s gestartet", version)
    return 0


def _ablage(root: Path) -> int:
    try:
        ordner_angleichen(root)
        anlegen(root)
    except (AblageFehler, bundles.ManifestError, OSError) as error:
        logger.error("FEHLER: Ablage gescheitert: %s", error)
        return 1
    return 0


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO)
    args = sys.argv[1:] if argv is None else argv
    root = Path(os.environ.get("SHG_ROOT", "/var/lib/smartheat"))
    if args == ["ablage"]:
        sys.exit(_ablage(root))
    if args != ["start"]:
        print("Aufruf: python3 -m smartheat_host.firstboot ablage|start", file=sys.stderr)
        sys.exit(2)
    compose = bundles.ComposeRunner(root)
    try:
        code = run(root, compose)
    except (bundles.LoadError, bundles.ComposeError, OSError) as error:
        logger.error("Start gescheitert: %s", error)
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
