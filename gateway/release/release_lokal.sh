#!/usr/bin/env bash
# Gateway-Release am Betreiberrechner (Nutzer-Entscheidung FR-24: eigene Ablage am Server, nicht GitHub): baut das
# Image-Archiv und das Bundle aus dem signierten Tag, prueft es per Neubau, signiert das Manifest mit dem Release-Schluessel
# (Hardware-Token oder Test-Kette) und laedt das Bundle per scp in die Ablage.
#
# Aufruf:  release_lokal.sh VERSION --schluessel PEM|pkcs11:URI --zertifikat DATEI [--roots DATEI] [--aus DIR] [--probe]
#   VERSION       x.y.z, ohne "v"; der Tag heisst gateway-vVERSION.
#   --schluessel  PEM-Datei (Test-Kette) oder pkcs11:-URI des Release-Schluessels (signieren.py; die PIN gehoert nicht in
#                 die URI, siehe signieren.py).
#   --zertifikat  das Release-Zertifikat (PEM, von ca.py ausstellen).
#   --roots       Schluesselbund, gegen den die Signatur sofort geprueft wird (Standard: gateway/host/release-roots.pem, der
#                 Schluesselbund im Geraete-Image).
#   --aus DIR     Arbeitsordner (Standard: neuer Ordner unter $TMPDIR; er bleibt, ausser bei --probe oder einem Fehler).
#                 Darin liegen image.oci.tar (Ausgabe von buildx) und <VERSION>/ (das Bundle samt manifest.json.sig).
#   --probe       Probelauf fuer Labor und CI: kein Tag-, Baum- und Umgebungs-Check, kein Upload, keine Ausgabe der Werte.
#                 Nur mit einer Wegwerf-Kette: ein pkcs11:-Schluessel und der Standard-Schluesselbund
#                 (gateway/host/release-roots.pem) sind im Probelauf verboten, sonst signierte --probe ohne Tag- und
#                 Baum-Check mit dem echten Token.
# Ohne --probe:
#   - $SHG_BUNDLE_ZIEL (scp-Ziel user@host:/pfad) und $SHG_BUNDLE_BASIS (https-Basis-URL der Ablage) muessen gesetzt sein,
#   - der Arbeitsbaum ist sauber (auch ohne unversionierte Dateien) und HEAD ist der Commit des Tags gateway-vVERSION,
#     dessen SSH-Signatur `git verify-tag` gegen .github/allowed_signers aus origin/main bestaetigt (frisch geholt, wie
#     scripts/ci/tag_pruefen.sh; nie aus dem Checkout des Tags und nicht aus gpg.ssh.allowedSignersFile des Betreibers:
#     wer einen Tag setzt, kann die Liste in dessen Commit aendern).
# Ablauf: Image bauen (docker buildx, nur linux/arm64, OCI-Archiv mit SBOM, Zeitstempel aus dem Commit), CycloneDX-SBOM
# (SG-16) des Archivs per gepinntem syft-Container nach <aus>/gateway-<VERSION>.cdx.json (neben dem Bundle, nicht darin),
# build_bundle.py,
# verify_bundle.py (Neubau aus diesem Stand), signieren.py (Signatur und sofortige Pruefung mit dem Code des Geraets), dann
# scp -r <aus>/<VERSION> nach $SHG_BUNDLE_ZIEL/. Stdout tragen nur die vier Werte fuer `accounts_admin bundle add`
# (version, manifest_url = $SHG_BUNDLE_BASIS/<VERSION>/manifest.json, sha256 des Manifests, signaturdatei); alles andere
# geht nach stderr. Ein vorhandener Ordner <VERSION> in der Ablage wird von scp ueberschrieben: neue Versionen nur mit neuer
# Nummer (der Server haelt den SHA-256 des Manifests, ein veraendertes Bundle lehnt das Geraet ab).
# Umgebung: PYTHON (Standard python3, braucht cryptography und PyYAML: gateway/release/requirements.txt).
set -euo pipefail
umask 022

usage() {
  echo "Aufruf: $0 VERSION --schluessel PEM|pkcs11:URI --zertifikat DATEI [--roots DATEI] [--aus DIR] [--probe]" >&2
  exit 2
}
fehler() { echo "FEHLER: $*" >&2; exit 1; }

