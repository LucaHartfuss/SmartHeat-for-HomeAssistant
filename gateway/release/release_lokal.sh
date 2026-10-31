#!/usr/bin/env bash
# Gateway-Release am Betreiberrechner (Nutzer-Entscheidung FR-24: eigene Ablage am Server, nicht GitHub): baut das
# Image-Archiv, das Werks-Image und das Bundle aus dem signierten Tag, prueft es per Neubau, signiert das Manifest und das
# RAUC-System-Bundle mit dem Release-Schluessel (Hardware-Token oder Test-Kette) und laedt das Bundle per scp in die Ablage.
#
# Aufruf:  release_lokal.sh VERSION --schluessel PEM|pkcs11:URI --zertifikat DATEI --device-api-url URL --portal-base-url URL
#                           [--roots DATEI] [--aus DIR] [--ohne-system] [--probe | --labor]
#   VERSION       x.y.z, ohne "v"; der Tag heisst gateway-vVERSION.
#   --schluessel  PEM-Datei (Test-Kette) oder pkcs11:-URI des Release-Schluessels (signieren.py; die PIN gehoert nicht in
#                 die URI, siehe signieren.py).
#   --zertifikat  das Release-Zertifikat (PEM, von ca.py ausstellen).
#   --roots       Schluesselbund, gegen den die Signatur sofort geprueft wird (Standard: gateway/host/release-roots.pem, der
#                 Schluesselbund im Geraete-Image). Auch das System-Bundle wird gegen ihn geprueft. Das Skript kopiert ihn
#                 einmal in einen privaten Temp-Ordner (0700 unter $TMPDIR, am Ende geloescht); Pruefung, Signatur, System-Bundle
#                 und Werks-Image nehmen genau diese Kopie, und sie geht bei einem System-Bau immer als --release-roots an
#                 make_image.sh (auch beim Standard-Schluesselbund).
#   --device-api-url, --portal-base-url
#                 Adressen im Werks-Image (eigener DNS-Name per https, gateway/image/own_url.sh); Pflicht, solange das
#                 System gebaut wird.
#   --ohne-system Nur das Container-Bundle, ohne Werks-Image und System-Bundle (das Manifest nennt dann kein `system`,
#                 min_updater_version bleibt 0.8.0): fuer Releases, in denen sich das System nicht geaendert hat.
#   --aus DIR     Arbeitsordner (Standard: neuer Ordner unter $TMPDIR; er bleibt, ausser bei --probe oder einem Fehler).
#                 Darin liegen image.oci.tar (Ausgabe von buildx) und <VERSION>/ (das Bundle samt manifest.json.sig). Mit
#                 System kommen dazu: image/ (Werks-Image smartheat-gateway-<VERSION>.img.zst, Slot-Images, SBOM des Systems,
#                 je mit .sha256; rund 8 GB), werk-bundle/ (das Bundle ohne System, aus dem das Image entsteht) und
#                 system.raucb. Ein Ordner mit genug Platz (rund 15 GB) angeben, $TMPDIR ist oft ein kleines tmpfs.
#   --probe       Probelauf fuer Labor und CI: kein Tag-, Baum- und Umgebungs-Check, kein Upload, keine Ausgabe der Werte,
#                 immer ohne System (die CI baut kein Image).
#                 Nur mit einer Wegwerf-Kette: ein pkcs11:-Schluessel und der Standard-Schluesselbund
#                 (gateway/host/release-roots.pem) sind im Probelauf verboten, sonst signierte --probe ohne Tag- und
#                 Baum-Check mit dem echten Token.
#   --labor       Labor-Bau: baut wie das echte Release (auch Werks-Image und System-Bundle, ausser mit --ohne-system),
#                 aber ohne Tag-, Baum- und Umgebungs-Check, ohne Upload und ohne Ausgabe der Werte auf stdout; am Ende
#                 steht auf stderr, wo die Ergebnisse liegen. Der Arbeitsordner bleibt (ausser bei einem Fehler). Nur mit
#                 Test-Roots: --roots ist Pflicht, ein pkcs11:-Schluessel ist verboten, und jede Root des Schluesselbunds
#                 muss "(Test)" im Namen tragen und darf nicht in gateway/host/release-roots.sha256 des Checkouts stehen
#                 (roots_pruefen.py, vor jedem Bau). Mit System gelten die Adressen und gateway/VERSION wie beim Release.
#                 Schliesst sich mit --probe aus.
# Ohne --probe und --labor:
#   - $SHG_BUNDLE_ZIEL (scp-Ziel user@host:/pfad) und $SHG_BUNDLE_BASIS (https-Basis-URL der Ablage) muessen gesetzt sein,
#   - der Arbeitsbaum ist sauber (auch ohne unversionierte Dateien) und HEAD ist der Commit des Tags gateway-vVERSION,
#     dessen SSH-Signatur `git verify-tag` gegen .github/allowed_signers aus origin/main bestaetigt (frisch geholt, wie
#     scripts/ci/tag_pruefen.sh; nie aus dem Checkout des Tags und nicht aus gpg.ssh.allowedSignersFile des Betreibers:
#     wer einen Tag setzt, kann die Liste in dessen Commit aendern),
#   - jede Root des Schluesselbunds (Standard wie eigenes --roots) steht mit ihrem Fingerabdruck in
#     gateway/host/release-roots.sha256 aus demselben frisch geholten origin/main und ist keine Test-Root ("(Test)" im Namen)
#     (roots_pruefen.py, vor jedem Bau). Der Schluesselbund muss dabei die strenge Form haben (nur Bloecke
#     "BEGIN CERTIFICATE"/"END CERTIFICATE" und Leerzeilen, kein anderer Text, keine andere Blockart wie TRUSTED CERTIFICATE,
#     die RAUC zusaetzlich liest). Die Liste kommt aus origin/main, das nur der Release-Workflow bewegt: Das Bundle laesst
#     sich erst nach CI-Gate und Fast-Forward von main bauen, ein Root-Tausch muss diesen Weg gehen. Das Skript und die
#     Werkzeuge selbst kommen aus dem Checkout des Tags; gegen einen boesartigen Tag-Commit schuetzt die Tag-Signatur des
#     Besitzers samt Gate (wie bei allowed_signers), nicht die Herkunft der Liste.
# Ablauf: Image bauen (docker buildx, nur linux/arm64, OCI-Archiv mit SBOM, Zeitstempel aus dem Commit), CycloneDX-SBOM
# (SG-16) des Archivs per gepinntem syft-Container nach <aus>/gateway-<VERSION>.cdx.json (neben dem Bundle, nicht darin),
# build_bundle.py, verify_bundle.py (Neubau aus diesem Stand), signieren.py (Signatur und sofortige Pruefung mit dem Code
# des Geraets). Mit System (GPC5, GPC6) dazwischen: aus dem Bundle ohne System baut gateway/image/make_image.sh das
# Werks-Image (arm64-Docker-Host oder QEMU-binfmt noetig, es bricht sonst laut ab), rauc_bundle.py baut aus den Slot-Images das
# System-Bundle (gleiche Version wie das Release, geprueft im Image) und signiert es mit demselben Zertifikat und
# Schluessel wie das Manifest, danach entsteht das Bundle mit dem Feld `system` (min_updater_version 0.9.0). Hochgeladen
# wird nur das Bundle (scp -r <aus>/<VERSION> nach $SHG_BUNDLE_ZIEL/): docker-compose.yml, mosquitto.conf, manifest.json,
# manifest.json.sig, gateway-image.oci.tar und system.raucb; Werks-Image und SBOMs bleiben in <aus>/. Der Schwachstellenscan
# des Systems (Image, Debian-Pakete) ist Restpunkt AP-05a-Systemscan und laeuft vor der Freigabe an Kunden (SHG1-5).
# Stdout tragen nur die vier Werte fuer `accounts_admin bundle add`
# (version, manifest_url = $SHG_BUNDLE_BASIS/<VERSION>/manifest.json, sha256 des Manifests, signaturdatei); alles andere
# geht nach stderr. Ein vorhandener Ordner <VERSION> in der Ablage wird von scp ueberschrieben: neue Versionen nur mit neuer
# Nummer (der Server haelt den SHA-256 des Manifests, ein veraendertes Bundle lehnt das Geraet ab).
# Umgebung: PYTHON (Standard python3, braucht cryptography und PyYAML: gateway/release/requirements.txt).
set -euo pipefail
umask 022

