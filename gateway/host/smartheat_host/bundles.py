"""Bundles des Updaters: Manifest, Pruefung der Compose-Datei, Ablage unter bundles/<version>/
und Compose-Aufrufe. Ein Bundle = Compose-Datei (nur Images mit Digest) + mosquitto.conf; im Bundle-Ordner liegen
zusaetzlich manifest.json und manifest.json.sig (Nachweis, was signiert war; exists() verlangt sie nicht, damit
aeltere Ordner gueltig bleiben). Das eigene Container-Image kommt als OCI-Archiv neben dem Bundle (Manifest-Feld
`archiv`, Ablage archive/<version>.oci.tar ausserhalb von bundles/, das install() loescht): das Geraet laedt es mit
`docker load` und startet es ueber `smartheat-gateway@<Index-Digest>` mit `pull_policy: never`, ohne Registry. Ein Bundle
kann zusaetzlich ein System-Bundle (RAUC) tragen (optionales Manifest-Feld `system`, Ablage
updater/system/<version>.raucb ausserhalb von bundles/): Datei, SHA-256, Groesse und `compatible` stehen im signierten
Manifest, das Geraet vergleicht `compatible` mit dem seiner system.conf."""
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

MANIFEST_FILES = ("docker-compose.yml", "mosquitto.conf")
MANIFEST_NAME = "manifest.json"
SIGNATURE_NAME = "manifest.json.sig"
ARCHIVE_NAME = "gateway-image.oci.tar"  # Dateiname des Archivs im Bundle der Betreiber und im Manifest
ARCHIVE_IMAGE = "smartheat-gateway"  # Name des eigenen Images (Teil von name@digest in der Compose-Datei)
MAX_ARCHIVE_BYTES = 536870912  # 512 MiB
SYSTEM_NAME = "system.raucb"  # Dateiname des System-Bundles im Bundle der Betreiber und im Manifest
SYSTEM_COMPATIBLE = "smartheat-gateway-pi5"  # `compatible` der system.conf des Images (rauc/system.conf.in)
MAX_SYSTEM_BYTES = 2147483648  # 2 GiB
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_SHA = re.compile(r"[0-9a-f]{64}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_COMPATIBLE = re.compile(r"[A-Za-z0-9._-]{1,64}")
_IMAGE = re.compile(r"^\s*image:\s*(.*?)\s*$", re.MULTILINE)
_PINNED_IMAGE = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}")
_BUILD = re.compile(r"^\s*build:", re.MULTILINE)
_DEVICES_KEY = re.compile(r"^(\s*)devices:\s*(.*?)\s*$")
_LIST_ITEM = re.compile(r"^(\s*)-\s+(.*?)\s*$")
#: Frist je docker-Aufruf in Sekunden: ohne sie hielte ein haengender Aufruf (Netz weg mitten im pull, Daemon
#: blockiert) den ganzen Updater endlos fest. pull ist lang (mehrere Images ueber eine langsame
#: Leitung; ein Image von rund 210 MB braucht bei 2 Mbit/s etwa 850 s, fertige Schichten bleiben beim naechsten Versuch), ps kurz.
PULL_TIMEOUT_SECONDS = 900
UP_TIMEOUT_SECONDS = 300
PS_TIMEOUT_SECONDS = 60
IMAGE_TIMEOUT_SECONDS = 120
LOAD_TIMEOUT_SECONDS = 600  # docker load: ein Archiv von rund 200 MB entpackt auf einem Pi 4 in ein bis drei Minuten


class ManifestError(Exception):
    pass


class ComposeError(Exception):
    pass


class LoadError(Exception):
    pass


@dataclass(frozen=True)
class Archiv:
    """Das OCI-Archiv des eigenen Images laut Manifest: Datei im Bundle-Ordner des Anbieters, SHA-256 und Groesse der
    Datei, Name des Images und Digest des Image-Index im Archiv (`sha256:<64 Hex>`)."""
    datei: str
    sha256: str
    bytes: int
    name: str
    digest: str

    @property
    def ref(self) -> str:
        """Wie die Compose-Datei das Image nennt."""
        return f"{self.name}@{self.digest}"


