#!/bin/bash
# Happy-path integration test: builds the real heizungsbruecke image, runs it against a
# stub MQTT broker (real eclipse-mosquitto) and a stub HA Supervisor Core API (a tiny
# Python HTTP server), and asserts that a real MQTT publish reaches the up-channel.
#
# This is the test that would have caught C1 (missing host_network -- approximated here
# via --network container:<mosquitto>, sharing a network namespace the same way
# host_network:true makes this add-on and cloudflared_access_mqtt share the real Pi's
# network stack), C2/C3 (wrong Core API permission flag / get_state attribute handling --
# exercised via a real HTTP round trip to a "supervisor"-named container), and the
# automatic room-temperature template helper provisioning against the stub's config-entry-flow
# (REST) and entity-registry (hand-rolled WebSocket handshake) endpoints. Since TP11 (0.24.0)
# the options also map the parallel shift (a climate zone, already in manual mode = heat_cool)
# and the minimum flow temperature; both already match the room target, so the start writes
# nothing to the stub plant.
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
# read paths); the climate zone (parallel shift) is in manual mode (heat_cool) with a
# target temperature. POSTs to the number/climate services are accepted. ---
cat > "$TMPDIR/stub_supervisor.py" <<'PYEOF'
import base64
import hashlib
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_FLOW_COUNTER = {"n": 0}
_ENTITY_REGISTRY = []
_WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _recv_ws_frame(rfile) -> dict:
    header = rfile.read(2)
    length = header[1] & 0x7F
    if length == 126:
        length = int.from_bytes(rfile.read(2), "big")
    elif length == 127:
        length = int.from_bytes(rfile.read(8), "big")
    mask = rfile.read(4)
    payload = rfile.read(length)
    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return json.loads(payload.decode())


def _send_ws_frame(wfile, obj) -> None:
    payload = json.dumps(obj).encode()
    length = len(payload)
    if length < 126:
        header = bytes([0x81, length])
    else:
        header = bytes([0x81, 126]) + length.to_bytes(2, "big")
    wfile.write(header + payload)
    wfile.flush()


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.headers.get("Upgrade", "").lower() == "websocket":
            key = self.headers["Sec-WebSocket-Key"]
            accept = base64.b64encode(hashlib.sha1((key + _WS_MAGIC).encode()).digest()).decode()
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            _send_ws_frame(self.wfile, {"type": "auth_required"})
            _recv_ws_frame(self.rfile)  # auth message, not checked -- stub trusts everyone
            _send_ws_frame(self.wfile, {"type": "auth_ok"})
            command = _recv_ws_frame(self.rfile)
            result = self._fake_ws_result(command)
            _send_ws_frame(self.wfile, {"id": command.get("id", 1), "type": "result", "success": True, "result": result})
            return
        if self.path == "/core/api/config":
            # "state": the bridge only starts once HA reports RUNNING (ha_api.is_reachable).
            self._send_json({"time_zone": "UTC", "state": "RUNNING"})
            return
        if self.path == "/core/api/states/climate.zone":
            self._send_json({"state": "heat_cool", "attributes": {"temperature": 20.0}})
        elif self.path.startswith("/core/api/states/"):
            self._send_json({
                "state": "20.0",
                "attributes": {"current_temperature": 20.0, "temperature": 20.0},
            })
        else:
            self._send_json({"error": "not found"}, code=404)

    def _fake_ws_result(self, command):
        cmd_type = command.get("type")
        if cmd_type in ("config/entity_registry/list", "config/entity_registry/list_for_display"):
            return list(_ENTITY_REGISTRY)
        return {}

    def do_POST(self):
        if self.path in (
            "/core/api/services/number/set_value",
            "/core/api/services/climate/set_temperature",
            "/core/api/services/climate/set_hvac_mode",
        ):
            self._send_json({})
        elif self.path == "/core/api/config/config_entries/flow":
            _FLOW_COUNTER["n"] += 1
            self._send_json({
                "flow_id": f"flow-{_FLOW_COUNTER['n']}",
                "type": "form",
                "step_id": "user",
                "data_schema": [
                    {"name": "name"}, {"name": "entity_id"}, {"name": "state_characteristic"},
                    {"name": "max_age"}, {"name": "sampling_size"}, {"name": "precision"},
                ],
            })
        elif re.match(r"^/core/api/config/config_entries/flow/flow-\d+$", self.path):
            entry_id = f"entry-{self.path.rsplit('-', 1)[1]}"
            entity_id = f"sensor.stub_derived_{entry_id}"
            _ENTITY_REGISTRY.append({"entity_id": entity_id, "config_entry_id": entry_id})
            self._send_json({"type": "create_entry", "result": {"entry_id": entry_id}})
        elif self.path.startswith("/core/api/events/"):
            self._send_json({"message": "Event fired."})
        elif self.path.startswith("/core/api/services/persistent_notification/"):
            self._send_json({})
        else:
            self._send_json({"error": "not found"}, code=404)

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 80), Handler).serve_forever()
PYEOF