usage() {
  echo "Aufruf: $0 VERSION --schluessel PEM|pkcs11:URI --zertifikat DATEI --device-api-url URL --portal-base-url URL" \
       "[--roots DATEI] [--aus DIR] [--ohne-system] [--probe | --labor]" >&2
  exit 2
}
fehler() { echo "FEHLER: $*" >&2; exit 1; }

[ $# -ge 1 ] || usage
version="$1"; shift
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "FEHLER: Version muss x.y.z sein: $version" >&2; exit 2; }
schluessel=""; zertifikat=""; roots=""; aus=""; probe=0; labor=0; ohne_system=0; api_url=""; portal_url=""
while [ $# -gt 0 ]; do
  case "$1" in
    --schluessel) [ $# -ge 2 ] || usage; schluessel="$2"; shift 2 ;;
    --zertifikat) [ $# -ge 2 ] || usage; zertifikat="$2"; shift 2 ;;
    --roots) [ $# -ge 2 ] || usage; roots="$2"; shift 2 ;;
    --aus) [ $# -ge 2 ] || usage; aus="$2"; shift 2 ;;
    --probe) probe=1; shift ;;
    --labor) labor=1; shift ;;
    --ohne-system) ohne_system=1; shift ;;
    --device-api-url) [ $# -ge 2 ] || usage; api_url="$2"; shift 2 ;;
    --portal-base-url) [ $# -ge 2 ] || usage; portal_url="$2"; shift 2 ;;
    *) usage ;;
  esac
