#!/usr/bin/env python3
"""Prueft ein Bundle vor dem Signieren: Das Bundle kommt vom Bau-Rechner (oder aus einem Artefakt) und ist Daten, nicht
vertrauenswuerdig. Signiert wird nur, was der getaggte Commit definiert: genau die Dateien manifest.json,
docker-compose.yml, mosquitto.conf und gateway-image.oci.tar (und system.raucb, wenn das Manifest das Feld `system` traegt),
Manifest-Version gleich Release-Version, SHA-256 der Dateien, des Archivs und des System-Bundles wie im Manifest, der Index-Digest im Archiv gleich archiv.digest und images.gateway gleich
smartheat-gateway@<Digest> - und ein Neubau mit build_bundle.py aus dem ausgecheckten Tag mit genau diesem Archiv ergibt
dieselben Bytes fuer alle Dateien (das System-Bundle selbst ist ein signiertes Binaerartefakt des Betreibers: geprueft wird
seine Bindung im Manifest, die Signatur prueft RAUC auf dem Geraet). Damit stimmen auch die Digests von Mosquitto und Zigbee2MQTT mit der Compose-Datei
des Tags ueberein; eine fremde Compose-Datei (privileged, Host-Mounts, andere Images) faellt auf, selbst wenn das Manifest
zu ihr passt. Ob das Archiv wirklich aus dem Quelltext des Tags gebaut wurde, zeigt diese Pruefung nicht (reproduzierbarer
Bau: Restpunkt AP-04-Repro); sie stellt sicher, dass das Manifest zum Archiv passt, das signiert wird.

Aufruf: verify_bundle.py BUNDLE_DIR VERSION [--compose PFAD] [--mosquitto-conf PFAD]
Exit 0 = gueltig, 1 = abgelehnt (Grund auf stderr)."""
import argparse
import hashlib
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))  # Aufruf als Skript ohne PYTHONPATH
from build_bundle import build, oci_digest  # noqa: E402
from smartheat_host import bundles  # noqa: E402

GW = Path(__file__).resolve().parents[1]
COMPOSE = GW / "compose" / "docker-compose.yml"
MOSQUITTO_CONF = GW / "compose" / "mosquitto.conf"
# Reihenfolge: Dateien vor dem Manifest; mit Systemteil kommt bundles.SYSTEM_NAME dazu
EXPECTED_FILES = (*bundles.MANIFEST_FILES, bundles.ARCHIVE_NAME, bundles.MANIFEST_NAME)


class BundleRejected(Exception):
    pass


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        while block := handle.read(1 << 20):
            hasher.update(block)
    return hasher.hexdigest()


def _same_bytes(a: Path, b: Path) -> bool:
    with open(a, "rb") as first, open(b, "rb") as second:
        while True:
            block = first.read(1 << 20)
            if block != second.read(1 << 20):
                return False
            if not block:
                return True


def verify(bundle: Path, version: str, compose: Path = COMPOSE, mosquitto_conf: Path = MOSQUITTO_CONF) -> None:
    found = sorted(path.name for path in bundle.iterdir())
    try:
        manifest = bundles.parse_manifest((bundle / bundles.MANIFEST_NAME).read_bytes())
        error = None
    except (OSError, bundles.ManifestError) as caught:
        manifest, error = None, caught
    expected = (*EXPECTED_FILES, bundles.SYSTEM_NAME) if manifest is not None and manifest.system else EXPECTED_FILES
    if found != sorted(expected):
        raise BundleRejected(f"Bundle enthaelt {found}, erwartet {sorted(expected)}")
    if manifest is None:
        raise BundleRejected(f"Manifest: {error}")
    if manifest.version != version:
        raise BundleRejected(f"Manifest-Version {manifest.version} passt nicht zur Release-Version {version}")
    for name, sha in manifest.files.items():
        if hashlib.sha256((bundle / name).read_bytes()).hexdigest() != sha:
            raise BundleRejected(f"{name}: SHA-256 passt nicht zum Manifest")
    archive = bundle / bundles.ARCHIVE_NAME
    if archive.stat().st_size != manifest.archiv.bytes or _sha256(archive) != manifest.archiv.sha256:
        raise BundleRejected("Archiv: SHA-256 oder Groesse passt nicht zum Manifest")
    try:
        digest = oci_digest(archive)
    except SystemExit as error:
        raise BundleRejected(str(error)) from None
    if digest != manifest.archiv.digest:
        raise BundleRejected(f"Archiv: Index-Digest {digest} passt nicht zu archiv.digest {manifest.archiv.digest}")
    if manifest.images.get("gateway") != manifest.archiv.ref:
        raise BundleRejected(f"images.gateway {manifest.images.get('gateway')!r} ist nicht {manifest.archiv.ref}")
    if manifest.system is not None:
        system = bundle / bundles.SYSTEM_NAME
        if system.stat().st_size != manifest.system.groesse or _sha256(system) != manifest.system.sha256:
            raise BundleRejected("System-Bundle: SHA-256 oder Groesse passt nicht zum Manifest")
    with tempfile.TemporaryDirectory() as tmp:
        try:
            build(compose, mosquitto_conf, archive, version, Path(tmp) / "neu",
                  bundle / bundles.SYSTEM_NAME if manifest.system is not None else None)
        except (SystemExit, bundles.ManifestError) as error:
            raise BundleRejected(f"Neubau aus dem Tag gescheitert: {error}") from None
        for name in expected:
            if not _same_bytes(Path(tmp) / "neu" / name, bundle / name):
                raise BundleRejected(f"{name} weicht vom Neubau aus dem Tag ab")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("version")
    parser.add_argument("--compose", type=Path, default=COMPOSE)
    parser.add_argument("--mosquitto-conf", type=Path, default=MOSQUITTO_CONF)
    args = parser.parse_args(argv)
    try:
        verify(args.bundle, args.version, args.compose, args.mosquitto_conf)
    except BundleRejected as error:
        print(f"Bundle abgelehnt: {error}", file=sys.stderr)
        return 1
    print(f"Bundle {args.version} entspricht dem Neubau aus dem Tag")
    return 0


if __name__ == "__main__":
    sys.exit(main())
