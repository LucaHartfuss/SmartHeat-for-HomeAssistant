#!/bin/bash
# Gateway-Image bauen (amd64), Grundpruefung des Images und Compose-Lauf mit dem Dev-Overlay (Spec SHG G2 7.4):
# Agent registriert sich bei der Fake-Geraete-API, Diagnoseseite und /healthz antworten, Laufzeit ruht "nicht
# eingerichtet", Zigbee2MQTT (gleiche uid wie das Gateway) darf die vom Agenten geschriebene configuration.yaml lesen
# und ersetzen. arm64 baut die CI (Job build-gateway).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
IMAGE="smartheat-gateway-test:local"
PROJECT="shg-gateway-test"
DATA="$(mktemp -d)"
COMPOSE_IMAGE="docker:29-cli@sha256:b1805116a6a86cc591b5d5f60a910a0715cdcc9d18d866ad68b1457ead25c35c"
# uid:gid des Gateway-Images und des Dienstes zigbee2mqtt (docker-compose.yml, user:).
GATEWAY_UID="1000:1000"
FAIL=0
fail() { echo "FAIL: $1"; FAIL=1; }
# label=disable: auf SELinux-Hosts (Fedora) duerfen Container die Bind-Mounts sonst nicht lesen (wie tools/e2e).
run_in_image() { docker run --rm --security-opt label=disable "$@"; }

docker build --platform linux/amd64 -f "$REPO/gateway/Dockerfile" -t "$IMAGE" "$REPO" || { echo "FAIL: docker build"; exit 1; }
echo "PASS: docker build"
[ "$(docker run --rm "$IMAGE" id -u)" = "1000" ] || fail "Image laeuft nicht als uid 1000"
docker run --rm "$IMAGE" cloudflared --version >/dev/null || fail "cloudflared fehlt im Image"
docker run --rm "$IMAGE" python -c "import smartheat_gateway.agent.loop, smartheat_gateway.runtime_main, smartheat_gateway.tunnel" \
  || fail "Module nicht importierbar"
docker run --rm "$IMAGE" sh -c "test ! -e /app/src/heizungsbruecke" || fail "HA-Host im Gateway-Image"

# Die Basisdatei verlangt SHG_DEVICE_API_URL schon beim Einlesen (${...:?}); das Overlay setzt dieselbe URL.
export SHG_ROOT="$DATA" SHG_DEVICE_API_URL=http://fake-device-api:8090
if docker compose version >/dev/null 2>&1; then COMPOSE=(docker compose)
else COMPOSE=(docker run --rm --security-opt label=disable -v /var/run/docker.sock:/var/run/docker.sock
              -v "$REPO:$REPO:ro" -v "$DATA:$DATA" -e SHG_ROOT -e SHG_DEVICE_API_URL -w "$REPO" "$COMPOSE_IMAGE" docker compose); fi
COMPOSE+=(-p "$PROJECT" -f "$REPO/gateway/compose/docker-compose.yml" -f "$REPO/gateway/compose/docker-compose.dev.yml")
mkdir -p "$DATA/data" "$DATA/zigbee2mqtt" "$DATA/host"
chmod -R 777 "$DATA"
# Die Container legen Ordner und Dateien als uid 1000 mit 0700/0600 an (device/, secrets/, configuration.yaml); ein
# Skript unter anderer uid (CI-Runner 1001) kann sie nicht loeschen. Deshalb raeumt ein Wegwerf-Container als root.
cleanup() {
  "${COMPOSE[@]}" down -v --rmi local >/dev/null 2>&1
  run_in_image --user 0:0 -v "$DATA:/d" "$IMAGE" sh -c 'find /d -mindepth 1 -delete' >/dev/null 2>&1
  rm -rf "$DATA"
}
trap cleanup EXIT

"${COMPOSE[@]}" up -d --build || { echo "FAIL: compose up"; exit 1; }
ok=0
for _ in $(seq 1 45); do
  if curl -fsS http://127.0.0.1:18090/_e2e/state 2>/dev/null | grep -q '"registrations": [1-9]'; then ok=1; break; fi
  sleep 2
done
[ "$ok" = 1 ] || fail "Agent hat sich nicht bei der Fake-Geraete-API registriert"
curl -fsS http://127.0.0.1:8080/healthz | grep -q '"server_ok": true' || fail "/healthz meldet keinen Serverkontakt"
curl -fsS http://127.0.0.1:8080/ | grep -q "<svg\|Übernahme-Code" || fail "Diagnoseseite ohne Uebernahme-Code"
# Fremder Host-Name (DNS-Rebinding): 403, die Seite antwortet nur unter IP, localhost und SHG_DIAG_HOSTNAMES.
[ "$(curl -s -o /dev/null -w '%{http_code}' -H 'Host: rebind.example.test' http://127.0.0.1:8080/)" = "403" ] \
  || fail "Diagnoseseite antwortet unter fremdem Host-Namen"
"${COMPOSE[@]}" logs runtime 2>/dev/null | grep -q "nicht eingerichtet" || fail "Laufzeit nicht im Ruhezustand"
run_in_image --user "$GATEWAY_UID" -v "$DATA/zigbee2mqtt:/app/data" "$IMAGE" \
  sh -c 'test -r /app/data/configuration.yaml && test -w /app/data/configuration.yaml && test -w /app/data' \
  || fail "Zigbee2MQTT (uid $GATEWAY_UID) kann configuration.yaml des Agenten nicht lesen/ersetzen"
if [ "$FAIL" = 1 ]; then "${COMPOSE[@]}" logs --no-color | tail -n 120; exit 1; fi
echo "PASS: Gateway-Image und Compose-Lauf"
