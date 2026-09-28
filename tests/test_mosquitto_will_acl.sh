#!/bin/bash
# Pinnt das Verhalten, auf das sich der Rollout verlaesst (TP7-Plan, Praezisierung 1 (a); Praez.
# 1 (b), die Rollout-Reihenfolge, ist in Task 19 geklaert, siehe dort): Mosquitto nimmt einen
# CONNECT mit einem von der ACL nicht erlaubten Last-Will-Topic an (CONNACK 0) -- eine anfangs
# angenommene Ablehnung an dieser Stelle wurde per Test widerlegt, siehe Task-11-Report. Eine
# direkte PUBLISH auf denselben Topic lehnt die ACL dagegen ganz normal ab. Deshalb verliert ein
# Add-on < 0.20.0 (das noch ein Last Will auf status/ setzt) seine Broker-Verbindung NICHT, wenn
# `refresh-acl` die ACL fuer seinen Tenant verschaerft -- sein Last Will wird beim Connect
# ignoriert, nie als Ablehnungsgrund benutzt. Dieses Skript schlaegt fehl, sobald sich das
# (broker-versionsabhaengige) Verhalten aendert.
set -eu
MOSQUITTO_IMAGE="${MOSQUITTO_IMAGE:-eclipse-mosquitto:2.0.11}"  # Produktionsversion: RPi OS Bookworm
TMPDIR="$(mktemp -d)"
NET_NAME="smartheat-will-acl-net"
BROKER="smartheat-will-acl-broker"
HOST_UID_GID="$(id -u):$(id -g)"
cleanup() {
  # set -e ist auch im Trap aktiv: jeder Schritt bekommt sein eigenes || true, sonst
  # bricht ein fehlendes Netz/Container den nachfolgenden rm -rf "$TMPDIR" ab.
  docker rm -f "$BROKER" >/dev/null 2>&1 || true
  docker network rm "$NET_NAME" >/dev/null 2>&1 || true
  rm -rf "$TMPDIR"
}
trap cleanup EXIT

cat > "$TMPDIR/mosquitto.conf" <<'EOF'
listener 1883
allow_anonymous false
password_file /mosquitto/config/passwd
acl_file /mosquitto/config/acl
log_type all
EOF
cat > "$TMPDIR/acl" <<'EOF'
user t1_a
topic write smartheat/t1/up/#
topic write smartheat/t1/telemetry
topic read smartheat/t1/down/#
EOF
chmod -R 777 "$TMPDIR"
# --user: die Passwortdatei landet sonst root-owned auf dem Host (Bind-Mount), weil der
# Mosquitto-Entrypoint ein durchgereichtes Kommando ohne eigenes --user als root ausfuehrt (F10).
# :Z auf dem Mount: SELinux (Fedora-Hosts) verweigert dem Container sonst jeden Zugriff auf das
# Bind-Mount, unabhaengig von den Unix-Rechten.
docker run --rm --user "$HOST_UID_GID" -v "$TMPDIR:/mosquitto/config:Z" "$MOSQUITTO_IMAGE" \
  mosquitto_passwd -b -c /mosquitto/config/passwd t1_a geheim >/dev/null \
  || { echo "FEHLER: Passwortdatei konnte nicht angelegt werden"; exit 1; }
chmod 700 "$TMPDIR/passwd"
docker network create "$NET_NAME" >/dev/null || { echo "FEHLER: Docker-Netz konnte nicht angelegt werden"; exit 1; }
docker run -d --name "$BROKER" --network "$NET_NAME" --user "$HOST_UID_GID" \
  -v "$TMPDIR:/mosquitto/config:Z" "$MOSQUITTO_IMAGE" >/dev/null \
  || { echo "FEHLER: Broker konnte nicht gestartet werden"; exit 1; }
sleep 2

DENIED_TOPIC="smartheat/t1/status/availability"
FAIL=0

# 1) Gegenprobe: ein Connect ganz ohne Last Will wird angenommen (die ACL blockt nicht
#    grundsaetzlich jeden Connect).
docker run --rm --network "$NET_NAME" "$MOSQUITTO_IMAGE" \
  mosquitto_pub -h "$BROKER" -u t1_a -P geheim -t smartheat/t1/up/x -m 1 \
  && echo "PASS: Connect ohne Last Will wird angenommen" \
  || { echo "FEHLER: Connect ohne Last Will wurde abgelehnt"; FAIL=1; }

# 2) Die eigentliche Annahme des Rollouts: ein Connect MIT einem von der ACL nicht erlaubten
#    Will-Topic wird trotzdem angenommen (CONNACK 0).
if docker run --rm --network "$NET_NAME" "$MOSQUITTO_IMAGE" \
  mosquitto_pub -h "$BROKER" -u t1_a -P geheim -t smartheat/t1/up/x -m 1 \
  --will-topic "$DENIED_TOPIC" --will-payload offline --will-retain 2>"$TMPDIR/err"; then
  echo "PASS: Connect mit nicht erlaubtem Will-Topic wird angenommen (CONNACK 0)"
else
  echo "FEHLER: Connect mit nicht erlaubtem Will-Topic wurde abgelehnt ($(cat "$TMPDIR/err")) -- Rollout-Annahme verletzt, Praezisierung 1 (b) mit dem Nutzer neu bewerten"
  FAIL=1
fi

# 3) Gegenprobe: die ACL wirkt tatsaechlich -- eine direkte Publish auf denselben Topic (nicht
#    ueber ein Will) wird abgelehnt. Sonst waere (2) nur eine leere/deaktivierte ACL.
BEFORE_LINES="$(docker logs "$BROKER" 2>&1 | wc -l)"
docker run --rm --network "$NET_NAME" "$MOSQUITTO_IMAGE" \
  mosquitto_pub -h "$BROKER" -u t1_a -P geheim -t "$DENIED_TOPIC" -m offline >/dev/null 2>&1 || true
NEW_LOG="$(docker logs "$BROKER" 2>&1 | tail -n "+$((BEFORE_LINES + 1))")"
if echo "$NEW_LOG" | grep -q "Denied PUBLISH.*$DENIED_TOPIC"; then
  echo "PASS: Direkte Publish auf $DENIED_TOPIC wird von der ACL abgelehnt"
else
  echo "FEHLER: Direkte Publish auf $DENIED_TOPIC wurde NICHT abgelehnt -- ACL wirkt nicht wie erwartet"
  FAIL=1
fi

exit $FAIL