[ $# -ge 1 ] || usage
version="$1"; shift
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "FEHLER: Version muss x.y.z sein: $version" >&2; exit 2; }
schluessel=""; zertifikat=""; roots=""; aus=""; probe=0
while [ $# -gt 0 ]; do
  case "$1" in
    --schluessel) [ $# -ge 2 ] || usage; schluessel="$2"; shift 2 ;;
    --zertifikat) [ $# -ge 2 ] || usage; zertifikat="$2"; shift 2 ;;
    --roots) [ $# -ge 2 ] || usage; roots="$2"; shift 2 ;;
    --aus) [ $# -ge 2 ] || usage; aus="$2"; shift 2 ;;
    --probe) probe=1; shift ;;
    *) usage ;;
  esac
done
[ -n "$schluessel" ] && [ -n "$zertifikat" ] || usage
# syft (CycloneDX-SBOM): Image per Digest gepinnt, laeuft ohne Netz und liest das Archiv nur lesend. Erneuern: Tag holen
# (`docker pull anchore/syft:vX.Y.Z`), Digest aus `docker inspect --format '{{index .RepoDigests 0}}'` eintragen; die CI
# (release.yml, Job image) nutzt denselben Digest.
SYFT_IMAGE="anchore/syft:v1.55.0@sha256:4cc92b64ca74ffca4c94d4c8fcc01f217731375300be14d0b6eeb2e82eaa110a"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
roots="${roots:-$repo/gateway/host/release-roots.pem}"
py() { "${PYTHON:-python3}" "$@"; }
tag="gateway-v$version"

# Alles pruefen, bevor etwas gebaut oder angelegt wird.
[[ "$schluessel" == pkcs11:* ]] || [ -f "$schluessel" ] || fehler "Schluesseldatei fehlt"
[ -f "$zertifikat" ] || fehler "Zertifikat fehlt: $zertifikat"
[ -f "$roots" ] || fehler "Schluesselbund fehlt: $roots"
if [ "$probe" = 1 ]; then
  [[ "$schluessel" != pkcs11:* ]] || fehler "--probe signiert nie mit einem pkcs11:-Schluessel (echtes Token, ohne Tag- und Baum-Check)"
  [ "$(realpath -- "$roots")" != "$(realpath -- "$repo/gateway/host/release-roots.pem")" ] \
    || fehler "--probe braucht --roots einer Wegwerf-Kette, nicht den Standard-Schluesselbund (gateway/host/release-roots.pem)"
