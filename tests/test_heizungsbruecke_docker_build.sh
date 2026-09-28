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
{"tenant_id":"test","verteilsystem":"Heizkoerper","daily_trigger_time":"12:00","day_avg_window_start":"14:00","day_avg_window_end":"17:00","night_avg_window_start":"04:00","night_avg_window_end":"07:00","accounts_api_base_url":"https://accounts.example.test","room_sensors":["climate.test::current_temperature"]}
JSON

if command -v cygpath >/dev/null 2>&1; then
  DATA_DIR_HOST="$(cygpath -w "$DATA_DIR")"
else
  DATA_DIR_HOST="$DATA_DIR"
fi

echo "--- container run mit unvollstaendiger Config (fehlende Pflicht-Rollen) ---"
CONTAINER_NAME="heizungsbruecke-docker-build-test"
# Kein --rm hier: der Container soll nach dem Beenden absichtlich noch existieren, damit
# "docker logs" danach noch greifen kann (Cleanup passiert explizit am Skriptende).
MSYS_NO_PATHCONV=1 docker run -d --name "$CONTAINER_NAME" \
  -e SUPERVISOR_TOKEN=test-token \
  -v "$DATA_DIR_HOST:/data:Z" "$IMAGE_TAG" >/dev/null \
  || { echo "FAIL: container start"; exit 1; }

# Seit TP7 (Ruhezustand statt Exit, siehe __main__.py::_idle/IdleBridge, IDLE_NOT_CONFIGURED)
# beendet sich der Prozess bei unvollstaendiger Config nicht mehr: er bleibt im Ruhezustand
# "nicht_eingerichtet" am Leben, weil der Supervisor-Watchdog einen Exit 0 ohnehin sofort neu
# starten wuerde (siehe SmartHeat-for-HomeAssistant/CLAUDE.md). Erwartet wird daher: der
# Container laeuft nach kurzer Wartezeit noch, und das Log zeigt den tatsaechlichen
# Ruhezustand-Hinweis aus __main__.py::_start_bridge/_idle.
sleep 10
RUNNING="$(docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null)"

if [ "$RUNNING" = "true" ]; then
  echo "PASS: Container mit unvollstaendiger Config laeuft weiter im Ruhezustand (kein Exit mehr seit TP7)"
else
  echo "FAIL: Container mit unvollstaendiger Config sollte im Ruhezustand weiterlaufen, State.Running war '$RUNNING'"
  FAIL=1
fi

if docker logs "$CONTAINER_NAME" 2>&1 | grep -q "Add-on ist noch nicht eingerichtet"; then
  echo "PASS: Hinweis auf die SmartHeat-Integration im Log vorhanden"
else
  echo "FAIL: erwarteter Hinweis auf die SmartHeat-Integration fehlt im Log"
  FAIL=1
fi

if docker logs "$CONTAINER_NAME" 2>&1 | grep -q "Ruhezustand: nicht_eingerichtet"; then
  echo "PASS: Ruhezustand-Log (nicht_eingerichtet) vorhanden"
else
  echo "FAIL: erwarteter Ruhezustand-Log-Eintrag (nicht_eingerichtet) fehlt"
  FAIL=1
fi

# Es gibt keinen Ingress-Wizard und keinen Flask-Server mehr, also auch nichts mehr auf
# Port 8099 zu erreichen -- der fruehere "GET / liefert 200"-Check entfaellt ersatzlos.

docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1
rm -rf "$TMPDIR"
exit $FAIL
