#!/usr/bin/env python3
"""Prueft ein Bundle vor dem Signieren (Spec G2b-1 7, Final-Review FW-4): Das Bundle kommt als Artefakt aus dem
Build-Job und ist Daten, nicht vertrauenswuerdig. Signiert wird nur, was der getaggte Commit definiert: genau die
Dateien manifest.json, docker-compose.yml und mosquitto.conf, Manifest-Version gleich Release-Version, SHA-256 der
Dateien wie im Manifest, images.gateway aus dem erwarteten Repository mit Digest - und ein Neubau mit build_bundle.py
aus dem ausgecheckten Tag mit genau diesem Gateway-Digest ergibt dieselben Bytes fuer alle drei Dateien. Damit stimmen
auch die Digests von Mosquitto und Zigbee2MQTT mit der Compose-Datei des Tags ueberein; eine fremde Compose-Datei
(privileged, Host-Mounts, andere Images) faellt auf, selbst wenn das Manifest zu ihr passt.

Aufruf: verify_bundle.py BUNDLE_DIR VERSION --image-repo REPO [--compose PFAD] [--mosquitto-conf PFAD]
Exit 0 = gueltig, 1 = abgelehnt (Grund auf stderr)."""
import argparse
import hashlib
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))  # Aufruf als Skript ohne PYTHONPATH
from build_bundle import build  # noqa: E402
from smartheat_host import bundles  # noqa: E402

GW = Path(__file__).resolve().parents[1]
COMPOSE = GW / "compose" / "docker-compose.yml"
MOSQUITTO_CONF = GW / "compose" / "mosquitto.conf"
EXPECTED_FILES = (*bundles.MANIFEST_FILES, bundles.MANIFEST_NAME)  # Reihenfolge: Dateien vor dem Manifest


class BundleRejected(Exception):
    pass


def verify(bundle: Path, version: str, image_repo: str, compose: Path = COMPOSE,
           mosquitto_conf: Path = MOSQUITTO_CONF) -> None:
    found = sorted(path.name for path in bundle.iterdir())
    if found != sorted(EXPECTED_FILES):
        raise BundleRejected(f"Bundle enthaelt {found}, erwartet {sorted(EXPECTED_FILES)}")
    try:
        manifest = bundles.parse_manifest((bundle / bundles.MANIFEST_NAME).read_bytes())
    except bundles.ManifestError as error:
        raise BundleRejected(f"Manifest: {error}") from None
    if manifest.version != version:
        raise BundleRejected(f"Manifest-Version {manifest.version} passt nicht zur Release-Version {version}")
    for name, sha in manifest.files.items():
        if hashlib.sha256((bundle / name).read_bytes()).hexdigest() != sha:
            raise BundleRejected(f"{name}: SHA-256 passt nicht zum Manifest")
    image = manifest.images.get("gateway", "")
    if not image.startswith(image_repo + "@sha256:"):
        raise BundleRejected(f"images.gateway {image!r} stammt nicht aus {image_repo}")
    with tempfile.TemporaryDirectory() as tmp:
        try:
            build(compose, mosquitto_conf, image, version, Path(tmp))
        except (SystemExit, bundles.ManifestError) as error:
            raise BundleRejected(f"Neubau aus dem Tag gescheitert: {error}") from None
        for name in EXPECTED_FILES:
            if (Path(tmp) / name).read_bytes() != (bundle / name).read_bytes():
                raise BundleRejected(f"{name} weicht vom Neubau aus dem Tag ab")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("version")
    parser.add_argument("--image-repo", required=True)
    parser.add_argument("--compose", type=Path, default=COMPOSE)
    parser.add_argument("--mosquitto-conf", type=Path, default=MOSQUITTO_CONF)
    args = parser.parse_args(argv)
    try:
        verify(args.bundle, args.version, args.image_repo, args.compose, args.mosquitto_conf)
    except BundleRejected as error:
        print(f"Bundle abgelehnt: {error}", file=sys.stderr)
        return 1
    print(f"Bundle {args.version} entspricht dem Neubau aus dem Tag")
    return 0


if __name__ == "__main__":
    sys.exit(main())
