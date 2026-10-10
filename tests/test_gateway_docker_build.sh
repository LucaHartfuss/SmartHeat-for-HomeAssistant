#!/bin/bash
# Gateway-Image bauen (amd64), Grundpruefung des Images und Compose-Lauf mit dem Dev-Overlay (Spec SHG G2 7.4, ab
# 0.6.0 Spec 5b 5.3): ein Gateway-Container runtime (Geraetekern, Laufzeit, Diagnoseseite); Diagnoseseite und /healthz
# antworten, Zigbee2MQTT (gleiche uid wie das Gateway) darf die vom Init-Schritt geschriebene configuration.yaml lesen
# und ersetzen; der lokale Bus verlangt Anmeldung, die ACL laesst nur Zigbee2MQTT-Topics zu; runtime schreibt
# data/device (keine Masken mehr) und hat eine Stopp-Frist ueber JOIN_SECONDS. Fake-Bootstrap und Fake-Broker (Server
# erreichbar, Uebernahme-Code) ergaenzt Plan 5b D2 Task 10. arm64 baut die CI (Job build-gateway).
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
docker run --rm "$IMAGE" sh -c "! command -v cloudflared" >/dev/null || fail "cloudflared noch im Image"
docker run --rm "$IMAGE" python -c "import smartheat_gateway.runtime_main, smartheat_gateway.init, smartheat_device.device" \
  || fail "Module nicht importierbar"
docker run --rm "$IMAGE" python -c "import smartheat_gateway.agent" 2>/dev/null && fail "Agent noch im Image"
docker run --rm "$IMAGE" sh -c "test ! -e /app/src/heizungsbruecke" || fail "HA-Host im Gateway-Image"

# Die Basisdatei verlangt SHG_DEVICE_API_URL schon beim Einlesen (${...:?}). Bis Task 10 (Fake-Bootstrap) zeigt sie
# ins Leere: der Geraetekern versucht den Bootstrap erneut, die Seite zeigt keine_verbindung.
export SHG_ROOT="$DATA" SHG_DEVICE_API_URL=http://127.0.0.1:9
if docker compose version >/dev/null 2>&1; then COMPOSE=(docker compose)
else COMPOSE=(docker run --rm --security-opt label=disable -v /var/run/docker.sock:/var/run/docker.sock
              -v "$REPO:$REPO:ro" -v "$DATA:$DATA" -e SHG_ROOT -e SHG_DEVICE_API_URL -w "$REPO" "$COMPOSE_IMAGE" docker compose); fi
COMPOSE+=(-p "$PROJECT" -f "$REPO/gateway/compose/docker-compose.yml" -f "$REPO/gateway/compose/docker-compose.dev.yml")
mkdir -p "$DATA/data" "$DATA/zigbee2mqtt" "$DATA/host"
mkdir -p "$DATA/bus/mosquitto" "$DATA/bus/credentials/runtime" "$DATA/bus/credentials/zigbee2mqtt"
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
  if curl -fsS http://127.0.0.1:8080/healthz >/dev/null 2>&1; then ok=1; break; fi
  sleep 2
done
[ "$ok" = 1 ] || fail "/healthz antwortet nicht"
curl -fsS http://127.0.0.1:8080/ | grep -q "SmartHeat" || fail "Diagnoseseite antwortet nicht"
# Fremder Host-Name (DNS-Rebinding): 403, die Seite antwortet nur unter IP, localhost und SHG_DIAG_HOSTNAMES.
[ "$(curl -s -o /dev/null -w '%{http_code}' -H 'Host: rebind.example.test' http://127.0.0.1:8080/)" = "403" ] \
  || fail "Diagnoseseite antwortet unter fremdem Host-Namen"
run_in_image --user "$GATEWAY_UID" -v "$DATA/zigbee2mqtt:/app/data" "$IMAGE" \
  sh -c 'test -r /app/data/configuration.yaml && test -w /app/data/configuration.yaml && test -w /app/data' \
  || fail "Zigbee2MQTT (uid $GATEWAY_UID) kann configuration.yaml des Init-Schritts nicht lesen/ersetzen"
