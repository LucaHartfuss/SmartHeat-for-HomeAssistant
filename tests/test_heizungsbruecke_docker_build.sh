#!/bin/bash
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ADDON_DIR="$HERE/../heizungsbruecke"
FAIL=0
IMAGE_TAG="heizungsbruecke-test:local"

echo "--- docker build ---"
docker build --platform linux/amd64 -t "$IMAGE_TAG" "$ADDON_DIR" || { echo "FAIL: docker build"; exit 1; }
echo "PASS: docker build erfolgreich"

TMPDIR="$(mktemp -d)"
DATA_DIR="$TMPDIR/data"
mkdir -p "$DATA_DIR"

cat > "$DATA_DIR/options.json" <<JSON
{"tenant_id":"test","profile":"vaillant_gastherme_heizkoerper","entity_room_actual":"climate.test"}
JSON

if command -v cygpath >/dev/null 2>&1; then
  DATA_DIR_HOST="$(cygpath -w "$DATA_DIR")"
else
  DATA_DIR_HOST="$DATA_DIR"
fi

echo "--- container run mit unvollstaendiger Config (fehlende Pflicht-Rollen) ---"
MSYS_NO_PATHCONV=1 docker run --rm -e SUPERVISOR_TOKEN=test-token -v "$DATA_DIR_HOST:/data" "$IMAGE_TAG" \
  >"$TMPDIR/stdout.log" 2>"$TMPDIR/stderr.log"
CONTAINER_EXIT=$?

if [ "$CONTAINER_EXIT" != "1" ]; then
  echo "FAIL: erwarteter Exit-Code 1 bei unvollstaendiger Config, bekommen: $CONTAINER_EXIT"
  FAIL=1
else
  echo "PASS: Container beendet sich mit Exit-Code 1 bei fehlenden Pflicht-Rollen"
fi

if grep -q "FEHLER: Folgende Pflichtfelder fehlen" "$TMPDIR/stderr.log"; then
  echo "PASS: Fehlermeldung auf stderr vorhanden"
else
  echo "FAIL: erwartete Fehlermeldung fehlt auf stderr"
  FAIL=1
fi

rm -rf "$TMPDIR"
exit $FAIL