@dataclass(frozen=True)
class SystemBundle:
    """Das RAUC-System-Bundle laut Manifest: Datei im Bundle-Ordner des Anbieters, SHA-256 und Groesse der Datei und das
    `compatible`, das die system.conf des Geraets verlangt."""
    datei: str
    sha256: str
    groesse: int
    compatible: str


@dataclass(frozen=True)
class Manifest:
    version: str
    min_updater_version: str
    files: dict[str, str]
    images: dict[str, str]
    archiv: Archiv
    system: SystemBundle | None = None


def parse_version(text) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(str(text))
    if not match:
        raise ManifestError(f"Version {text!r} nicht im Format X.Y.Z")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def parse_manifest(raw: bytes) -> Manifest:
    try:
        data = json.loads(raw)
        version, minimum, files, images, archiv = (
            data["version"], data["min_updater_version"], data["files"], data["images"], data["archiv"],
        )
    except (ValueError, KeyError, TypeError):
        raise ManifestError("Manifest unlesbar oder unvollstaendig") from None
    parse_version(version)
    parse_version(minimum)
    if not isinstance(files, dict) or set(files) != set(MANIFEST_FILES):
        raise ManifestError(f"Manifest muss genau {', '.join(MANIFEST_FILES)} nennen")
    if not all(isinstance(sha, str) and _SHA.fullmatch(sha) for sha in files.values()):
        raise ManifestError("Manifest: SHA-256 der Dateien ungueltig")
    if not isinstance(images, dict) or not all(isinstance(ref, str) and "@sha256:" in ref for ref in images.values()):
        raise ManifestError("Manifest: Images ohne Digest")
    system = _parse_system(data["system"]) if "system" in data else None
    return Manifest(version, minimum, dict(files), dict(images), _parse_archiv(archiv), system)


def _parse_archiv(value) -> Archiv:
    keys = ("datei", "sha256", "bytes", "name", "digest")
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ManifestError(f"Manifest: archiv muss genau {', '.join(keys)} nennen")
    datei, sha, size, name, digest = (value[key] for key in keys)
    if datei != ARCHIVE_NAME:
        raise ManifestError(f"Manifest: archiv.datei muss {ARCHIVE_NAME} sein")
    if not (isinstance(sha, str) and _SHA.fullmatch(sha)):
        raise ManifestError("Manifest: SHA-256 des Archivs ungueltig")
    if not (isinstance(size, int) and not isinstance(size, bool) and 0 < size <= MAX_ARCHIVE_BYTES):
        raise ManifestError(f"Manifest: archiv.bytes muss zwischen 1 und {MAX_ARCHIVE_BYTES} liegen")
    if name != ARCHIVE_IMAGE:
        raise ManifestError(f"Manifest: archiv.name muss {ARCHIVE_IMAGE} sein")
    if not (isinstance(digest, str) and _DIGEST.fullmatch(digest)):
        raise ManifestError("Manifest: archiv.digest muss sha256:<64 Hex> sein")
    return Archiv(datei, sha, size, name, digest)


def _parse_system(value) -> SystemBundle:
    keys = ("datei", "sha256", "groesse", "compatible")
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ManifestError(f"Manifest: system muss genau {', '.join(keys)} nennen")
    datei, sha, size, compatible = (value[key] for key in keys)
    if datei != SYSTEM_NAME:
        raise ManifestError(f"Manifest: system.datei muss {SYSTEM_NAME} sein")
    if not (isinstance(sha, str) and _SHA.fullmatch(sha)):
        raise ManifestError("Manifest: SHA-256 des System-Bundles ungueltig")
    if not (isinstance(size, int) and not isinstance(size, bool) and 0 < size <= MAX_SYSTEM_BYTES):
        raise ManifestError(f"Manifest: system.groesse muss zwischen 1 und {MAX_SYSTEM_BYTES} liegen")
    if not (isinstance(compatible, str) and _COMPATIBLE.fullmatch(compatible)):
        raise ManifestError("Manifest: system.compatible ungueltig")
    return SystemBundle(datei, sha, size, compatible)


