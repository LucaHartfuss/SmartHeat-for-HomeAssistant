#!/bin/bash
# Gateway-Image bauen (Plan G2b-2 Task 9): prepare.sh auf dem Host, dann build.sh in einem privilegierten
# debian:trixie-Container fuer linux/arm64 (auf einem arm64-Host nativ, sonst ueber QEMU-binfmt, von rpi-image-gen
# nicht offiziell unterstuetzt). Geraete-API- und Portal-Adresse: eigener DNS-Name per https (own_url.sh), geprueft vor
# jeder anderen Arbeit.
# Aufruf: make_image.sh --bundle ORDNER --out ORDNER --device-api-url URL --portal-base-url URL [--pilot-ssh PUBKEY]
# Ergebnis: OUT/smartheat-gateway-<version>[-pilot].img.zst und .sha256
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
DEBIAN_IMAGE="debian:trixie@sha256:913f6706df59a68922d1dd08f78c2476560a8d367897200a6005b00e5f67c2d5"
# shellcheck source=own_url.sh
. "$HERE/own_url.sh"
usage() { echo "FEHLER: $1" >&2; exit 2; }
OUT="" API_URL="" PORTAL_URL="" API_SET=0 PORTAL_SET=0 PASS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --out|--bundle|--device-api-url|--portal-base-url|--pilot-ssh) [ $# -ge 2 ] || usage "$1 braucht einen Wert" ;;
  esac
  case "$1" in
    --out) OUT="$2"; shift ;;
    --device-api-url) API_URL="$2"; API_SET=1; PASS+=("$1" "$2"); shift ;;
    --portal-base-url) PORTAL_URL="$2"; PORTAL_SET=1; PASS+=("$1" "$2"); shift ;;
    --bundle|--pilot-ssh) PASS+=("$1" "$2"); shift ;;
    *) usage "Unbekannte Option: $1" ;;
  esac
  shift
done
[ "$API_SET" = 1 ] || usage "--device-api-url fehlt"
[ "$PORTAL_SET" = 1 ] || usage "--portal-base-url fehlt"
shg_require_own_url --device-api-url "$API_URL"
shg_require_own_url --portal-base-url "$PORTAL_URL"
[ -n "$OUT" ] || usage "--out fehlt"
arch="$(docker version -f '{{.Server.Arch}}' 2>/dev/null || true)"
if [ "$arch" != arm64 ] && [ ! -e /proc/sys/fs/binfmt_misc/qemu-aarch64 ]; then
  echo "FEHLER: linux/arm64 braucht einen arm64-Docker-Host oder QEMU-binfmt fuer aarch64 (Docker-Host: ${arch:-?})" >&2
  exit 1
fi
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
bash "$HERE/prepare.sh" --stage "$STAGE" "${PASS[@]}"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
docker run --rm --privileged --platform linux/arm64 --security-opt label=disable -e "HOST_IDS=$(id -u):$(id -g)" \
  -v "$REPO:/src:ro" -v "$STAGE:/stage:ro" -v "$OUT:/out" "$DEBIAN_IMAGE" bash /src/gateway/image/build.sh
