#!/usr/bin/env python3
"""Bundle-Bau: aus der Gateway-Compose (build:) und dem OCI-Archiv des Gateway-Images (buildx `--output type=oci`) die
Bundle-Compose, in der die Gateway-Dienste das Image `smartheat-gateway@<Index-Digest>` mit `pull_policy: never` nennen;
Mosquitto und Zigbee2MQTT behalten ihre gepinnten Digests. Das Archiv kommt mit in den Bundle-Ordner
(gateway-image.oci.tar), nach dem Einbau der Annotation `io.containerd.image.name`: so legt `docker load` auf dem Geraet
das Image unter dem Namen der Compose-Datei an (die Blobs und damit der Index-Digest bleiben, die index.json liegt
ausserhalb der Digests). Manifest mit Version, min_updater_version, SHA-256 der Dateien, Images und dem Feld `archiv`
(Datei, SHA-256, Groesse, Name, Index-Digest). Mit `--system DATEI` kommt das signierte RAUC-System-Bundle (`system.raucb`,
von rauc_signieren.py) dazu: das Manifest bindet dann Datei, SHA-256, Groesse und `compatible` (`system`), und
`min_updater_version` steigt auf 0.9.0 (ein aelterer Updater kennt den Systemwechsel nicht). Geprueft mit denselben
Funktionen wie auf dem Geraet."""
import argparse
import hashlib
import io
import json
import re
import shutil
import sys
import tarfile
from pathlib import Path
from typing import IO, NoReturn

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))  # Aufruf als Skript ohne PYTHONPATH
from smartheat_host import bundles  # noqa: E402

MIN_UPDATER_VERSION = "0.8.0"  # steigt nur, wenn sich das Manifest-Format aendert (0.8.0: Feld archiv)
MIN_UPDATER_VERSION_SYSTEM = "0.9.0"  # Bundles mit Feld `system` (0.9.0: Systemwechsel); der Updater 0.8.0 nimmt die ohne an
GATEWAY_SERVICES = ("init", "runtime")
ANNOTATION = "io.containerd.image.name"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_HEADER = "# SmartHeat-Gateway-Bundle (erzeugt von gateway/release/build_bundle.py, nicht von Hand aendern)\n"


class _FlatDumper(yaml.SafeDumper):
    """Schreibt geteilte Objekte (durch Anker und Merge-Keys entstanden) jedes Mal aus, ohne &id/*id-Aliase."""

    def ignore_aliases(self, data):
        return True


def render_compose(compose_text: str, image_ref: str) -> str:
    data = yaml.safe_load(compose_text)
    for anchor_key in [key for key in data if key.startswith("x-")]:
        data.pop(anchor_key)  # Anker sind nach dem Laden eingeflochten
    for name in GATEWAY_SERVICES:
        service = data["services"][name]
        service.pop("build", None)
        service["image"] = image_ref
        service["pull_policy"] = "never"  # das Image kommt aus dem Archiv; compose pull fragte sonst docker.io
    return _HEADER + yaml.dump(data, Dumper=_FlatDumper, sort_keys=False, allow_unicode=True)


def _fail(message: str) -> NoReturn:
    raise SystemExit(f"Archiv: {message}")


def _open(tar: tarfile.TarFile, name: str) -> IO[bytes]:
    """Der Inhalt des Eintrags `name` als Strom; KeyError, wenn er fehlt oder kein regulaerer Eintrag ist."""
    handle = tar.extractfile(name)
    if handle is None:
        raise KeyError(name)
    return handle


def oci_digest(archive: Path) -> str:
    """Index-Digest eines OCI-Archivs: der Digest des einen Eintrags in index.json. Der Blob dieses Digests muss im Archiv
    liegen und zu ihm passen (SHA-256 ueber den Blob). Wirft SystemExit mit dem Grund."""
    try:
        with tarfile.open(archive) as tar:
            try:
                eintraege = json.loads(_open(tar, "index.json").read())["manifests"]
            except (KeyError, ValueError, TypeError):
                _fail("index.json fehlt oder ist unlesbar")
            if not isinstance(eintraege, list) or len(eintraege) != 1 or not isinstance(eintraege[0], dict):
                _fail("index.json muss genau einen Eintrag nennen")
            digest = eintraege[0].get("digest")
            if not (isinstance(digest, str) and _DIGEST.fullmatch(digest)):
                _fail("Digest in index.json ist kein sha256:<64 Hex>")
            try:
                blob = _open(tar, f"blobs/sha256/{digest[7:]}")
            except KeyError:
                _fail(f"Blob {digest} fehlt")
            hasher = hashlib.sha256()
            while block := blob.read(1 << 20):
                hasher.update(block)
            if f"sha256:{hasher.hexdigest()}" != digest:
                _fail(f"Blob {digest} passt nicht zu seinem Digest")
    except (tarfile.TarError, OSError) as error:
        _fail(f"nicht lesbar ({type(error).__name__})")
    return digest