done
[ -n "$schluessel" ] && [ -n "$zertifikat" ] || usage
[ "$probe" = 0 ] || [ "$labor" = 0 ] || usage
# syft (CycloneDX-SBOM): Image per Digest gepinnt, laeuft ohne Netz und liest das Archiv nur lesend. Erneuern: Tag holen
# (`docker pull anchore/syft:vX.Y.Z`), Digest aus `docker inspect --format '{{index .RepoDigests 0}}'` eintragen; die CI
# (release.yml, Job image) nutzt denselben Digest.
SYFT_IMAGE="anchore/syft:v1.55.0@sha256:4cc92b64ca74ffca4c94d4c8fcc01f217731375300be14d0b6eeb2e82eaa110a"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
roots_gegeben="$roots"
roots="${roots:-$repo/gateway/host/release-roots.pem}"
py() { "${PYTHON:-python3}" "$@"; }
tag="gateway-v$version"

# Alles pruefen, bevor etwas gebaut oder angelegt wird.
[[ "$schluessel" == pkcs11:* ]] || [ -f "$schluessel" ] || fehler "Schluesseldatei fehlt"
[ -f "$zertifikat" ] || fehler "Zertifikat fehlt: $zertifikat"
[ -f "$roots" ] || fehler "Schluesselbund fehlt: $roots"
# Der Schluesselbund wird einmal in einen privaten Ordner kopiert; ab hier pruefen und verwenden alle (roots_pruefen.py,
# signieren.py, rauc_bundle.py, make_image.sh) genau diese Kopie, nie die Originaldatei, die sich dazwischen aendern koennte.
# Ob es der Standard-Schluesselbund ist, entscheidet das Original.
roots_original="$roots"
roots_ordner=""
roots_abraeumen() { if [ -n "$roots_ordner" ]; then rm -rf "${roots_ordner:?}"; fi; }
trap roots_abraeumen EXIT
roots_ordner="$(mktemp -d "${TMPDIR:-/tmp}/setwell-roots.XXXXXX")"
cp -- "$roots" "$roots_ordner/release-roots.pem" || fehler "Schluesselbund nicht zu kopieren: $roots_original"
roots="$roots_ordner/release-roots.pem"
if [ "$probe" = 1 ]; then
  [[ "$schluessel" != pkcs11:* ]] || fehler "--probe signiert nie mit einem pkcs11:-Schluessel (echtes Token, ohne Tag- und Baum-Check)"
  [ "$(realpath -- "$roots_original")" != "$(realpath -- "$repo/gateway/host/release-roots.pem")" ] \
    || fehler "--probe braucht --roots einer Wegwerf-Kette, nicht den Standard-Schluesselbund (gateway/host/release-roots.pem)"
