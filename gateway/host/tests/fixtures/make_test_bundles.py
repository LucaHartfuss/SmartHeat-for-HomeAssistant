"""Baut drei Test-Bundles (gesund, gesund, Laufzeit wirft) mit Wegwerf-Root und -Release-Zertifikat fuer
tests/test_gateway_updater.sh.
Aufruf (Python mit pyyaml und cryptography): python3 make_test_bundles.py --archive-dir DIR --out DIR --base-url URL
Eingabe: --archive-dir enthaelt je Version ein OCI-Archiv `<version>.oci.tar` (anderes Image je Version, damit der Rueckweg
auf ein anderes Image zeigt). Ausgabe: DIR/<version>/{docker-compose.yml,mosquitto.conf,gateway-image.oci.tar,
manifest.json,manifest.json.sig}, dazu DIR/0.9.4/manifest.json (Manifest von 0.9.2 mit geaenderter Version, also andere
Bytes als die Signatur von 0.9.2: wird abgelehnt), DIR/test-roots.pem und auf stdout JSON
{version: {"manifest_url", "manifest_sha256", "signature"}}; die URL liegt unter BASE_URL/<version>/ (neben dem Archiv)."""
import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
GATEWAY = HERE.parents[2]
# host/tests (kette_helper), host (smartheat_host fuer build_bundle), release (build_bundle)
sys.path[:0] = [str(HERE.parent), str(GATEWAY / "host"), str(GATEWAY / "release")]
from build_bundle import build  # noqa: E402
from kette_helper import release, roots_pem, signiere  # noqa: E402
from kette_helper import root as neue_root  # noqa: E402

VERSIONS = ("0.9.1", "0.9.2", "0.9.3")  # 0.9.3: Laufzeit wirft im laufenden Prozess -> ungesund -> Rueckweg
# 0.9.3: der Regel-Worker wirft 10 s nach dem Start (Geraetekern verbunden, Lebenszeichen frisch); prozess.laufen
# trennt, pausiert 60 s und startet neu. In der Pause muss sich das Gateway ungesund melden,
# sonst behielte der Updater das Update. Die Datei data/testfehler belegt dem Testskript, dass dieser Weg lief.
BROKEN_RUNTIME = ("import time; from smartheat_runtime.worker import RegulationWorker; "
                  "RegulationWorker.run = lambda self: "
                  "(time.sleep(10), open('/data/testfehler', 'a').write('x\\n'), 1 / 0); "
                  "from smartheat_gateway import runtime_main; runtime_main.main()")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    args = parser.parse_args()
    jetzt = datetime.now(UTC)
    root_key, root_cert = neue_root(cn="Test Root", von=jetzt - timedelta(days=2))
    release_key, release_cert = release(root_key, root_cert, von=jetzt - timedelta(days=1), bis=jetzt + timedelta(days=30))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "test-roots.pem").write_text(roots_pem(root_cert))
    compose = HERE / "updater-compose.yml"
    broken = args.out / "broken-compose.yml"
    data = yaml.safe_load(compose.read_text())
    data["services"]["runtime"]["command"] = ["python", "-c", BROKEN_RUNTIME]
    broken.write_text(yaml.safe_dump(data, sort_keys=False))
    answer = {}
    for version in VERSIONS:
        target = args.out / version
        build(broken if version == VERSIONS[-1] else compose, GATEWAY / "compose" / "mosquitto.conf",
              args.archive_dir / f"{version}.oci.tar", version, target)
        raw = (target / "manifest.json").read_bytes()
        signature = signiere(release_key, release_cert, raw)
        (target / "manifest.json.sig").write_text(signature)
        answer[version] = {"manifest_url": f"{args.base_url}/{version}/manifest.json",
                           "manifest_sha256": hashlib.sha256(raw).hexdigest(), "signature": signature}
    original = (args.out / "0.9.2" / "manifest.json").read_bytes()
    tampered = original.replace(b'"version": "0.9.2"', b'"version": "0.9.4"')
    assert tampered != original
    (args.out / "0.9.4").mkdir()
    (args.out / "0.9.4" / "manifest.json").write_bytes(tampered)
    answer["0.9.4"] = {"manifest_url": answer["0.9.2"]["manifest_url"].replace("/0.9.2/", "/0.9.4/"),
                       "manifest_sha256": hashlib.sha256(tampered).hexdigest(), "signature": answer["0.9.2"]["signature"]}
    print(json.dumps(answer))
    return 0


if __name__ == "__main__":
    sys.exit(main())