def compose_images(text: str) -> list[str]:
    """Alle Werte von image: (ohne Anfuehrungszeichen); ein Kommentar am Zeilenende bleibt im Wert und faellt so bei
    check_compose durch."""
    return [value.strip("\"'") for value in _IMAGE.findall(text)]


def _strip_comment(value: str) -> str:
    return re.sub(r"\s+#.*$", "", value).strip()


def compose_devices(text: str) -> list[str]:
    """Host-Pfade aus `devices:` aller Dienste (vor dem ersten ":"), ohne yaml (der Host hat keins). Erkannt werden die
    Formen, die Compose-Datei und Bundle-Bau erzeugen: Blockliste (`- /host:/ctr[:rechte]`, auf gleicher oder tieferer
    Einrueckung als der Schluessel) und Flussliste in einer Zeile (`devices: ["/host:/ctr"]`). Grenzen: Eintraege mit
    Variablen (`${...}`) oder ohne absoluten Pfad (CDI-Namen) werden uebergangen, mehrzeilige Flusslisten nicht erkannt."""
    values: list[str] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        match = _DEVICES_KEY.match(lines[index])
        index += 1
        if match is None:
            continue
        indent, rest = len(match.group(1)), _strip_comment(match.group(2))
        if rest.startswith("[") and rest.endswith("]"):
            values += [item.strip() for item in rest[1:-1].split(",") if item.strip()]
            continue
        while index < len(lines):
            line = lines[index]
            if not line.strip() or line.lstrip().startswith("#"):
                index += 1
                continue
            item = _LIST_ITEM.match(line)
            if item is None or len(item.group(1)) < indent:
                break
            values.append(_strip_comment(item.group(2)))
            index += 1
    paths = []
    for value in values:
        host = value.strip("\"'").split(":", 1)[0]
        if host.startswith("/") and "${" not in host:
            paths.append(host)
    return paths


def image_repository(ref: str) -> str:
    """Repository einer Image-Angabe wie `docker image ls` es zeigt: ohne Digest und ohne Tag. Der Tag steht nach dem
    letzten "/" (ein ":" davor gehoert zum Registry-Port, z. B. localhost:5000/name)."""
    name = ref.split("@", 1)[0]
    head, slash, last = name.rpartition("/")
    return head + slash + last.split(":", 1)[0]


def compose_services(text: str) -> dict[str, dict[str, str]]:
    """Je Dienst die Werte von `image` und `pull_policy` (nur Schluessel auf der ersten Ebene eines Dienstes, ohne
    Anfuehrungszeichen und Zeilenkommentar), ohne yaml wie compose_devices. Erkannt wird die Blockform, die Bundle-Bau
    und Compose-Datei erzeugen; ein Dienst in Flussform (`name: {image: x}`) hat keine Werte."""
    services: dict[str, dict[str, str]] = {}
    in_services, service_indent, key_indent, current = False, None, None, None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            in_services, service_indent, key_indent, current = _strip_comment(stripped) == "services:", None, None, None
            continue
        if not in_services:
            continue
        if service_indent is None:
            service_indent = indent
        if indent == service_indent:
            name = _strip_comment(stripped)
            current = name[:-1].strip() if name.endswith(":") else None
            key_indent = None
            if current is not None:
                services[current] = {}
            continue
        if current is None or indent < service_indent:
            continue
        if key_indent is None:
            key_indent = indent
        key, separator, value = stripped.partition(":")
        if indent == key_indent and separator and key in ("image", "pull_policy"):
            services[current][key] = _strip_comment(value).strip("\"'")
    return services