fi
if [ "$labor" = 1 ]; then
  [ -n "$roots_gegeben" ] || fehler "--labor braucht --roots (die Test-Kette); der Standard-Schluesselbund ist nie erlaubt"
  [[ "$schluessel" != pkcs11:* ]] || fehler "--labor signiert nie mit einem pkcs11:-Schluessel (echtes Token, ohne Tag- und Baum-Check)"
  # Nur Test-Roots, und keine aus der gepinnten Liste (die des Checkouts genuegt: sie darf hier nur nichts verraten).
  py "$here/roots_pruefen.py" labor --liste "$repo/gateway/host/release-roots.sha256" "$roots" >&2 \
    || fehler "Schluesselbund $roots_original ist fuer den Labor-Bau nicht erlaubt (nur Test-Roots, nicht aus gateway/host/release-roots.sha256)"
fi
# Mit System (Standard) braucht es das Werkzeug des Werks-Images; der Probelauf der CI baut kein Image.
system=1
if [ "$probe" = 1 ] || [ "$ohne_system" = 1 ]; then system=0; fi
if [ "$system" = 1 ]; then
  make_image="$repo/gateway/image/make_image.sh"
  [ -f "$make_image" ] || fehler "make_image.sh fehlt ($make_image); ohne Werks-Image und System-Bundle: --ohne-system"
  # shellcheck source=../image/own_url.sh
  . "$repo/gateway/image/own_url.sh"
  shg_require_own_url --device-api-url "$api_url"
  shg_require_own_url --portal-base-url "$portal_url"
  # make_image.sh benennt die Slot-Images nach gateway/VERSION, und das Host-Paket im Image meldet diese Version: sie
  # muss die des Release sein (rauc_bundle.py liest sie danach noch einmal im gebauten Image).
  [ "$(cat "$repo/gateway/VERSION")" = "$version" ] \
    || fehler "gateway/VERSION ($(cat "$repo/gateway/VERSION")) ist nicht die Release-Version $version (Tag $tag auschecken)"
fi
if [ "$probe" = 0 ] && [ "$labor" = 0 ]; then
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
  # Gepinnte Liste der Release-Roots aus demselben frischen origin/main (nie aus dem Checkout): jede Root des Schluesselbunds
  # muss mit ihrem Fingerabdruck darin stehen und darf keine Test-Root sein. Vor jedem Bau, auch fuer ein eigenes --roots.
  roots_liste="$(mktemp "${TMPDIR:-/tmp}/release-roots.XXXXXX")"
  git -C "$repo" show origin/main:gateway/host/release-roots.sha256 >"$roots_liste" 2>/dev/null \
    || { rm -f "$roots_liste"; fehler "gateway/host/release-roots.sha256 fehlt auf origin/main (gepinnte Liste der Release-Roots)"; }
  py "$here/roots_pruefen.py" produktion --liste "$roots_liste" "$roots" >&2 \
    || { rm -f "$roots_liste"; fehler "Schluesselbund $roots_original passt nicht zur Liste der Release-Roots auf origin/main"; }
  rm -f "$roots_liste"
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
werk_bundle="$aus/werk-bundle"      # das Bundle ohne System, aus dem das Werks-Image entsteht
image_aus="$aus/image"              # Ausgabe von make_image.sh: Werks-Image, Slot-Images, SBOM des Systems
buehne="$aus/rauc-buehne"           # Arbeitsordner von rauc_bundle.py (er loescht ihn selbst)
system_bundle="$aus/system.raucb"
for pfad in "$bundle" "$archiv" "$sbom" "$werk_bundle" "$image_aus" "$buehne" "$system_bundle"; do
  [ ! -e "$pfad" ] || fehler "$pfad existiert schon, nichts ueberschrieben"
