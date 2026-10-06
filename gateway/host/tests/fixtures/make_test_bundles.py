"""Baut drei Test-Bundles (gesund, gesund, kaputt) mit Wegwerf-Schluessel fuer tests/test_gateway_updater.sh.
Aufruf (Python mit pyyaml und cryptography): python3 make_test_bundles.py --image REF --out DIR --base-url URL
Ausgabe: DIR/<version>/{docker-compose.yml,mosquitto.conf,manifest.json,manifest.json.minisig}, DIR/test.pub und auf
stdout JSON {version: {"manifest_url", "manifest_sha256", "signature"}}."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
GATEWAY = HERE.parents[2]
# host/tests (minisign_helper), host (smartheat_host fuer build_bundle), release (build_bundle)
sys.path[:0] = [str(HERE.parent), str(GATEWAY / "host"), str(GATEWAY / "release")]
from build_bundle import build  # noqa: E402
from minisign_helper import keypair, sign  # noqa: E402

VERSIONS = ("0.9.1", "0.9.2", "0.9.3")  # 0.9.3: Laufzeit beendet sich sofort -> ungesund -> Rueckweg


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    args = parser.parse_args()
    secret, key_id, public = keypair()
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "test.pub").write_text(public)
    compose = HERE / "updater-compose.yml"
    broken = args.out / "broken-compose.yml"
    data = yaml.safe_load(compose.read_text())
    data["services"]["runtime"]["command"] = ["python", "-c", "import sys; sys.exit(3)"]
    broken.write_text(yaml.safe_dump(data, sort_keys=False))
    answer = {}
    for version in VERSIONS:
        target = args.out / version
        build(broken if version == VERSIONS[-1] else compose, GATEWAY / "compose" / "mosquitto.conf",
              args.image, version, target)
        raw = (target / "manifest.json").read_bytes()
        signature = sign(secret, key_id, raw, f"smartheat-gateway {version}")
        (target / "manifest.json.minisig").write_text(signature)
        answer[version] = {"manifest_url": f"{args.base_url}/_e2e/files/{version}/manifest.json",
                           "manifest_sha256": hashlib.sha256(raw).hexdigest(), "signature": signature}
    print(json.dumps(answer))
    return 0


if __name__ == "__main__":
    sys.exit(main())