fi
if [ "$probe" = 0 ]; then
  [ -n "${SHG_BUNDLE_ZIEL:-}" ] && [ -n "${SHG_BUNDLE_BASIS:-}" ] \
    || fehler "SHG_BUNDLE_ZIEL (scp-Ziel user@host:/pfad) und SHG_BUNDLE_BASIS (https-Basis-URL) muessen gesetzt sein (Runbook Gateway-Release)"
  [[ "$SHG_BUNDLE_ZIEL" =~ ^[A-Za-z0-9._][A-Za-z0-9._-]*@[A-Za-z0-9._][A-Za-z0-9._-]*:/[A-Za-z0-9._/+-]*$ ]] \
    || fehler "SHG_BUNDLE_ZIEL muss user@host:/pfad sein (nur Buchstaben, Ziffern und . _ - / +, user und host ohne fuehrenden Strich)"
  [[ "$SHG_BUNDLE_BASIS" =~ ^https://[A-Za-z0-9._-]+(:[0-9]+)?(/[A-Za-z0-9._~/+-]*)?$ ]] \
    || fehler "SHG_BUNDLE_BASIS muss eine https-URL sein (nur Buchstaben, Ziffern und . _ ~ - / +)"
  [ -z "$(git -C "$repo" status --porcelain)" ] || fehler "Arbeitsbaum ist nicht sauber (auch unversionierte Dateien zaehlen)"
  # Schluesselliste aus origin/main, wie scripts/ci/tag_pruefen.sh: nicht aus dem Checkout des Tags (dort aenderbar) und
  # nicht aus der Git-Konfiguration des Betreibers (-c uebersteuert gpg.ssh.allowedSignersFile).
  git -C "$repo" fetch --force --quiet origin "+refs/heads/main:refs/remotes/origin/main" \
    || fehler "origin/main nicht zu holen (Schluesselliste .github/allowed_signers)"
  liste="$(mktemp "${TMPDIR:-/tmp}/allowed_signers.XXXXXX")"
  git -C "$repo" show origin/main:.github/allowed_signers >"$liste" 2>/dev/null \
    || { rm -f "$liste"; fehler ".github/allowed_signers fehlt auf origin/main"; }
  if ! grep -qv '^[[:space:]]*\(#\|$\)' "$liste" || grep -v '^[[:space:]]*\(#\|$\)' "$liste" | grep -q 'SMARTHEAT-PLATZHALTER'; then
    rm -f "$liste"; fehler ".github/allowed_signers auf origin/main ist leer oder der Platzhalter: Signierschluessel eintragen (H-1)"
  fi
  git -C "$repo" -c "gpg.ssh.allowedSignersFile=$liste" verify-tag "$tag" >&2 \
    || { rm -f "$liste"; fehler "Tag $tag fehlt oder seine SSH-Signatur ist von keinem Schluessel aus .github/allowed_signers (origin/main)"; }
  rm -f "$liste"
  [ "$(git -C "$repo" rev-parse "$tag^{commit}")" = "$(git -C "$repo" rev-parse HEAD)" ] \
    || fehler "HEAD ist nicht der Commit von $tag (erst den Tag auschecken)"
fi

eigener_ordner=0
if [ -z "$aus" ]; then
  aus="$(mktemp -d "${TMPDIR:-/tmp}/setwell-release.XXXXXX")"
  eigener_ordner=1
fi
aus="$(mkdir -p "$aus" && cd "$aus" && pwd)"
bundle="$aus/$version"
archiv="$aus/image.oci.tar"
sbom="$aus/gateway-$version.cdx.json"
[ ! -e "$bundle" ] && [ ! -e "$archiv" ] && [ ! -e "$sbom" ] \
  || fehler "$bundle, $archiv oder $sbom existiert schon, nichts ueberschrieben"
abraeumen() {
  local rc=$?
  if [ "$eigener_ordner" = 1 ] && { [ "$probe" = 1 ] || [ "$rc" -ne 0 ]; }; then
    rm -rf "${aus:?}"
  elif [ "$rc" -ne 0 ]; then
    rm -rf "${bundle:?}" "${archiv:?}" "${sbom:?}"
  fi
}
trap abraeumen EXIT

epoche="$(git -C "$repo" log -1 --format=%ct HEAD)"
docker buildx build --platform linux/arm64 --sbom=true -f "$repo/gateway/Dockerfile" \
  --build-arg "SOURCE_DATE_EPOCH=$epoche" --output "type=oci,dest=$archiv,rewrite-timestamp=true" "$repo" >&2
# CycloneDX-SBOM des Image-Archivs (SG-16; --sbom=true oben liefert SPDX als Attestierung im Archiv).
docker run --rm --network none --security-opt label=disable -e SYFT_CHECK_FOR_APP_UPDATE=false \
  -v "$archiv:/image.oci.tar:ro" "$SYFT_IMAGE" scan oci-archive:/image.oci.tar -o cyclonedx-json >"$sbom" \
  || fehler "syft: CycloneDX-SBOM nicht erzeugt"
grep -q '"bomFormat": *"CycloneDX"' "$sbom" || fehler "syft: $sbom ist kein CycloneDX-Dokument"
py "$here/build_bundle.py" --compose "$repo/gateway/compose/docker-compose.yml" \
  --mosquitto-conf "$repo/gateway/compose/mosquitto.conf" --archive "$archiv" --version "$version" --out "$bundle" >/dev/null
py "$here/verify_bundle.py" "$bundle" "$version" >&2
py "$here/signieren.py" "$bundle" --zertifikat "$zertifikat" --schluessel "$schluessel" --roots "$roots" >&2

if [ "$probe" = 1 ]; then
  echo "Probelauf: gebaut, geprueft und signiert, nichts hochgeladen." >&2
  exit 0
fi
scp -r -- "$bundle" "${SHG_BUNDLE_ZIEL%/}/" >&2
echo "Hochgeladen: ${SHG_BUNDLE_ZIEL%/}/$version/ (Arbeitsordner: $aus; SBOM, nicht hochgeladen: $sbom)" >&2
printf 'version=%s\n' "$version"
printf 'manifest_url=%s/%s/manifest.json\n' "${SHG_BUNDLE_BASIS%/}" "$version"
printf 'sha256=%s\n' "$(sha256sum "$bundle/manifest.json" | cut -d' ' -f1)"
printf 'signaturdatei=%s\n' "$bundle/manifest.json.sig"