done
abraeumen() {
  local rc=$?
  roots_abraeumen
  if [ "$eigener_ordner" = 1 ] && { [ "$probe" = 1 ] || [ "$rc" -ne 0 ]; }; then
    rm -rf "${aus:?}"
  elif [ "$rc" -ne 0 ]; then
    rm -rf "${bundle:?}" "${archiv:?}" "${sbom:?}" "${werk_bundle:?}" "${image_aus:?}" "${buehne:?}" "${system_bundle:?}"
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
# Bundle bauen, gegen den Tag pruefen, signieren: $1 = Ordner, weitere Argumente fuer build_bundle.py (--system DATEI).
bundle_bauen() {
  local ordner="$1"; shift
  py "$here/build_bundle.py" --compose "$repo/gateway/compose/docker-compose.yml" \
    --mosquitto-conf "$repo/gateway/compose/mosquitto.conf" --archive "$archiv" --version "$version" --out "$ordner" "$@" >/dev/null
  # Der Neubau legt eine Kopie des System-Bundles an: nicht in $TMPDIR (oft ein kleines tmpfs), sondern im Arbeitsordner.
  TMPDIR="$aus" py "$here/verify_bundle.py" "$ordner" "$version" >&2
  py "$here/signieren.py" "$ordner" --zertifikat "$zertifikat" --schluessel "$schluessel" --roots "$roots" >&2
}
if [ "$system" = 1 ]; then
  # Das Werks-Image traegt das Bundle ohne System (es ist das erste Bundle des Geraets); das System-Bundle bindet das
  # Manifest erst danach (sonst haengte das Manifest vom Image ab, das Image vom Manifest).
  bundle_bauen "$werk_bundle"
  image_args=(--bundle "$werk_bundle" --out "$image_aus" --device-api-url "$api_url" --portal-base-url "$portal_url")
  # Das Image bekommt immer genau die gepruefte Kopie, gegen die hier signiert wird (auch beim Standard-Schluesselbund);
  # make_image.sh prueft ihre Form ein zweites Mal.
  image_args+=(--release-roots "$roots")
  bash "$make_image" "${image_args[@]}" >&2 || fehler "make_image.sh gescheitert (Werks-Image nicht gebaut)"
  py "$here/rauc_bundle.py" --version "$version" --system "$image_aus/system-$version.ext4" \
    --boot "$image_aus/boot-$version.vfat" --zertifikat "$zertifikat" --schluessel "$schluessel" --roots "$roots" \
    --arbeit "$buehne" --out "$system_bundle" >&2 || fehler "rauc_bundle.py: System-Bundle nicht gebaut"
  bundle_bauen "$bundle" --system "$system_bundle"
else
  bundle_bauen "$bundle"
fi

if [ "$probe" = 1 ]; then
  echo "Probelauf: gebaut, geprueft und signiert, nichts hochgeladen." >&2
  exit 0
fi
if [ "$labor" = 1 ]; then
  echo "Labor-Bau: gebaut, geprueft und signiert, nichts hochgeladen. Ergebnisse in $aus (Bundle: $bundle/, SBOM: $sbom)" >&2
  if [ "$system" = 1 ]; then
    echo "Werks-Image und System-Bundle: $image_aus/ und $system_bundle" >&2
  fi
  exit 0
fi
scp -r -- "$bundle" "${SHG_BUNDLE_ZIEL%/}/" >&2
echo "Hochgeladen: ${SHG_BUNDLE_ZIEL%/}/$version/ (Arbeitsordner: $aus; SBOM, nicht hochgeladen: $sbom)" >&2
if [ "$system" = 1 ]; then
  echo "Werks-Image und SBOM des Systems, nicht hochgeladen: $image_aus/" >&2
fi
printf 'version=%s\n' "$version"
printf 'manifest_url=%s/%s/manifest.json\n' "${SHG_BUNDLE_BASIS%/}" "$version"
printf 'sha256=%s\n' "$(sha256sum "$bundle/manifest.json" | cut -d' ' -f1)"
printf 'signaturdatei=%s\n' "$bundle/manifest.json.sig"
