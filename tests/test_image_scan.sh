#!/bin/bash
# Image-Scan: Add-on-Image, Gateway-Image und die Fremd-Images aus
# gateway/compose/docker-compose.yml mit trivy. Rot bei Schwachstellen der Schwere CRITICAL, fuer die es einen Fix gibt.
# Ausnahmen: .trivyignore (je ID eine Kommentarzeile mit Begruendung darueber). Braucht Docker und Netz
# (Schwachstellen-Datenbank); ohne Netz scheitert der Schritt, er wird nie still uebersprungen. Nur mit --full.
# Die Datenbank liegt im benannten Docker-Volume smartheat-trivy-cache (nicht im Checkout: der Container laeuft als
# root, Dateien im Checkout waeren fuer den Nutzer nicht loeschbar); Checkouts und Worktrees teilen sich das Volume.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TRIVY="aquasec/trivy:0.76.0@sha256:2416d1f74045b57274df1a340eee6b510cacd73c66a6ccb8da092b117eb7d222"
CACHE="${TRIVY_CACHE_VOLUME:-smartheat-trivy-cache}"
docker build --platform linux/amd64 -q -t smartheat-scan-addon "$REPO/heizungsbruecke" >/dev/null
docker build --platform linux/amd64 -q -f "$REPO/gateway/Dockerfile" -t smartheat-scan-gateway "$REPO" >/dev/null
mapfile -t FREMD < <(sed -n 's/^[[:space:]]*image:[[:space:]]*\([^[:space:]#]*@sha256:[0-9a-f]*\).*/\1/p' \
  "$REPO/gateway/compose/docker-compose.yml")
[ "${#FREMD[@]}" -ge 2 ] || { echo "FAIL: Fremd-Images nicht gefunden"; exit 1; }
rc=0
for image in smartheat-scan-addon smartheat-scan-gateway "${FREMD[@]}"; do
  echo "--- trivy $image ---"
  # label=disable: auf SELinux-Hosts (Fedora) duerfen Container die Bind-Mounts sonst nicht lesen (wie die anderen
  # Skripte).
  docker run --rm --security-opt label=disable -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$CACHE:/root/.cache/trivy" \
    -v "$REPO/.trivyignore:/.trivyignore:ro" "$TRIVY" image --quiet --scanners vuln --severity CRITICAL \
    --ignore-unfixed --ignorefile /.trivyignore --exit-code 1 "$image" || rc=1
done
if [ "$rc" = 0 ]; then echo "PASS: image scan"
else echo "FAIL: image scan (Befunde oben; Ausnahme nur begruendet in .trivyignore)"; fi
exit "$rc"
