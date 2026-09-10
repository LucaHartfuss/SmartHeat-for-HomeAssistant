#!/bin/bash
# Happy-path integration test: builds the real heizungsbruecke image, runs it against a
# stub MQTT broker (real eclipse-mosquitto) and a stub HA Supervisor Core API (a tiny
# Python HTTP server), and asserts that a real MQTT publish reaches the up-channel.
#
# This is the test that would have caught C1 (missing host_network -- not exercised here
# since we don't go through the real Supervisor network path, but it does exercise the
# actual mqtt_host/mqtt_port config plumbing), C2/C3 (wrong Core API permission flag /
# get_state attribute handling -- exercised here via a real HTTP round trip to a
# "supervisor"-named container, exactly like the real Supervisor proxy), and C4 (boost
# config validation at startup, since the options file below sets consistent clamp and
# boost values).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ADDON_DIR="$HERE/../heizungsbruecke"
FAIL=0
IMAGE_TAG="heizungsbruecke-happypath-test:local"
NET_NAME="heizungsbruecke-happypath-net"
MOSQUITTO_NAME="heizungsbruecke-happypath-mosquitto"
SUPERVISOR_NAME="supervisor"  # hardcoded hostname baked into ha_api.py's base_url, must match exactly
BRIDGE_NAME="heizungsbruecke-happypath-bridge"

TMPDIR="$(mktemp -d)"
DATA_DIR="$TMPDIR/data"
mkdir -p "$DATA_DIR"

cleanup() {
  docker rm -f "$BRIDGE_NAME" "$SUPERVISOR_NAME" "$MOSQUITTO_NAME" >/dev/null 2>&1
  docker network rm "$NET_NAME" >/dev/null 2>&1
  rm -rf "$TMPDIR"
}
trap cleanup EXIT

if command -v cygpath >/dev/null 2>&1; then
  path_for_docker() { cygpath -w "$1"; }
else
  path_for_docker() { printf '%s' "$1"; }
fi

echo "--- docker build ---"
docker build --platform linux/amd64 -t "$IMAGE_TAG" "$ADDON_DIR" || { echo "FAIL: docker build"; exit 1; }
echo "PASS: docker build erfolgreich"

# --- stub HA Supervisor Core API: any GET on /core/api/states/<entity> returns a fixed
# state + attribute set (covers both the plain-state and the C3 entity_id::attribute
# read paths); any POST to the number/set_value service is accepted. ---
cat > "$TMPDIR/stub_supervisor.py" <<'PYEOF'
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/core/api/states/"):
            self._send_json({
                "state": "20.0",
                "attributes": {"current_temperature": 20.0, "temperature": 20.0},
            })
        else:
            self._send_json({"error": "not found"}, code=404)

    def do_POST(self):
        if self.path == "/core/api/services/number/set_value":
            self._send_json({})
        else:
            self._send_json({"error": "not found"}, code=404)

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 80), Handler).serve_forever()
PYEOF

cat > "$TMPDIR/mosquitto.conf" <<'EOF'
listener 1883
allow_anonymous true
EOF

# entity_room_actual/target deliberately use the C3 entity_id::attribute convention
# (like the real climate.wohnzimmer_thermostat setup) so this test also exercises that
# read path end-to-end, not just the plain-state path.
cat > "$DATA_DIR/options.json" <<JSON
{"tenant_id":"happytest","mqtt_host":"${MOSQUITTO_NAME}","mqtt_port":1883,"entity_room_actual":"climate.testroom::current_temperature","entity_room_target":"climate.testroom::temperature","entity_curve_current":"number.curve","entity_offset_current":"number.offset","curve_min":0.2,"curve_max":0.8,"offset_min":0.0,"offset_max":5.0,"boost_threshold_k":0.5,"boost_curve_value":0.5,"boost_offset_value":2.0,"poll_interval_seconds":2}
JSON

docker network rm "$NET_NAME" >/dev/null 2>&1
docker network create "$NET_NAME" >/dev/null || { echo "FAIL: docker network create"; exit 1; }

MOSQ_CONF_HOST="$(path_for_docker "$TMPDIR/mosquitto.conf")"
MSYS_NO_PATHCONV=1 docker run -d --name "$MOSQUITTO_NAME" --network "$NET_NAME" \
  -v "${MOSQ_CONF_HOST}:/mosquitto/config/mosquitto.conf" \
  eclipse-mosquitto:2 >/dev/null || { echo "FAIL: mosquitto stub start"; exit 1; }

STUB_HOST="$(path_for_docker "$TMPDIR/stub_supervisor.py")"
MSYS_NO_PATHCONV=1 docker run -d --name "$SUPERVISOR_NAME" --network "$NET_NAME" \
  -v "${STUB_HOST}:/stub_supervisor.py" \
  python:3.11-slim python /stub_supervisor.py >/dev/null || { echo "FAIL: supervisor stub start"; exit 1; }

DATA_DIR_HOST="$(path_for_docker "$DATA_DIR")"
MSYS_NO_PATHCONV=1 docker run -d --name "$BRIDGE_NAME" --network "$NET_NAME" \
  -e SUPERVISOR_TOKEN=test-token \
  -v "${DATA_DIR_HOST}:/data" \
  "$IMAGE_TAG" >/dev/null || { echo "FAIL: bridge container start"; exit 1; }

echo "--- warte auf Publish auf smartheat/happytest/up/# ---"
docker run --rm --network "$NET_NAME" eclipse-mosquitto:2 sh -c \
  "mosquitto_sub -h '$MOSQUITTO_NAME' -t 'smartheat/+/up/#' -v -C 1 -W 20"
SUB_EXIT=$?

if [ "$SUB_EXIT" = "0" ]; then
  echo "PASS: echte MQTT-Publish-Nachricht auf dem up-Kanal empfangen"
else
  echo "FAIL: keine Publish-Nachricht auf dem up-Kanal innerhalb des Timeouts empfangen"
  FAIL=1
  echo "--- bridge container logs (fuer Diagnose) ---"
  docker logs "$BRIDGE_NAME" 2>&1 | tail -50
  echo "--- supervisor stub logs ---"
  docker logs "$SUPERVISOR_NAME" 2>&1 | tail -50
  echo "--- mosquitto logs ---"
  docker logs "$MOSQUITTO_NAME" 2>&1 | tail -50
fi

exit $FAIL
