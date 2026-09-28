#!/bin/bash
# Belegt Praezisierung 1 des TP7-Plans: Mosquitto 2 lehnt einen Connect ab, dessen Last-Will-Topic
# die ACL nicht erlaubt. Deshalb setzt heizungsbruecke ab 0.20.0 kein Last Will mehr, und
# refresh-acl darf erst laufen, wenn kein Add-on < 0.20.0 mehr verbunden ist.
set -eu
MOSQUITTO_IMAGE="${MOSQUITTO_IMAGE:-eclipse-mosquitto:2}"
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
  mosquitto_passwd -b -c /mosquitto/config/passwd t1_a geheim >/dev/null || { echo "FAIL: passwd"; exit 1; }
chmod 700 "$TMPDIR/passwd"
docker network create "$NET_NAME" >/dev/null || { echo "FAIL: network"; exit 1; }
docker run -d --name "$BROKER" --network "$NET_NAME" --user "$HOST_UID_GID" \
  -v "$TMPDIR:/mosquitto/config:Z" "$MOSQUITTO_IMAGE" >/dev/null \
  || { echo "FAIL: broker start"; exit 1; }
sleep 2

FAIL=0
docker run --rm --network "$NET_NAME" "$MOSQUITTO_IMAGE" \
  mosquitto_pub -h "$BROKER" -u t1_a -P geheim -t smartheat/t1/up/x -m 1 \
  && echo "PASS: Connect ohne Last Will wird angenommen" || { echo "FAIL: Connect ohne Last Will abgelehnt"; FAIL=1; }
if docker run --rm --network "$NET_NAME" "$MOSQUITTO_IMAGE" \
  mosquitto_pub -h "$BROKER" -u t1_a -P geheim -t smartheat/t1/up/x -m 1 \
  --will-topic smartheat/t1/status/availability --will-payload offline --will-retain 2>"$TMPDIR/err"; then
  echo "BEFUND: Mosquitto nimmt einen Connect mit nicht erlaubtem Will-Topic an -- Praezisierung 1 (b) im Plan pruefen"
  FAIL=1
else
  echo "PASS: Connect mit nicht erlaubtem Will-Topic abgelehnt ($(cat "$TMPDIR/err"))"
fi
exit $FAIL
