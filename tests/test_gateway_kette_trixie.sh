#!/bin/bash
# Kettenpruefung auf dem System-Python des Geraets: gateway/host/tests/test_kette.py laeuft in Debian 13 (trixie) mit
# python3-cryptography und python3-pytest aus Debian, also mit genau der cryptography-Version, die das Gateway
# einsetzt (SG-04); --noconftest, weil die conftest hypothesis laedt, das auf dem Geraete-Python fehlt. Der
# Quellbaum ist nur lesbar eingehaengt.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
DEBIAN_IMAGE="debian:trixie@sha256:913f6706df59a68922d1dd08f78c2476560a8d367897200a6005b00e5f67c2d5"
docker run --rm --security-opt label=disable -v "$REPO/gateway/host:/host:ro" "$DEBIAN_IMAGE" \
  bash -c 'apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      python3 python3-cryptography python3-pytest openssl >/dev/null \
    && python3 -c "import cryptography; print(\"cryptography\", cryptography.__version__)" \
    && cd /host && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/host:/host/tests \
         python3 -m pytest -q -p no:cacheprovider -c /dev/null --noconftest --rootdir=/host tests/test_kette.py' \
  || { echo "FAIL: test_kette.py auf Debian 13"; exit 1; }
echo "PASS: Kettenpruefung auf Debian 13 (trixie)"
