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

echo "--- pruefe GET / (Wizard-Startseite) im laufenden Container ---"
# Guards against the static/ wizard assets (index.html, wizard.js) silently not being
# packaged into the installed wheel -- in that case Flask is up and the container stays
# running (the two checks above would still PASS), but "/" 404s and the add-on is
# unconfigurable in practice. No curl in the python:3.12-alpine base image, so use the
# python interpreter that's already there (same one the Dockerfile's ENTRYPOINT uses).
WIZARD_STATUS="$(docker exec "$CONTAINER_NAME" python -c '
import urllib.request, urllib.error
try:
    resp = urllib.request.urlopen("http://localhost:8099/", timeout=5)
    print(resp.status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception as e:
    print("ERROR:", e)
' 2>/dev/null)"

if [ "$WIZARD_STATUS" = "200" ]; then
  echo "PASS: GET / liefert 200 (Wizard-Startseite wird ausgeliefert)"
else
  echo "FAIL: GET / liefert '$WIZARD_STATUS' statt 200 (fehlt static/ im installierten Wheel?)"
  FAIL=1
fi

docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1
rm -rf "$TMPDIR"
exit $FAIL
