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
CONTAINER_NAME="heizungsbruecke-docker-build-test"
MSYS_NO_PATHCONV=1 docker run -d --rm --name "$CONTAINER_NAME" \
  -e SUPERVISOR_TOKEN=test-token \
  -e HEIZUNGSSERVER_BASE_URL=http://heizungsserver.invalid \
  -v "$DATA_DIR_HOST:/data" "$IMAGE_TAG" >/dev/null \
  || { echo "FAIL: container start"; exit 1; }

sleep 3

if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null)" != "true" ]; then
  echo "FAIL: Container mit unvollstaendiger Config sollte weiterlaufen (Wizard-Modus), ist aber beendet"
  FAIL=1
else
  echo "PASS: Container laeuft weiter (Wizard-Modus) statt bei unvollstaendiger Config abzustuerzen"
fi

if docker logs "$CONTAINER_NAME" 2>&1 | grep -q "Add-on ist noch nicht eingerichtet"; then
  echo "PASS: Hinweis auf den Einrichtungs-Assistenten im Log vorhanden"
else
  echo "FAIL: erwarteter Hinweis auf den Einrichtungs-Assistenten fehlt im Log"
  FAIL=1
fi

docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1
rm -rf "$TMPDIR"
exit $FAIL
