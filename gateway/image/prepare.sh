#!/bin/bash
# Stage fuer den Image-Bau (Plan G2b-2 Task 9), laeuft auf dem Build-Host (braucht Docker und python3): Installer-Baum
# (ohne Tests), signiertes Bundle, Container-Images als OCI-Archive (export_images.sh mit skopeo im Debian-13-Container)
# und die Installer-Optionen. Geraete-API- und Portal-Adresse muessen ein eigener DNS-Name per https sein (own_url.sh,
# Nutzer-Vorgabe 2026-10-07); geprueft wird das vor jeder anderen Arbeit.
# Aufruf: prepare.sh --bundle ORDNER --stage ORDNER --device-api-url URL --portal-base-url URL [--pilot-ssh PUBKEY]
#   --stage: darf nicht existieren oder muss leer sein (wird nie geloescht).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
GW="$(cd "$HERE/.." && pwd)"
DEBIAN_IMAGE="debian:trixie@sha256:913f6706df59a68922d1dd08f78c2476560a8d367897200a6005b00e5f67c2d5"
# Module aus gateway/src, die install.sh mit ablegt (wie dort und in tests/test_boundaries.py SHIPPED).
SHIPPED=(__init__.py files.py paths.py version.py agent/__init__.py agent/wire.py agent/identity.py)
# shellcheck source=own_url.sh
. "$HERE/own_url.sh"
usage() { echo "FEHLER: $1" >&2; exit 2; }
BUNDLE="" STAGE="" API_URL="" PORTAL_URL="" PILOT_KEY="" API_SET=0 PORTAL_SET=0
while [ $# -gt 0 ]; do
  case "$1" in
    --bundle|--stage|--device-api-url|--portal-base-url|--pilot-ssh)
      [ $# -ge 2 ] || usage "$1 braucht einen Wert" ;;
  esac
  case "$1" in
    --bundle) BUNDLE="$2"; shift ;;
    --stage) STAGE="$2"; shift ;;
    --device-api-url) API_URL="$2"; API_SET=1; shift ;;
    --portal-base-url) PORTAL_URL="$2"; PORTAL_SET=1; shift ;;
    --pilot-ssh) PILOT_KEY="$2"; shift ;;
    *) usage "Unbekannte Option: $1" ;;
  esac
  shift
done
# 1. Adressen (vor allem anderen): eigener DNS-Name per https, keine IP- und keine AWS-Adresse
[ "$API_SET" = 1 ] || usage "--device-api-url fehlt"
[ "$PORTAL_SET" = 1 ] || usage "--portal-base-url fehlt"
shg_require_own_url --device-api-url "$API_URL"
shg_require_own_url --portal-base-url "$PORTAL_URL"
# 2. Uebrige Eingaben
if [ -n "$PILOT_KEY" ]; then
  [[ $PILOT_KEY =~ ^(ssh-[a-z0-9-]+|ecdsa-sha2-[a-z0-9-]+|sk-[a-z0-9@.-]+)\ [A-Za-z0-9+/]+=*(\ [^[:cntrl:]]*)?$ ]] \
    || usage "--pilot-ssh: genau ein oeffentlicher SSH-Schluessel in einer Zeile erwartet"
fi
[ -n "$BUNDLE" ] || usage "--bundle fehlt"
[ -n "$STAGE" ] || usage "--stage fehlt"
for name in docker-compose.yml mosquitto.conf manifest.json manifest.json.minisig; do
  [ -f "$BUNDLE/$name" ] || usage "Bundle unvollstaendig: $name fehlt in $BUNDLE"
done
if [ -e "$STAGE" ] && { [ ! -d "$STAGE" ] || [ -n "$(ls -A "$STAGE")" ]; }; then
  usage "--stage $STAGE existiert und ist nicht leer"
fi
mkdir -p "$STAGE"
STAGE="$(cd "$STAGE" && pwd)"
# 3. Installer-Baum, wie install.sh ihn erwartet (gateway/host, gateway/src/..., gateway/VERSION) plus Image-Pruefung
inst="$STAGE/installer/gateway"
mkdir -p "$inst/src/smartheat_gateway/agent" "$inst/image" "$STAGE/bundle" "$STAGE/images"
cp -a "$GW/host" "$inst/host"
rm -rf "$inst/host/tests"
find "$inst/host" -name __pycache__ -type d -prune -exec rm -rf {} +
cp "$HERE/rootfs_checks.sh" "$HERE/own_url.sh" "$HERE/customize.sh" "$inst/image/"
cp "$GW/VERSION" "$inst/VERSION"
for rel in "${SHIPPED[@]}"; do cp "$GW/src/smartheat_gateway/$rel" "$inst/src/smartheat_gateway/$rel"; done
cp "$BUNDLE"/{docker-compose.yml,mosquitto.conf,manifest.json,manifest.json.minisig} "$STAGE/bundle/"
# 4. Container-Images: jede Referenz einmal (Agent und Laufzeit teilen sich das Gateway-Image)
mapfile -t refs < <(PYTHONPATH="$GW/host" python3 -c 'import sys
from smartheat_host import bundles
print("\n".join(bundles.compose_images(open(sys.argv[1]).read())))' "$BUNDLE/docker-compose.yml" | awk 'NF && !seen[$0]++')
[ ${#refs[@]} -gt 0 ] || usage "keine Images in $BUNDLE/docker-compose.yml"
# Export ohne Docker-Daemon und ohne Privilegien (skopeo); die Archive gehoeren danach dem Aufrufer.
docker run --rm --security-opt label=disable -e "HOST_IDS=$(id -u):$(id -g)" -v "$STAGE:/stage" -v "$HERE:/img:ro" \
  "$DEBIAN_IMAGE" bash -c 'rc=0
    apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      skopeo ca-certificates >/dev/null && bash /img/export_images.sh linux/arm64 /stage/images "$@" || rc=$?
    chown -R "$HOST_IDS" /stage/images; exit "$rc"' _ "${refs[@]}"
# 5. Optionen fuer install.sh --image (eine pro Zeile; customize.sh liest sie mit mapfile)
{ printf '%s\n' --device-api-url "$API_URL" --portal-base-url "$PORTAL_URL"
  if [ -n "$PILOT_KEY" ]; then printf '%s\n' --pilot-ssh "$PILOT_KEY"; fi; } >"$STAGE/install.args"
if [ -n "$PILOT_KEY" ]; then touch "$STAGE/pilot"; fi
echo "Stage bereit: $STAGE (${#refs[@]} Images)"
