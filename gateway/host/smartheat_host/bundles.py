"""Bundles des Updaters (Spec G2b-1 2.2, 4): Manifest, Pruefung der Compose-Datei, Ablage unter bundles/<version>/
und Compose-Aufrufe. Ein Bundle = Compose-Datei (nur Images mit Digest) + mosquitto.conf; im Bundle-Ordner liegen
zusaetzlich manifest.json und manifest.json.minisig (Nachweis, was signiert war; exists() verlangt sie nicht, damit
aeltere Ordner gueltig bleiben)."""
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

MANIFEST_FILES = ("docker-compose.yml", "mosquitto.conf")
MANIFEST_NAME = "manifest.json"
SIGNATURE_NAME = "manifest.json.minisig"
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_SHA = re.compile(r"[0-9a-f]{64}")
_IMAGE = re.compile(r"^\s*image:\s*(.*?)\s*$", re.MULTILINE)
_PINNED_IMAGE = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}")
_BUILD = re.compile(r"^\s*build:", re.MULTILINE)
_DEVICES_KEY = re.compile(r"^(\s*)devices:\s*(.*?)\s*$")
_LIST_ITEM = re.compile(r"^(\s*)-\s+(.*?)\s*$")


class ManifestError(Exception):
    pass


class ComposeError(Exception):
    pass


@dataclass(frozen=True)
class Manifest:
    version: str
    min_updater_version: str
    files: dict[str, str]
    images: dict[str, str]


def parse_version(text) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(str(text))
    if not match:
        raise ManifestError(f"Version {text!r} nicht im Format X.Y.Z")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def parse_manifest(raw: bytes) -> Manifest:
    try:
        data = json.loads(raw)
        version, minimum, files, images = (
            data["version"], data["min_updater_version"], data["files"], data["images"],
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
    return Manifest(version, minimum, dict(files), dict(images))


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


def check_compose(text: str) -> None:
    images = compose_images(text)
    if not images or not all(_PINNED_IMAGE.fullmatch(image) for image in images):
        raise ManifestError("Compose-Datei: jedes Image braucht einen Digest")
    if _BUILD.search(text):
        raise ManifestError("Compose-Datei: build: ist im Bundle verboten")


class BundleStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def dir(self, version: str) -> Path:
        return self.root / "bundles" / version

    def exists(self, version: str) -> bool:
        return all((self.dir(version) / name).is_file() for name in MANIFEST_FILES)

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


class ComposeRunner:
    def __init__(self, root: Path, project: str = "smartheat", run=subprocess.run) -> None:
        self._root, self._project, self._run = root, project, run

    def _command(self, version: str, *args: str) -> list[str]:
        compose = self._root / "bundles" / version / "docker-compose.yml"
        return ["docker", "compose", "-p", self._project, "--env-file", str(self._root / "host" / "gateway.env"),
                "-f", str(compose), *args]

    def _compose(self, version: str, *args: str) -> None:
        try:
            result = self._run(self._command(version, *args), capture_output=True, text=True, check=False)
        except OSError as error:
            raise ComposeError(f"{' '.join(args)}: {type(error).__name__}") from None
        if result.returncode != 0:
            raise ComposeError(f"{' '.join(args)}: {(result.stderr or result.stdout).strip()[-500:]}")

    def pull(self, version: str) -> None:
        self._compose(version, "pull")

    def up(self, version: str) -> None:
        self._compose(version, "up", "-d", "--remove-orphans")

    def health(self, version: str) -> dict[str, str]:
        try:
            result = self._run(self._command(version, "ps", "--format", "json"),
                               capture_output=True, text=True, check=False)
        except OSError:
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

    def prune(self, keep: set[str]) -> None:
        """Loescht Images der Repositories aus `keep`, deren Digest nicht in `keep` steht. Verglichen wird ohne Tag
        (Compose nennt eclipse-mosquitto:2@sha256:..., `docker image ls` zeigt eclipse-mosquitto)."""
        kept = {f"{image_repository(ref)}@{ref.split('@', 1)[1]}" for ref in keep if "@" in ref}
        repositories = {image_repository(ref) for ref in keep}
        try:
            listed = self._run(["docker", "image", "ls", "--digests", "--format", "{{.Repository}}@{{.Digest}}"],
                               capture_output=True, text=True, check=False)
            for ref in (listed.stdout or "").split():
                if image_repository(ref) in repositories and ref not in kept and "@sha256:" in ref:
                    self._run(["docker", "image", "rm", ref], capture_output=True, text=True, check=False)
        except OSError:
            return
