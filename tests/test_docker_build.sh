#!/bin/bash
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ADDON_DIR="$HERE/../cloudflared_access_mqtt"
FAIL=0
IMAGE_TAG="cloudflared-access-mqtt-test:local"

echo "--- docker build ---"
docker build --platform linux/amd64 -t "$IMAGE_TAG" "$ADDON_DIR" || { echo "FAIL: docker build"; exit 1; }
echo "PASS: docker build erfolgreich"

TMPDIR="$(mktemp -d)"
DATA_DIR="$TMPDIR/data"
STUB_DIR="$TMPDIR/bin"
mkdir -p "$DATA_DIR" "$STUB_DIR"
cp "$HERE/stub_cloudflared.sh" "$STUB_DIR/cloudflared"
chmod +x "$STUB_DIR/cloudflared"

cat > "$DATA_DIR/options.json" <<JSON
{"hostname":"test.example.com","local_port":18830,"service_token_id":"fake-id","service_token_secret":"fake-secret"}
JSON

echo "--- container run (Stub-cloudflared via PATH-Override) ---"
# Git-Bash/MSYS auf Windows schreibt Unix-artige Pfad-Strings in
# Kommandozeilenargumenten automatisch in Windows-Pfade um, bevor sie an
# docker.exe uebergeben werden. Fuer die -v-Mounts unten ist das erwuenscht
# (Host-Pfad muss ein echter Windows-Pfad sein) - deshalb hier per `cygpath -w`
# explizit vorab konvertiert. Fuer den PATH-Wert im -e-Flag ist es dagegen
# schaedlich (kein echter Dateipfad, wuerde jq im Container unauffindbar
# machen) - MSYS_NO_PATHCONV=1 unterdrueckt die automatische Konvertierung fuer
# diesen einen Aufruf, jetzt ohne die -v-Mounts zu beeintraechtigen, da deren
# Pfade schon explizit konvertiert sind. Auf Linux/Mac sind cygpath und
# MSYS_NO_PATHCONV wirkungslose No-Ops (cygpath -w gibt dort den Pfad
# unveraendert zurueck, falls ueberhaupt vorhanden).
if command -v cygpath >/dev/null 2>&1; then
  DATA_DIR_HOST="$(cygpath -w "$DATA_DIR")"
  STUB_CF_HOST="$(cygpath -w "$STUB_DIR/cloudflared")"
else
  DATA_DIR_HOST="$DATA_DIR"
  STUB_CF_HOST="$STUB_DIR/cloudflared"
fi

MSYS_NO_PATHCONV=1 docker run --rm \
  -v "$DATA_DIR_HOST:/data" \
  -v "$STUB_CF_HOST:/usr/local/sbin/cloudflared" \
  -e STUB_LOG=/data/stub.log \
  -e PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
  "$IMAGE_TAG"
CONTAINER_EXIT=$?

if [ "$CONTAINER_EXIT" != "0" ]; then
  echo "FAIL: Container-Exit-Code $CONTAINER_EXIT (erwartet 0)"
  FAIL=1
else
  echo "PASS: Container lief mit Exit-Code 0"
fi

if [ -s "$DATA_DIR/stub.log" ] && grep -q "test.example.com" "$DATA_DIR/stub.log"; then
  echo "PASS: cloudflared-Stub im Container korrekt mit Hostname aufgerufen"
else
  echo "FAIL: cloudflared-Stub im Container nicht wie erwartet aufgerufen"
  FAIL=1
fi

rm -rf "$TMPDIR"
exit $FAIL
