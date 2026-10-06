#!/usr/bin/env python3
"""Bundle-Bau (Spec G2b-1 7): aus der Gateway-Compose (build:) die Bundle-Compose mit dem Image-Digest fuer die
Gateway-Dienste; Mosquitto und Zigbee2MQTT behalten ihre gepinnten Digests. Manifest mit Version,
min_updater_version, SHA-256 der Dateien und Images. Geprueft mit denselben Funktionen wie auf dem Geraet."""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))  # Aufruf als Skript ohne PYTHONPATH
from smartheat_host import bundles  # noqa: E402

MIN_UPDATER_VERSION = "0.2.0"  # steigt nur, wenn sich das Manifest-Format aendert (Plan G2b-1 Praezisierung 9)
GATEWAY_SERVICES = ("init", "agent", "tunnel", "runtime")
_PINNED = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}")
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
    return _HEADER + yaml.dump(data, Dumper=_FlatDumper, sort_keys=False, allow_unicode=True)


def build(compose_path: Path, mosquitto_conf: Path, image_ref: str, version: str, out: Path) -> dict:
    if not _PINNED.fullmatch(image_ref):
        raise SystemExit(f"Image ohne gueltigen Digest (name@sha256:<64 Hex>): {image_ref}")
    bundles.parse_version(version)
    compose = render_compose(compose_path.read_text(), image_ref)
    bundles.check_compose(compose)
    files = {"docker-compose.yml": compose.encode(), "mosquitto.conf": mosquitto_conf.read_bytes()}
    images = {"gateway": image_ref}
    for ref in bundles.compose_images(compose):
        if ref != image_ref:
            images[ref.split("@", 1)[0].split("/")[-1].split(":")[0]] = ref
    manifest = {"version": version, "min_updater_version": MIN_UPDATER_VERSION,
                "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}, "images": images}
    raw = json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"
    bundles.parse_manifest(raw)  # alles pruefen, bevor etwas geschrieben wird
    out.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (out / name).write_bytes(data)
    (out / "manifest.json").write_bytes(raw)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose", type=Path, required=True)
    parser.add_argument("--mosquitto-conf", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    manifest = build(args.compose, args.mosquitto_conf, args.image, args.version, args.out)
    print(json.dumps(manifest["files"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
