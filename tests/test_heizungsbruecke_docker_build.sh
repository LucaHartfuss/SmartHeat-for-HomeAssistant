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

# Ab 1.0.0 (Spec 5c) gibt es nur noch Start-Optionen; eingerichtet wird ueber das Konfigurationsdokument.
cat > "$DATA_DIR/options.json" <<JSON
{"log_level":"info"}
JSON

if command -v cygpath >/dev/null 2>&1; then
  DATA_DIR_HOST="$(cygpath -w "$DATA_DIR")"
else
  DATA_DIR_HOST="$DATA_DIR"
fi

echo "--- container run ohne Konfigurationsdokument (nicht eingerichtet) ---"
CONTAINER_NAME="heizungsbruecke-docker-build-test"
# Kein --rm hier: der Container soll nach dem Beenden absichtlich noch existieren, damit
# "docker logs" danach noch greifen kann (Cleanup passiert explizit am Skriptende).
MSYS_NO_PATHCONV=1 docker run -d --name "$CONTAINER_NAME" \
  -e SUPERVISOR_TOKEN=test-token \
  -e SMARTHEAT_BOOTSTRAP_URL=http://127.0.0.1:9 \
  -v "$DATA_DIR_HOST:/data:Z" "$IMAGE_TAG" >/dev/null \
  || { echo "FAIL: container start"; exit 1; }

# Seit TP7 (Ruhezustand statt Exit, siehe smartheat_runtime/app.py::_idle/IdleBridge, IDLE_NOT_CONFIGURED)
# beendet sich der Prozess ohne Einrichtung nicht: er bleibt im Ruhezustand "nicht_eingerichtet" am Leben, weil der
# Supervisor-Watchdog einen Exit 0 ohnehin sofort neu starten wuerde (siehe SmartHeat-for-HomeAssistant/CLAUDE.md).
# Ab 1.0.0 laeuft daneben der Geraetekern; SMARTHEAT_BOOTSTRAP_URL zeigt auf eine nicht erreichbare Adresse, damit
# der Test sich nie beim Produktionsdienst anmeldet. Erwartet wird: der Container laeuft nach kurzer Wartezeit noch,
# hat seine Geraete-Identitaet angelegt, und das Log zeigt den Ruhezustand-Hinweis.
sleep 10
RUNNING="$(docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null)"

if [ "$RUNNING" = "true" ]; then
  echo "PASS: Container ohne Einrichtung laeuft weiter im Ruhezustand (kein Exit mehr seit TP7)"
else
  echo "FAIL: Container ohne Einrichtung sollte im Ruhezustand weiterlaufen, State.Running war '$RUNNING'"
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

if docker exec "$CONTAINER_NAME" test -f /data/device/key.pem 2>/dev/null; then
  echo "PASS: Geraete-Identitaet unter /data/device angelegt"
else
  echo "FAIL: Geraetekern hat unter /data/device nichts angelegt"
  FAIL=1
fi

if docker logs "$CONTAINER_NAME" 2>&1 | grep -q "Traceback"; then
  echo "FAIL: Traceback im Log"
  docker logs "$CONTAINER_NAME" 2>&1 | tail -30
  FAIL=1
else
  echo "PASS: kein Traceback im Log"
fi

# Es gibt keinen Ingress-Wizard und keinen Flask-Server mehr, also auch nichts mehr auf
# Port 8099 zu erreichen -- der fruehere "GET / liefert 200"-Check entfaellt ersatzlos.

docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1
rm -rf "$TMPDIR"
exit $FAIL