def check_compose(text: str, archiv: Archiv) -> None:
    """Jedes Image mit Digest, kein build:. Das eigene Image (`name@digest` des Archivs) kommt nur aus dem Archiv und
    steht darum mit `pull_policy: never` in der Datei (compose pull fragte sonst docker.io); umgekehrt ist `never`
    nur fuer dieses Image erlaubt."""
    images = compose_images(text)
    if not images or not all(_PINNED_IMAGE.fullmatch(image) for image in images):
        raise ManifestError("Compose-Datei: jedes Image braucht einen Digest")
    if _BUILD.search(text):
        raise ManifestError("Compose-Datei: build: ist im Bundle verboten")
    for name, service in compose_services(text).items():
        image, never = service.get("image", ""), service.get("pull_policy") == "never"
        own = image_repository(image) == archiv.name
        if never and image != archiv.ref:
            raise ManifestError(f"Compose-Datei: Dienst {name}: pull_policy never nur fuer {archiv.ref}")
        if own and not (never and image == archiv.ref):
            raise ManifestError(f"Compose-Datei: Dienst {name}: {archiv.name} nur als {archiv.ref} mit pull_policy never")


class BundleStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def dir(self, version: str) -> Path:
        return self.root / "bundles" / version

    def exists(self, version: str) -> bool:
        return all((self.dir(version) / name).is_file() for name in MANIFEST_FILES)

    def archive_path(self, version: str) -> Path:
        """Ablage des Image-Archivs bis zum Laden; neben bundles/, weil install() den Ordner einer Version loescht."""
        return self.root / "archive" / f"{version}.oci.tar"

    def system_path(self, version: str) -> Path:
        """Ablage des System-Bundles bis zur Bestaetigung (oder Ablehnung, Rueckfall); unter updater/, nicht unter archive/,
        weil RAUC die Datei liest (root, nicht gruppen- oder fremdschreibbar)."""
        return self.root / "updater" / "system" / f"{version}.raucb"

    def clean_system(self, version: str | None) -> None:
        """Entfernt alle Dateien in updater/system/ ausser dem System-Bundle (und seiner .part-Datei) der Version, die gerade
        geholt wird (`None`: alle)."""
        keep = set() if version is None else {self.system_path(version).name, self.system_path(version).name + ".part"}
        folder = self.root / "updater" / "system"
        if not folder.is_dir():
            return
        for path in folder.iterdir():
            if path.is_file() and path.name not in keep:
                path.unlink()

    def clean_archives(self, version: str | None) -> None:
        """Entfernt alle Dateien in archive/ ausser dem Archiv (und seiner .part-Datei) der Version, die gerade geholt
        wird (`None`: alle): Reste frueherer, nie fertig gewordener Versionen fuellten sonst den Datentraeger."""
        keep = set() if version is None else {self.archive_path(version).name, self.archive_path(version).name + ".part"}
        folder = self.root / "archive"
        if not folder.is_dir():
            return
        for path in folder.iterdir():
            if path.is_file() and path.name not in keep:
                path.unlink()

    def install(self, version: str, files: dict[str, bytes]) -> Path:
        """Schreibt erst in einen temporaeren Ordner (fsync je Datei), dann ein Rename: ein halbes Bundle bleibt nie
        unter bundles/<version>/ liegen."""
        target = self.dir(version)
        tmp = target.with_name(f".tmp-{version}")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        for name, data in files.items():
            with open(tmp / name, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        if target.exists():
            shutil.rmtree(target)
        os.replace(tmp, target)
        return target


def docker_load(archive: Path, timeout: float = LOAD_TIMEOUT_SECONDS) -> None:
    """Laedt ein OCI-Archiv in den Docker des Geraets (containerd-Speicher, docker-daemon.json): kein Netz, kein Pull.
    Das Archiv benennt sein Image selbst (Annotation io.containerd.image.name, build_bundle.py)."""
    try:
        result = subprocess.run(["docker", "load", "-i", str(archive)], capture_output=True, text=True, check=False,
                                timeout=timeout)
    except OSError as error:
        raise LoadError(f"{archive.name}: {type(error).__name__}") from None
    except subprocess.TimeoutExpired:  # subprocess.run beendet den Prozess selbst
        raise LoadError(f"{archive.name}: Frist von {timeout:g} s ueberschritten") from None
    if result.returncode != 0:
        raise LoadError(f"{archive.name}: {(result.stderr or result.stdout).strip()[-300:]}")


COMPOSE_PROJECT = "smartheat"  # Projektname von install.sh (`docker compose -p smartheat`), Updater und Erststart


class ComposeRunner:
    def __init__(self, root: Path, project: str = COMPOSE_PROJECT, run=subprocess.run) -> None:
        self._root, self._project, self._run = root, project, run

    def _command(self, version: str, *args: str) -> list[str]:
        compose = self._root / "bundles" / version / "docker-compose.yml"
        return ["docker", "compose", "-p", self._project, "--env-file", str(self._root / "host" / "gateway.env"),
                "-f", str(compose), *args]

    def _compose(self, version: str, *args: str, timeout: float) -> None:
        try:
            result = self._run(self._command(version, *args), capture_output=True, text=True, check=False,
                               timeout=timeout)
        except OSError as error:
            raise ComposeError(f"{' '.join(args)}: {type(error).__name__}") from None
        except subprocess.TimeoutExpired:  # subprocess.run beendet den Prozess selbst
            raise ComposeError(f"{' '.join(args)}: Frist von {timeout:g} s ueberschritten") from None
        if result.returncode != 0:
            raise ComposeError(f"{' '.join(args)}: {(result.stderr or result.stdout).strip()[-500:]}")

    def pull(self, version: str) -> None:
        self._compose(version, "pull", timeout=PULL_TIMEOUT_SECONDS)

    def up(self, version: str) -> None:
        self._compose(version, "up", "-d", "--remove-orphans", timeout=UP_TIMEOUT_SECONDS)

    def health(self, version: str) -> dict[str, str]:
        try:
            result = self._run(self._command(version, "ps", "--format", "json"),
                               capture_output=True, text=True, check=False, timeout=PS_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            return {}
        if result.returncode != 0:
            return {}
        text = (result.stdout or "").strip()
        try:  # Compose gibt je nach Version ein Array oder ein Objekt je Zeile aus
            rows = json.loads(text) if text.startswith("[") else [json.loads(line) for line in text.splitlines() if line]
        except ValueError:
            return {}
        return {row.get("Service", ""): row.get("Health", "")
                for row in rows if isinstance(row, dict) and row.get("Health")}

    def has_image(self, ref: str) -> bool:
        """Kennt der Docker des Geraets das Image `ref` (name@digest)? Ein Fehler oder eine abgelaufene Frist gilt als nein."""
        try:
            result = self._run(["docker", "image", "inspect", ref], capture_output=True, text=True, check=False,
                               timeout=IMAGE_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def prune(self, keep: set[str]) -> None:
        """Loescht Images der Repositories aus `keep`, deren Digest nicht in `keep` steht. Verglichen wird ohne Tag
        (Compose nennt eclipse-mosquitto:2@sha256:..., `docker image ls` zeigt eclipse-mosquitto)."""
        kept = {f"{image_repository(ref)}@{ref.split('@', 1)[1]}" for ref in keep if "@" in ref}
        repositories = {image_repository(ref) for ref in keep}
        try:
            listed = self._run(["docker", "image", "ls", "--digests", "--format", "{{.Repository}}@{{.Digest}}"],
                               capture_output=True, text=True, check=False, timeout=IMAGE_TIMEOUT_SECONDS)
            for ref in (listed.stdout or "").split():
                if image_repository(ref) in repositories and ref not in kept and "@sha256:" in ref:
                    self._run(["docker", "image", "rm", ref], capture_output=True, text=True, check=False,
                              timeout=IMAGE_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):  # Aufraeumen ist Beiwerk: abbrechen, nicht scheitern
            return
