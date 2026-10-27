#!/bin/bash
# Stage fuer den Image-Bau, laeuft auf dem Build-Host (braucht Docker und python3): Installer-Baum
# (ohne Tests), das signierte Bundle des Betreibers samt seinem Gateway-Image (gateway-image.oci.tar), die Container-Archive
# und die Installer-Optionen. Das Bundle wird vor allem anderen gegen sein manifest.json geprueft (SHA-256 der Dateien, Groesse,
# SHA-256 und Index-Digest des Archivs); das eigene Image kommt aus diesem Archiv und nie per skopeo, nur Mosquitto und
# Zigbee2MQTT holt export_images.sh (skopeo im Debian-13-Container). Alle Archive landen als <SHA-256>.tar in images/ und
# im Image unter /opt/smartheat/werkseinstellung/images/. Geraete-API- und Portal-Adresse muessen ein eigener DNS-Name per
# https sein (own_url.sh, Nutzer-Vorgabe 2026-10-07); geprueft wird das vor jeder anderen Arbeit.
# Aufruf: prepare.sh --bundle ORDNER --stage ORDNER --device-api-url URL --portal-base-url URL [--release-roots DATEI]
#   --bundle: docker-compose.yml, mosquitto.conf, manifest.json, manifest.json.sig und gateway-image.oci.tar.
#   --stage: darf nicht existieren oder muss leer sein (wird nie geloescht).
#   --release-roots: Schluesselbund der Roots (PEM, Format wie host/release-roots.pem), ersetzt im Installer-Baum genau
#     installer/gateway/host/release-roots.pem (fuer Labor-Images mit eigener Root); Standard ist host/release-roots.pem.
#     Nur von Hand fuer Labor-Images, kein Workflow setzt ihn (Test-Waechter in test_image_scripts.py).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
GW="$(cd "$HERE/.." && pwd)"
DEBIAN_IMAGE="debian:trixie@sha256:913f6706df59a68922d1dd08f78c2476560a8d367897200a6005b00e5f67c2d5"
# Module aus gateway/src bzw. heizungsbruecke/src/smartheat_core, die install.sh mit ablegt (wie dort und in
# tests/test_boundaries.py SHIPPED und SHIPPED_CORE).
SHIPPED=(__init__.py paths.py version.py)
SHIPPED_CORE=(__init__.py dateien.py)
# shellcheck source=own_url.sh
. "$HERE/own_url.sh"
# shellcheck source=release_roots.sh
. "$HERE/release_roots.sh"
usage() { echo "FEHLER: $1" >&2; exit 2; }
BUNDLE="" STAGE="" API_URL="" PORTAL_URL="" RELEASE_ROOTS="" API_SET=0 PORTAL_SET=0 RELEASE_ROOTS_SET=0
while [ $# -gt 0 ]; do
  case "$1" in
    --bundle|--stage|--device-api-url|--portal-base-url|--release-roots)
      [ $# -ge 2 ] || usage "$1 braucht einen Wert" ;;
  esac
  case "$1" in
    --bundle) BUNDLE="$2"; shift ;;
    --stage) STAGE="$2"; shift ;;
    --device-api-url) API_URL="$2"; API_SET=1; shift ;;
    --portal-base-url) PORTAL_URL="$2"; PORTAL_SET=1; shift ;;
    --release-roots) RELEASE_ROOTS="$2"; RELEASE_ROOTS_SET=1; shift ;;
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
if [ "$RELEASE_ROOTS_SET" = 1 ]; then shg_release_roots_ok "$RELEASE_ROOTS" || usage "$shg_release_roots_hint"; fi
[ -n "$BUNDLE" ] || usage "--bundle fehlt"
[ -n "$STAGE" ] || usage "--stage fehlt"
for name in docker-compose.yml mosquitto.conf manifest.json manifest.json.sig gateway-image.oci.tar; do
  [ -f "$BUNDLE/$name" ] || usage "Bundle unvollstaendig: $name fehlt in $BUNDLE"
done
# Das Bundle ist Daten vom Bau-Rechner: Dateien, Archiv und Index-Digest muessen zu manifest.json passen. Ausgabe: SHA-256 des
# Archivs, dann die Images, die nicht im Archiv liegen (Mosquitto, Zigbee2MQTT), je eine Zeile.
bundle_check="$(PYTHONDONTWRITEBYTECODE=1 python3 - "$BUNDLE" "$GW" <<'PY'
import hashlib
import sys
from pathlib import Path

bundle, gw = Path(sys.argv[1]), Path(sys.argv[2])
sys.path[:0] = [str(gw / "host"), str(gw / "release")]
from build_bundle import oci_digest  # noqa: E402
from smartheat_host import bundles  # noqa: E402


def fail(text):
    print(f"FEHLER: Bundle {bundle}: {text}", file=sys.stderr)
    sys.exit(1)


