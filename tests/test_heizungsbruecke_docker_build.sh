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
{"tenant_id":"test","verteilsystem":"Heizkoerper","daily_trigger_time":"12:00","day_avg_window_start":"14:00","day_avg_window_end":"17:00","night_avg_window_start":"04:00","night_avg_window_end":"07:00","accounts_api_base_url":"https://accounts.example.test","entity_room_actual":"climate.test"}
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
  -v "$DATA_DIR_HOST:/data" "$IMAGE_TAG" >/dev/null \
  || { echo "FAIL: container start"; exit 1; }

# Ab dieser Version gibt es keinen Wizard/Flask-Server mehr, der den Prozess am Leben
# haelt: main() ruft _run_bridge() jetzt synchron im Hauptthread auf. Bei unvollstaendiger
# Config kehrt _run_bridge() sofort zurueck, main() laeuft durch und der Prozess (und
# damit der Container) beendet sich sauber mit Exit 0. Konfiguriert wird das Add-on ab
# jetzt ausschliesslich durch die separate SmartHeat-Integration in Home Assistant,
# die options.json per Supervisor-API schreibt -- nicht mehr durch dieses Add-on selbst.
# "docker wait" blockiert bis der Container stoppt und liefert dann den Exit-Code --
# robuster als ein fixes "sleep" gefolgt von einer Running-Pruefung. "timeout 30" davor
# verhindert, dass eine kuenftige Regression (Prozess beendet sich nicht mehr) das Skript
# ewig haengen laesst statt schnell fehlzuschlagen -- ein Timeout liefert eine leere
# Ausgabe, die unten in den bestehenden FAIL-Zweig faellt (kein numerischer Vergleich
# noetig, der bei leerem/nicht-numerischem Wert sonst einen verwirrenden Fehler werfen
# wuerde).
EXIT_CODE="$(timeout 30 docker wait "$CONTAINER_NAME" 2>/dev/null)"

if [ "$EXIT_CODE" = "0" ]; then
  echo "PASS: Container mit unvollstaendiger Config hat sauber mit Exit 0 beendet (kein Wizard-Modus mehr)"
else
  echo "FAIL: Container mit unvollstaendiger Config sollte sauber mit Exit 0 beenden, Exit-Code war '$EXIT_CODE'"
  FAIL=1
fi

if docker logs "$CONTAINER_NAME" 2>&1 | grep -q "Add-on ist noch nicht eingerichtet"; then
  echo "PASS: Hinweis auf die SmartHeat-Integration im Log vorhanden"
else
  echo "FAIL: erwarteter Hinweis auf die SmartHeat-Integration fehlt im Log"
  FAIL=1
fi

# Es gibt keinen Ingress-Wizard und keinen Flask-Server mehr, also auch nichts mehr auf
# Port 8099 zu erreichen -- der fruehere "GET / liefert 200"-Check entfaellt ersatzlos.

docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1
rm -rf "$TMPDIR"
exit $FAIL
