#!/bin/bash
# Gateway-Image bauen (amd64), Grundpruefung des Images und Compose-Lauf mit dem Dev-Overlay:
# ein Gateway-Container runtime (Geraetekern, Laufzeit, Diagnoseseite). Der
# Geraetekern bootstrappt beim Fake-Bootstrap (Test-CA signiert den CSR) und verbindet mit dem Fake-Broker (TLS,
# Client-Zertifikat, ACL wie die Geraete-Policy), up/hello kommt dort an; Seite und /healthz antworten mit
# Serverkontakt und Zustand "wartet_auf_einrichtung", ohne Uebernahme-Code (uebernommen); die Laufzeit
# ruht "nicht eingerichtet".
# Zigbee2MQTT (gleiche uid wie das Gateway) darf die vom Init-Schritt geschriebene configuration.yaml lesen und
# ersetzen; der lokale Bus verlangt Anmeldung, die ACL laesst nur Zigbee2MQTT-Topics zu; runtime schreibt data/device
# (keine Masken mehr) und hat eine Stopp-Frist ueber JOIN_SECONDS. docker stop trennt den Link sauber (MQTT-DISCONNECT
# am Fake-Broker, Review Focus 1). arm64 baut die CI (Job build-gateway).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
IMAGE="smartheat-gateway-test:local"
PROJECT="shg-gateway-test"
DATA="$(mktemp -d)"
COMPOSE_IMAGE="docker:29-cli@sha256:b1805116a6a86cc591b5d5f60a910a0715cdcc9d18d866ad68b1457ead25c35c"
# uid:gid des Gateway-Images und des Dienstes zigbee2mqtt (docker-compose.yml, user:).
GATEWAY_UID="1000:1000"
MOSQ_IMAGE="eclipse-mosquitto:2@sha256:38c0da4f2ef84284d47b3b3eeea1cb3bdeabe81ee10caf0cd5c5ff61ee3ea408"
# Test-PKI (je Lauf neu) und Fake-Broker im Netz wan des Compose-Projekts (gateway_lib.sh: dienst_pub, dienst_wait).
PKI="$DATA/pki"; BROKER="fake-broker"; NET="${PROJECT}_wan"
# shellcheck source=tests/gateway_lib.sh
source "$HERE/gateway_lib.sh"
FAIL=0
fail() { echo "FAIL: $1"; FAIL=1; }
# label=disable: auf SELinux-Hosts (Fedora) duerfen Container die Bind-Mounts sonst nicht lesen (wie tools/e2e).
run_in_image() { docker run --rm --security-opt label=disable "$@"; }

docker build --platform linux/amd64 -f "$REPO/gateway/Dockerfile" -t "$IMAGE" "$REPO" || { echo "FAIL: docker build"; exit 1; }
echo "PASS: docker build"
[ "$(docker run --rm "$IMAGE" id -u)" = "1000" ] || fail "Image laeuft nicht als uid 1000"
docker run --rm "$IMAGE" python -c "import smartheat_gateway.runtime_main, smartheat_gateway.init, smartheat_gateway.diagnose, smartheat_device.device" \
  || fail "Module nicht importierbar"
docker run --rm "$IMAGE" sh -c "test ! -e /app/src/heizungsbruecke" || fail "HA-Host im Gateway-Image"

# Der Geraetekern bootstrappt beim Fake-Bootstrap des Dev-Overlays (Dienstname fake-bootstrap im Netz wan).
export SHG_ROOT="$DATA" SHG_DEVICE_API_URL=http://fake-bootstrap:8090 SHG_FAKE_PKI="$PKI" SHG_FAKE_USER="$PKI_USER"
if docker compose version >/dev/null 2>&1; then COMPOSE=(docker compose)
else COMPOSE=(docker run --rm --security-opt label=disable -v /var/run/docker.sock:/var/run/docker.sock
              -v "$REPO:$REPO:ro" -v "$DATA:$DATA" -e SHG_ROOT -e SHG_DEVICE_API_URL -e SHG_FAKE_PKI -e SHG_FAKE_USER
              -w "$REPO"
              "$COMPOSE_IMAGE" docker compose); fi
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
# Test-PKI erst nach dem trap: scheitert sie, raeumt cleanup den CA-Schluessel trotzdem weg. Nach dem chmod 777 oben,
# damit die Schluessel 0600 bleiben.
{ make_pki "$PKI" "$BROKER" && broker_conf "$PKI"; } || { echo "FAIL: Test-PKI"; exit 1; }