def sha256(path):
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        while block := handle.read(1 << 20):
            hasher.update(block)
    return hasher.hexdigest()


try:
    manifest = bundles.parse_manifest((bundle / bundles.MANIFEST_NAME).read_bytes())
except (OSError, bundles.ManifestError) as error:
    fail(f"manifest.json: {error}")
for name, sha in manifest.files.items():
    if sha256(bundle / name) != sha:
        fail(f"{name}: SHA-256 passt nicht zum Manifest")
archive = bundle / bundles.ARCHIVE_NAME
if archive.stat().st_size != manifest.archiv.bytes or sha256(archive) != manifest.archiv.sha256:
    fail("Archiv: Groesse oder SHA-256 passt nicht zum Manifest")
try:
    digest = oci_digest(archive)
except SystemExit as error:
    fail(str(error))
if digest != manifest.archiv.digest:
    fail(f"Archiv: Index-Digest {digest} passt nicht zu archiv.digest {manifest.archiv.digest}")
if manifest.images.get("gateway") != manifest.archiv.ref:
    fail("images.gateway passt nicht zum Archiv")
externe = [ref for ref in dict.fromkeys(bundles.compose_images((bundle / "docker-compose.yml").read_text()))
           if ref != manifest.archiv.ref]
if not externe or any(ref.startswith(bundles.ARCHIVE_IMAGE + "@") for ref in externe):
    fail("die Compose-Datei nennt keine fremden Images oder ein zweites eigenes")
print(manifest.archiv.sha256)
print("\n".join(externe))
PY
)" || exit 2
if [ -e "$STAGE" ] && { [ ! -d "$STAGE" ] || [ -n "$(ls -A "$STAGE")" ]; }; then
  usage "--stage $STAGE existiert und ist nicht leer"
fi
mkdir -p "$STAGE"
STAGE="$(cd "$STAGE" && pwd)"
# 3. Installer-Baum, wie install.sh ihn erwartet (gateway/host, gateway/src/..., gateway/VERSION) plus Image-Pruefung
inst="$STAGE/installer/gateway"
mkdir -p "$inst/src/smartheat_gateway" "$inst/src/smartheat_core" "$inst/image" "$STAGE/bundle" "$STAGE/images"
cp -a "$GW/host" "$inst/host"
rm -rf "$inst/host/tests"
if [ "$RELEASE_ROOTS_SET" = 1 ]; then cp "$RELEASE_ROOTS" "$inst/host/release-roots.pem"; fi
find "$inst/host" -name __pycache__ -type d -prune -exec rm -rf {} +
cp "$HERE/rootfs_checks.sh" "$HERE/own_url.sh" "$HERE/customize.sh" "$inst/image/"
cp "$GW/VERSION" "$inst/VERSION"
for rel in "${SHIPPED[@]}"; do cp "$GW/src/smartheat_gateway/$rel" "$inst/src/smartheat_gateway/$rel"; done
for rel in "${SHIPPED_CORE[@]}"; do
  cp "$GW/../heizungsbruecke/src/smartheat_core/$rel" "$inst/src/smartheat_core/$rel"
done
cp "$BUNDLE"/{docker-compose.yml,mosquitto.conf,manifest.json,manifest.json.sig} "$STAGE/bundle/"
# 4. Container-Archive: das eigene Image aus dem Bundle (nie per skopeo), danach die fremden per skopeo. Jedes Archiv
# heisst nach der SHA-256 seines Inhalts (<SHA-256>.tar): der erste Start laedt je Name einmal, ohne zu hashen.
{ read -r own_sha; mapfile -t refs; } <<<"$bundle_check"
cp "$BUNDLE/gateway-image.oci.tar" "$STAGE/images/$own_sha.tar"
# Export ohne Docker-Daemon und ohne Privilegien (skopeo); die Archive gehoeren danach dem Aufrufer.
docker run --rm --security-opt label=disable -e "HOST_IDS=$(id -u):$(id -g)" -v "$STAGE:/stage" -v "$HERE:/img:ro" \
  "$DEBIAN_IMAGE" bash -c 'rc=0
    apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      skopeo ca-certificates >/dev/null && bash /img/export_images.sh linux/arm64 /stage/images "$@" || rc=$?
    chown -R "$HOST_IDS" /stage/images; exit "$rc"' _ "${refs[@]}"
for archive in "$STAGE"/images/[0-9][0-9].tar; do
  sum="$(sha256sum "$archive")"
  mv "$archive" "$STAGE/images/${sum%% *}.tar"
done
# 5. Optionen fuer install.sh --image (eine pro Zeile; customize.sh liest sie mit mapfile)
printf '%s\n' --device-api-url "$API_URL" --portal-base-url "$PORTAL_URL" >"$STAGE/install.args"
echo "Stage bereit: $STAGE ($(( ${#refs[@]} + 1 )) Images)"