def write_named_archive(source: Path, target: Path, digest: str) -> None:
    """Schreibt das Archiv nach `target` mit der Annotation, unter der `docker load` das Image als
    `docker.io/library/smartheat-gateway@<digest>` anlegt (so listet und findet der Docker `smartheat-gateway@<digest>`).
    Traegt es den richtigen Namen schon, wird es unveraendert kopiert (der Neubau aus dem Bundle ergibt dieselben Bytes);
    einen anderen Namen lehnt der Bau ab."""
    name = f"docker.io/library/{bundles.ARCHIVE_IMAGE}@{digest}"
    with tarfile.open(source) as old:
        annotations = json.loads(_open(old, "index.json").read())["manifests"][0].get("annotations") or {}
        if ANNOTATION in annotations:
            if annotations[ANNOTATION] != name:
                _fail(f"Name {annotations[ANNOTATION]!r} statt {name!r}")
            shutil.copyfile(source, target)
            return
        with tarfile.open(target, "w", format=tarfile.PAX_FORMAT) as new:
            for member in old:
                if member.name == "index.json":
                    index = json.loads(_open(old, "index.json").read())
                    index["manifests"][0].setdefault("annotations", {})[ANNOTATION] = name
                    data = json.dumps(index, separators=(",", ":")).encode()
                    member.size = len(data)
                    new.addfile(member, io.BytesIO(data))
                else:
                    new.addfile(member, old.extractfile(member) if member.isfile() else None)


def min_updater_version(system: bool) -> str:
    """Die Version, die der Updater des Geraets mindestens haben muss: mit Systemteil 0.9.0, sonst 0.8.0."""
    return MIN_UPDATER_VERSION_SYSTEM if system else MIN_UPDATER_VERSION


def build(compose_path: Path, mosquitto_conf: Path, archive: Path, version: str, out: Path,
          system: Path | None = None) -> dict:
    bundles.parse_version(version)
    digest = oci_digest(archive)
    ref = f"{bundles.ARCHIVE_IMAGE}@{digest}"
    compose = render_compose(compose_path.read_text(), ref)
    neu = not out.exists()
    out.mkdir(parents=True, exist_ok=True)
    pending = out / (bundles.ARCHIVE_NAME + ".tmp")
    pending_system = out / (bundles.SYSTEM_NAME + ".tmp")
    try:
        write_named_archive(archive, pending, digest)
        archiv = bundles.Archiv(bundles.ARCHIVE_NAME, _sha256_file(pending), pending.stat().st_size,
                                bundles.ARCHIVE_IMAGE, digest)
        bundles.check_compose(compose, archiv)
        files = {"docker-compose.yml": compose.encode(), "mosquitto.conf": mosquitto_conf.read_bytes()}
        images = {"gateway": ref}
        for other in bundles.compose_images(compose):
            if other != ref:
                images[other.split("@", 1)[0].split("/")[-1].split(":")[0]] = other
        manifest = {"version": version, "min_updater_version": min_updater_version(system is not None),
                    "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}, "images": images,
                    "archiv": {"datei": archiv.datei, "sha256": archiv.sha256, "bytes": archiv.bytes, "name": archiv.name,
                               "digest": archiv.digest}}
        if system is not None:
            shutil.copyfile(system, pending_system)
            manifest["system"] = {"datei": bundles.SYSTEM_NAME, "sha256": _sha256_file(pending_system),
                                  "groesse": pending_system.stat().st_size, "compatible": bundles.SYSTEM_COMPATIBLE}
        raw = json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"
        bundles.parse_manifest(raw)  # alles pruefen, bevor etwas Endgueltiges liegt
        for name, data in files.items():
            (out / name).write_bytes(data)
        pending.replace(out / bundles.ARCHIVE_NAME)
        if system is not None:
            pending_system.replace(out / bundles.SYSTEM_NAME)
        (out / bundles.MANIFEST_NAME).write_bytes(raw)
    finally:
        pending.unlink(missing_ok=True)
        pending_system.unlink(missing_ok=True)
        if neu and not (out / bundles.MANIFEST_NAME).exists():  # gescheitert: kein halbes Bundle zuruecklassen
            shutil.rmtree(out, ignore_errors=True)
    return manifest


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        while block := handle.read(1 << 20):
            hasher.update(block)
    return hasher.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose", type=Path, required=True)
    parser.add_argument("--mosquitto-conf", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True, help="OCI-Archiv des Gateway-Images (buildx type=oci)")
    parser.add_argument("--system", type=Path, help="signiertes RAUC-System-Bundle (rauc_signieren.py) fuer das Feld `system`")
    parser.add_argument("--version", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    manifest = build(args.compose, args.mosquitto_conf, args.archive, args.version, args.out, args.system)
    print(json.dumps(manifest["files"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