"${COMPOSE[@]}" up -d --build || { echo "FAIL: compose up"; "${COMPOSE[@]}" logs --no-color | tail -n 80; exit 1; }
ok=0
for _ in $(seq 1 45); do
  curl -fsS http://127.0.0.1:18090/_e2e/state 2>/dev/null | grep -q '"registrations": [1-9]' && { ok=1; break; }
  sleep 2
done
[ "$ok" = 1 ] || fail "Geraetekern hat sich nicht beim Fake-Bootstrap registriert"
device_id="$(curl -fsS http://127.0.0.1:18090/_e2e/state \
  | python3 -c 'import json,sys; ids = json.load(sys.stdin)["device_ids"]; print(ids[0] if ids else "")')"
[ -n "$device_id" ] || fail "Fake-Bootstrap kennt keine Geraete-ID"
# Verbunden mit dem Fake-Broker: /healthz meldet Serverkontakt; uebernommen, aber ohne Konfiguration wartet das Gateway
# auf die Einrichtung, die Laufzeit ruht (nicht eingerichtet) und die Seite zeigt keinen Uebernahme-Code mehr
# (Anzeige.code_zeigen).
ok=0
for _ in $(seq 1 30); do
  curl -fsS http://127.0.0.1:8080/healthz 2>/dev/null | grep -q '"server_ok": true' && { ok=1; break; }; sleep 2
done
[ "$ok" = 1 ] || fail "/healthz meldet keinen Serverkontakt"
healthz="$(curl -fsS http://127.0.0.1:8080/healthz 2>/dev/null)"
printf '%s' "$healthz" | grep -q '"zustand": "wartet_auf_einrichtung"' \
  || fail "/healthz: Zustand nicht wartet_auf_einrichtung ($healthz)"
# Ruhezustand ohne Konfiguration: kein Laufzeitstatus (runtime_main.attach), Hinweis im Log (host.NOT_CONFIGURED_HINT).
printf '%s' "$healthz" | grep -q '"runtime_status": null' || fail "/healthz: Laufzeit ruht nicht ($healthz)"
"${COMPOSE[@]}" logs runtime 2>/dev/null | grep -q "noch nicht eingerichtet" || fail "Laufzeit nicht im Ruhezustand"
# Gesundheitsserver (Compose-Healthcheck und Updater): nur /healthz, eigener Port, auf dem Loopback des Hosts veroeffentlicht.
[ "$(curl -fsS http://127.0.0.1:8081/healthz 2>/dev/null)" = "$healthz" ] || fail "Gesundheitsserver auf 8081 antwortet anders als /healthz"
[ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8081/ 2>/dev/null)" = 404 ] || fail "Gesundheitsserver zeigt mehr als /healthz"
page="$(curl -fsS http://127.0.0.1:8080/ 2>/dev/null)"
printf '%s' "$page" | grep -q "SmartHeat-Gateway" || fail "Diagnoseseite antwortet nicht"
! printf '%s' "$page" | grep -q "Übernahme-Code" || fail "Diagnoseseite zeigt nach der Uebernahme den Code"
printf '%s' "$page" | grep -q "übernommen und wartet auf die Einrichtung" || fail "Diagnoseseite ohne Zustandstext"
unset page
# up/hello am Fake-Broker (Rolle Dienst): hello ging beim ersten Verbinden vor jedem Abo raus; das Abo laeuft deshalb
# vor einem Neustart der Laufzeit, deren neuer Prozess sich mit derselben Identitaet meldet.
dienst_wait "$NET" "$PKI" "smartheat/$device_id/up/hello" 90 >"$DATA/hello" & waiter=$!
sleep 3
"${COMPOSE[@]}" restart runtime >/dev/null 2>&1 || fail "Neustart der Laufzeit"
wait "$waiter"
hello="$(cat "$DATA/hello")"
[ -n "$hello" ] || fail "kein up/hello am Fake-Broker"
printf '%s' "$hello" | grep -q '"host": *"gateway"' || fail "hello ohne host gateway"
python3 -c 'import json,sys; sys.exit(0 if json.loads(sys.argv[1]).get("konfiguration_version") == 0 else 1)' "$hello" \
  2>/dev/null || fail "hello: Konfigurationsversion nicht 0 ($hello)"
# Fremder Host-Name (DNS-Rebinding): 403, die Seite antwortet nur unter IP, localhost und SHG_DIAG_HOSTNAMES.
[ "$(curl -s -o /dev/null -w '%{http_code}' -H 'Host: rebind.example.test' http://127.0.0.1:8080/)" = "403" ] \
  || fail "Diagnoseseite antwortet unter fremdem Host-Namen"
