#!/bin/bash
# Gateway-Image bauen (amd64), Grundpruefung des Images und Compose-Lauf mit dem Dev-Overlay (Spec SHG G2 7.4):
# Agent registriert sich bei der Fake-Geraete-API, Diagnoseseite und /healthz antworten, Laufzeit ruht "nicht
# eingerichtet", Zigbee2MQTT (gleiche uid wie das Gateway) darf die vom Init-Schritt geschriebene configuration.yaml
# lesen und ersetzen; der lokale Bus verlangt Anmeldung und ACL, tunnel/runtime sehen die Geraeteschluessel nicht,
# die Netz-Wache startet die Laufzeit nach einem Tunnel-Neustart neu (G2b-1). arm64 baut die CI (Job build-gateway).
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
export SHG_ROOT="$DATA" SHG_DEVICE_API_URL=http://fake-device-api:8090 SHG_BUS_LOST_EXIT_SECONDS=30
if docker compose version >/dev/null 2>&1; then COMPOSE=(docker compose)
else COMPOSE=(docker run --rm --security-opt label=disable -v /var/run/docker.sock:/var/run/docker.sock
              -v "$REPO:$REPO:ro" -v "$DATA:$DATA" -e SHG_ROOT -e SHG_DEVICE_API_URL -e SHG_BUS_LOST_EXIT_SECONDS -w "$REPO" "$COMPOSE_IMAGE" docker compose); fi
COMPOSE+=(-p "$PROJECT" -f "$REPO/gateway/compose/docker-compose.yml" -f "$REPO/gateway/compose/docker-compose.dev.yml")
mkdir -p "$DATA/data" "$DATA/zigbee2mqtt" "$DATA/host"
mkdir -p "$DATA/bus/mosquitto" "$DATA/bus/credentials/agent" "$DATA/bus/credentials/runtime" \
         "$DATA/bus/credentials/zigbee2mqtt"
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
  || fail "Zigbee2MQTT (uid $GATEWAY_UID) kann configuration.yaml des Init-Schritts nicht lesen/ersetzen"
MOSQ_IMAGE="eclipse-mosquitto:2@sha256:38c0da4f2ef84284d47b3b3eeea1cb3bdeabe81ee10caf0cd5c5ff61ee3ea408"
# Die Zugangsdaten gehoeren uid 1000 mit 0600: ein Wegwerf-Container als root liest sie fuer jede Host-uid (CI-Runner 1001).
pw() {
  run_in_image --user 0:0 -v "$DATA/bus:/b:ro" "$IMAGE" \
    python -c "import json,sys; print(json.load(open('/b/credentials/' + sys.argv[1] + '/bus.json'))['password'])" "$1"
}
for svc in agent runtime zigbee2mqtt; do [ -n "$(pw "$svc")" ] || fail "Bus-Zugangsdaten von $svc nicht lesbar"; done
mosq() { docker run --rm --security-opt label=disable --network "${PROJECT}_bus" "$MOSQ_IMAGE" "$@"; }
# Anonym abgewiesen, Dienste angemeldet, ACL greift (Review Focus 5).
mosq mosquitto_pub -h mosquitto -t shg/cmd/reload -m x 2>/dev/null && fail "anonymer Zugriff auf den Bus moeglich"
got=$(mosq sh -c "mosquitto_sub -h mosquitto -u runtime -P '$(pw runtime)' -t shg/cmd/reload -C 1 -W 6 & sleep 2;
  mosquitto_pub -h mosquitto -u zigbee2mqtt -P '$(pw zigbee2mqtt)' -t shg/cmd/reload -m from-z2m; wait" 2>/dev/null)
[ "$got" = "from-z2m" ] && fail "zigbee2mqtt darf shg/cmd/reload schreiben"
got=$(mosq sh -c "mosquitto_sub -h mosquitto -u runtime -P '$(pw runtime)' -t shg/cmd/test -C 1 -W 6 & sleep 2;
  mosquitto_pub -h mosquitto -u agent -P '$(pw agent)' -t shg/cmd/test -m from-agent; wait" 2>/dev/null)
[ "$got" = "from-agent" ] || fail "agent kann shg/cmd/# nicht schreiben (ACL zu streng)"
# zigbee2mqtt darf shg/status nicht faelschen; Gegenprobe: die Laufzeit darf es und der Agent liest es.
got=$(mosq sh -c "mosquitto_sub -h mosquitto -u agent -P '$(pw agent)' -t shg/status -C 1 -W 6 & sleep 2;
  mosquitto_pub -h mosquitto -u zigbee2mqtt -P '$(pw zigbee2mqtt)' -t shg/status -m forged-by-z2m; wait" 2>/dev/null)
[ "$got" = "forged-by-z2m" ] && fail "zigbee2mqtt darf shg/status faelschen"
got=$(mosq sh -c "mosquitto_sub -h mosquitto -u agent -P '$(pw agent)' -t shg/status -C 1 -W 6 & sleep 2;
  mosquitto_pub -h mosquitto -u runtime -P '$(pw runtime)' -t shg/status -m from-runtime; wait" 2>/dev/null)
[ "$got" = "from-runtime" ] || fail "runtime kann shg/status nicht schreiben oder agent nicht lesen (ACL zu streng)"
# Schluessel in tunnel/runtime unsichtbar (Restpunkt 4).
for svc in tunnel runtime; do
  "${COMPOSE[@]}" exec -T "$svc" sh -c 'test -z "$(ls -A /data/device 2>/dev/null)"' || fail "$svc sieht /data/device"
  # Schreibgeschuetzt (Spec G2b-1 Restpunkt 4): weder device/ noch agent/ nimmt Dateien an.
  for dir in /data/device /data/agent; do
    "${COMPOSE[@]}" exec -T "$svc" sh -c "touch $dir/x" 2>/dev/null && fail "$svc darf in $dir schreiben"
  done
done
for svc in tunnel runtime; do  # Nachweis der Einbindung: tmpfs, schreibgeschuetzt
  cid=$("${COMPOSE[@]}" ps -q "$svc")
  docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data/device" "/data/agent"}}{{.Destination}} {{.Type}} rw={{.RW}}{{"\n"}}{{end}}{{end}}' "$cid" \
    | grep -c "tmpfs rw=false" | grep -qx 2 || fail "$svc: Masken sind kein schreibgeschuetztes tmpfs"
done
# Netz-Wache (Restpunkt 3): Tunnel neu starten, die Laufzeit muss sich innerhalb der Frist neu starten.
tunnel_id=$("${COMPOSE[@]}" ps -q tunnel); runtime_id=$("${COMPOSE[@]}" ps -q runtime)
docker restart "$tunnel_id" >/dev/null
ok=0
for _ in $(seq 1 60); do
  [ "$(docker inspect -f '{{.RestartCount}}' "$runtime_id")" -ge 1 ] && { ok=1; break; }; sleep 2
done
[ "$ok" = 1 ] || fail "Laufzeit nach Tunnel-Neustart nicht neu gestartet (Netz-Wache)"
if [ "$FAIL" = 1 ]; then "${COMPOSE[@]}" logs --no-color | tail -n 120; exit 1; fi
echo "PASS: Gateway-Image und Compose-Lauf"