MOSQ_IMAGE="eclipse-mosquitto:2@sha256:38c0da4f2ef84284d47b3b3eeea1cb3bdeabe81ee10caf0cd5c5ff61ee3ea408"
# Die Zugangsdaten gehoeren uid 1000 mit 0600: ein Wegwerf-Container als root liest sie fuer jede Host-uid (CI-Runner 1001).
pw() {
  run_in_image --user 0:0 -v "$DATA/bus:/b:ro" "$IMAGE" \
    python -c "import json,sys; print(json.load(open('/b/credentials/' + sys.argv[1] + '/bus.json'))['password'])" "$1"
}
for svc in runtime zigbee2mqtt; do [ -n "$(pw "$svc")" ] || fail "Bus-Zugangsdaten von $svc nicht lesbar"; done
mosq() { docker run --rm --security-opt label=disable --network "${PROJECT}_bus" "$MOSQ_IMAGE" "$@"; }
# Anonym abgewiesen, Dienste angemeldet, ACL greift: der Bus traegt nur noch Zigbee2MQTT (Spec 5b 5.3).
mosq mosquitto_pub -h mosquitto -t zigbee2mqtt/x -m x 2>/dev/null && fail "anonymer Zugriff auf den Bus moeglich"
got=$(mosq sh -c "mosquitto_sub -h mosquitto -u zigbee2mqtt -P '$(pw zigbee2mqtt)' -t 'zigbee2mqtt/#' -C 1 -W 6 & sleep 2;
  mosquitto_pub -h mosquitto -u runtime -P '$(pw runtime)' -t zigbee2mqtt/0x00124b0000000002/set -m from-runtime; wait" 2>/dev/null)
[ "$got" = "from-runtime" ] || fail "runtime kann zigbee2mqtt/+/set nicht schreiben (ACL zu streng)"
got=$(mosq sh -c "mosquitto_sub -h mosquitto -u zigbee2mqtt -P '$(pw zigbee2mqtt)' -t 'zigbee2mqtt/#' -C 1 -W 6 & sleep 2;
  mosquitto_pub -h mosquitto -u runtime -P '$(pw runtime)' -t zigbee2mqtt/bridge/state -m forged-by-runtime; wait" 2>/dev/null)
[ "$got" = "forged-by-runtime" ] && fail "runtime darf Zigbee2MQTT-Meldungen faelschen"
got=$(mosq sh -c "mosquitto_sub -h mosquitto -u runtime -P '$(pw runtime)' -t 'zigbee2mqtt/#' -C 1 -W 6 & sleep 2;
  mosquitto_pub -h mosquitto -u zigbee2mqtt -P '$(pw zigbee2mqtt)' -t zigbee2mqtt/bridge/state -m from-z2m; wait" 2>/dev/null)
[ "$got" = "from-z2m" ] || fail "runtime kann zigbee2mqtt/# nicht lesen (ACL zu streng)"
# Der Geraetekern im Container runtime schreibt data/device (ab 0.6.0 keine tmpfs-Masken mehr).
"${COMPOSE[@]}" exec -T runtime sh -c 'test -d /data/device && test -w /data/device' \
  || fail "runtime kann /data/device nicht schreiben"
cid=$("${COMPOSE[@]}" ps -q runtime)
docker inspect -f '{{range .Mounts}}{{if eq .Type "tmpfs"}}{{.Destination}}{{"\n"}}{{end}}{{end}}' "$cid" | grep -q "^/data" \
  && fail "runtime: tmpfs-Maske unter /data"
# Stopp-Frist ueber runtime_main.JOIN_SECONDS (10 s): sauberes Trennen vor SIGKILL.
[ "$(docker inspect -f '{{.Config.StopTimeout}}' "$cid")" = "20" ] || fail "runtime: stop_grace_period nicht 20 s"
if [ "$FAIL" = 1 ]; then "${COMPOSE[@]}" logs --no-color | tail -n 120; exit 1; fi
echo "PASS: Gateway-Image und Compose-Lauf"