cat > "$TMPDIR/mosquitto.conf" <<'EOF'
listener 18830
allow_anonymous true
EOF

# room_sensors/entity_room_target deliberately use the C3 entity_id::attribute convention
# (like the real climate.wohnzimmer_thermostat setup) so this test also exercises that
# read path end-to-end, not just the plain-state path. transport (descriptor as JSON string)
# and installation_token are required since 0.32.0, mqtt_username/mqtt_password are required
# for the password credential, even though the stub broker accepts any value (allow_anonymous
# true below) -- without them the bridge would just report "Konfiguration veraltet" and idle
# without ever touching MQTT.
cat > "$DATA_DIR/options.json" <<JSON
{"tenant_id":"happytest","verteilsystem":"Heizkoerper","daily_trigger_time":"12:00","accounts_api_base_url":"https://accounts.example.test","transport":"{\"kind\":\"mosquitto_cloudflared\",\"host\":\"127.0.0.1\",\"port\":18830}","installation_token":"happy-token","mqtt_username":"heizungsbruecke","mqtt_password":"test-secret","room_sensors":["climate.testroom::current_temperature"],"entity_room_target":"climate.testroom::temperature","entity_curve_current":"number.curve","entity_shift_current":"climate.zone","entity_min_flow":"number.min_flow","entity_heat_limit":"number.heat_limit","entity_outdoor_temp":"sensor.outdoor","local_check_interval_seconds":2}
JSON

# $TMPDIR is created via `mktemp -d` (mode 0700) and Docker may run the containers below
# under a different (e.g. remapped/rootless) uid than this script's -- without this, reads
# of the bind-mounted files (mosquitto.conf, stub_supervisor.py, options.json) and writes
# into $DATA_DIR (backup.json etc., created by the bridge at runtime) can fail with
# "Permission denied" even though the files/dirs look fine from this shell. Test-only
# scratch directory, removed by `cleanup` on exit -- permissive mode is not a concern here.
chmod -R 777 "$TMPDIR"

dump_logs() {
  for name in "$@"; do
    echo "--- $name logs (fuer Diagnose) ---"
    docker logs "$name" 2>&1 | tail -50
  done
}

docker network rm "$NET_NAME" >/dev/null 2>&1
docker network create "$NET_NAME" >/dev/null || { echo "FAIL: docker network create"; exit 1; }

MOSQ_CONF_HOST="$(path_for_docker "$TMPDIR/mosquitto.conf")"
MSYS_NO_PATHCONV=1 docker run -d --name "$MOSQUITTO_NAME" --network "$NET_NAME" \
  -v "${MOSQ_CONF_HOST}:/mosquitto/config/mosquitto.conf:Z" \
  eclipse-mosquitto:2 >/dev/null || { echo "FAIL: mosquitto stub start"; dump_logs "$MOSQUITTO_NAME"; exit 1; }

STUB_HOST="$(path_for_docker "$TMPDIR/stub_supervisor.py")"
MSYS_NO_PATHCONV=1 docker run -d --name "$SUPERVISOR_NAME" --network "$NET_NAME" \
  -v "${STUB_HOST}:/stub_supervisor.py:Z" \
  python:3.11-slim python /stub_supervisor.py >/dev/null || { echo "FAIL: supervisor stub start"; dump_logs "$SUPERVISOR_NAME"; exit 1; }

DATA_DIR_HOST="$(path_for_docker "$DATA_DIR")"
MSYS_NO_PATHCONV=1 docker run -d --name "$BRIDGE_NAME" --network "container:$MOSQUITTO_NAME" \
  -e SUPERVISOR_TOKEN=test-token \
  -v "${DATA_DIR_HOST}:/data:Z" \
  "$IMAGE_TAG" >/dev/null || { echo "FAIL: bridge container start"; dump_logs "$MOSQUITTO_NAME" "$SUPERVISOR_NAME"; exit 1; }

echo "--- warte auf Publish auf smartheat/happytest/up/# ---"
docker run --rm --network "$NET_NAME" eclipse-mosquitto:2 sh -c \
  "mosquitto_sub -h '$MOSQUITTO_NAME' -p 18830 -t 'smartheat/+/up/#' -v -C 1 -W 20"
SUB_EXIT=$?

if [ "$SUB_EXIT" = "0" ]; then
  echo "PASS: echte MQTT-Publish-Nachricht auf dem up-Kanal empfangen"
else
  echo "FAIL: keine Publish-Nachricht auf dem up-Kanal innerhalb des Timeouts empfangen"
  FAIL=1
  dump_logs "$BRIDGE_NAME" "$SUPERVISOR_NAME" "$MOSQUITTO_NAME"
fi

exit $FAIL