run_in_image --user "$GATEWAY_UID" -v "$DATA/zigbee2mqtt:/app/data" "$IMAGE" \
  sh -c 'test -r /app/data/configuration.yaml && test -w /app/data/configuration.yaml && test -w /app/data' \
  || fail "Zigbee2MQTT (uid $GATEWAY_UID) kann configuration.yaml des Init-Schritts nicht lesen/ersetzen"
# Die Zugangsdaten gehoeren uid 1000 mit 0600: ein Wegwerf-Container als root liest sie fuer jede Host-uid (CI-Runner 1001).
pw() {
  run_in_image --user 0:0 -v "$DATA/bus:/b:ro" "$IMAGE" \
    python -c "import json,sys; print(json.load(open('/b/credentials/' + sys.argv[1] + '/bus.json'))['password'])" "$1"
}
for svc in runtime zigbee2mqtt; do [ -n "$(pw "$svc")" ] || fail "Bus-Zugangsdaten von $svc nicht lesbar"; done
mosq() { docker run --rm --security-opt label=disable --network "${PROJECT}_bus" "$MOSQ_IMAGE" "$@"; }
# Anonym abgewiesen, Dienste angemeldet, ACL greift: der Bus traegt nur noch Zigbee2MQTT. Die Abos
# ignorieren retained Nachrichten (-R): Fake-Zigbee2MQTT haelt bridge/* retained, das kaeme sonst vor der Probe an.
mosq mosquitto_pub -h mosquitto -t zigbee2mqtt/x/set -m x 2>/dev/null && fail "anonymer Zugriff auf den Bus moeglich"
# runtime darf Thermostate setzen und die Bruecke anfragen, zigbee2mqtt liest beides (Gegenprobe der ACL).
got=$(mosq sh -c "mosquitto_sub -R -h mosquitto -u zigbee2mqtt -P '$(pw zigbee2mqtt)' -t zigbee2mqtt/0x1/set -C 1 -W 6 & sleep 2;
  mosquitto_pub -h mosquitto -u runtime -P '$(pw runtime)' -t zigbee2mqtt/0x1/set -m from-runtime; wait" 2>/dev/null)
[ "$got" = "from-runtime" ] || fail "runtime kann zigbee2mqtt/+/set nicht schreiben (ACL zu streng)"
got=$(mosq sh -c "mosquitto_sub -R -h mosquitto -u zigbee2mqtt -P '$(pw zigbee2mqtt)' -t zigbee2mqtt/bridge/request/permit_join -C 1 -W 6 & sleep 2;
  mosquitto_pub -h mosquitto -u runtime -P '$(pw runtime)' -t zigbee2mqtt/bridge/request/permit_join -m from-runtime; wait" 2>/dev/null)
[ "$got" = "from-runtime" ] || fail "runtime kann permit_join nicht anfragen (ACL zu streng)"
# runtime liest, was Zigbee2MQTT meldet, darf es aber nicht faelschen: bridge/state und bridge/devices nimmt die
# Laufzeit fuer wahr (zigbee.ZigbeeMirror._on_message), ausserhalb von zigbee2mqtt/ darf sie gar nichts.
got=$(mosq sh -c "mosquitto_sub -R -h mosquitto -u runtime -P '$(pw runtime)' -t zigbee2mqtt/0x00124b00000000ff -C 1 -W 6 & sleep 2;
  mosquitto_pub -h mosquitto -u zigbee2mqtt -P '$(pw zigbee2mqtt)' -t zigbee2mqtt/0x00124b00000000ff -m from-z2m; wait" 2>/dev/null)
[ "$got" = "from-z2m" ] || fail "runtime kann zigbee2mqtt/# nicht lesen (ACL zu streng)"
# forged TOPIC: publiziert als runtime auf TOPIC, Ausgabe = was zigbee2mqtt (liest zigbee2mqtt/#) dort sieht.
forged() {
  mosq sh -c "mosquitto_sub -R -h mosquitto -u zigbee2mqtt -P '$(pw zigbee2mqtt)' -t '$1' -C 1 -W 6 & sleep 2;
    mosquitto_pub -h mosquitto -u runtime -P '$(pw runtime)' -t '$1' -m forged; wait" 2>/dev/null
}
for topic in zigbee2mqtt/bridge/state zigbee2mqtt/bridge/devices; do
  [ "$(forged "$topic")" = "forged" ] && fail "runtime darf $topic faelschen"
done
# Reason Code des PUBACK (MQTT 5, QoS 1) fuer einen Publish als runtime: 0 erlaubt, 135 nicht autorisiert. Prueft auch
# Topics, die kein Bus-Dienst lesen darf (ausserhalb von zigbee2mqtt/). Gegenprobe: zigbee2mqtt/+/set ist erlaubt.
puback() {
  mosq mosquitto_pub -d -V mqttv5 -q 1 -h mosquitto -u runtime -P "$(pw runtime)" -t "$1" -m forged 2>&1 \
    | sed -n 's/.*received PUBACK.*RC:\([0-9]*\).*/\1/p'
}
[ "$(puback zigbee2mqtt/0x1/set)" = "0" ] || fail "PUBACK-Probe: erlaubter Publish nicht mit RC 0 bestaetigt"
for topic in zigbee2mqtt/bridge/state zigbee2mqtt/bridge/devices smartheat/x; do
  [ "$(puback "$topic")" = "135" ] || fail "runtime darf auf $topic veroeffentlichen (PUBACK nicht 135)"
done
# Der Geraetekern im Container runtime schreibt data/device (keine tmpfs-Masken).
"${COMPOSE[@]}" exec -T runtime sh -c 'test -d /data/device && test -w /data/device' \
  || fail "runtime kann /data/device nicht schreiben"
cid=$("${COMPOSE[@]}" ps -q runtime)
docker inspect -f '{{range .Mounts}}{{if eq .Type "tmpfs"}}{{.Destination}}{{"\n"}}{{end}}{{end}}' "$cid" | grep -q "^/data" \
  && fail "runtime: tmpfs-Maske unter /data"
# Stopp-Frist ueber runtime_main.JOIN_SECONDS (10 s): sauberes Trennen vor SIGKILL.
[ "$(docker inspect -f '{{.Config.StopTimeout}}' "$cid")" = "20" ] || fail "runtime: stop_grace_period nicht 20 s"
# Docker stop -> SIGTERM -> sauberes MQTT-DISCONNECT. Mosquitto 2.1 schreibt dann "Client <id>
# [<adresse>] disconnected."; bei einem Abbruch "... closed its connection." bzw. "... has exceeded timeout". Gezaehlt
# wird ab hier (der Neustart oben hat schon eine Trennung protokolliert).
broker_log() { "${COMPOSE[@]}" logs --no-color fake-broker 2>/dev/null; }
clean="Client $device_id( \[[^]]*\])? disconnected\."
clean_before=$(broker_log | grep -cE "$clean")
"${COMPOSE[@]}" stop runtime >/dev/null 2>&1 || fail "Laufzeit stoppt nicht"
[ "$(docker inspect -f '{{.State.ExitCode}}' "$cid")" = "0" ] || fail "runtime endet nach SIGTERM nicht mit Exit 0"
ok=0
for _ in $(seq 1 10); do  # der Broker schreibt die Zeile womoeglich erst nach dem Ende von compose stop
  [ "$(broker_log | grep -cE "$clean")" -gt "$clean_before" ] && { ok=1; break; }
  sleep 0.5
done
[ "$ok" = 1 ] || fail "Laufzeit trennt beim Stoppen nicht sauber (kein DISCONNECT)"
# Jede andere Zeile "Client <id> ..." ist ein Abbruch (closed its connection, exceeded timeout, Socket error,
# disconnected due to ...). Ausnahme: "session taken over" fuer eine Verbindung, die vorher sauber getrennt hat (die
# persistente Session wechselt beim Neustart auf die neue Verbindung).
abbrueche="$(broker_log | python3 -c '
import re, sys
device = sys.argv[1]
line_re = re.compile(r"Client " + re.escape(device) + r"(?: \[([^]]*)\])? (.*)$")
clean = set()
for line in sys.stdin:
    match = line_re.search(line.rstrip())
    if not match:
        continue
    addr, rest = match.groups()
    if rest == "disconnected.":
        clean.add(addr)
    elif rest.startswith("negotiated TLS"):
        continue
    elif rest == "disconnected: session taken over." and addr in clean:
        continue
    else:
        print(line.rstrip())
' "$device_id")"
[ -z "$abbrueche" ] || fail "Fake-Broker meldet Abbrueche der Geraeteverbindung: $abbrueche"
# Test-PKI und Konfiguration gehoeren dem Benutzer, als der der Fake-Broker laeuft (0600): keine Warnungen von Mosquitto.
warnungen="$(broker_log | grep "Warning:")"
[ -z "$warnungen" ] || fail "Fake-Broker warnt: $warnungen"
if [ "$FAIL" = 1 ]; then "${COMPOSE[@]}" logs --no-color | tail -n 120; exit 1; fi
echo "PASS: Gateway-Image und Compose-Lauf"
