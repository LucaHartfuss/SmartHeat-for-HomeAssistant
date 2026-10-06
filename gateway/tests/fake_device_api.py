"""Fake-Geraete-API (Spec SHG G2 7.3) nach dem Vertrag agent/wire.py: prueft Signatur, Zeitstempel und Body-Grenze
wie der Server (G3 2.1), liefert Befehle erneut bis zum Ergebnis, erstes Ergebnis gilt. Unit-Tests starten sie im
Prozess; die E2E (tools/e2e) startet main() im Gateway-Image. Steuer-Routen /_e2e/* sind unsigniert (nur Test)."""
import base64
import contextlib
import json
import os
import threading
import time
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from smartheat_gateway.agent import wire
from smartheat_gateway.agent.identity import device_id_for


def _route(method: str, path: str):
    parts = path.strip("/").split("/")
    for name, (route_method, template) in wire.ROUTES.items():
        template_parts = template.strip("/").split("/")
        if route_method != method or len(template_parts) != len(parts):
            continue
        params = {}
        for template_part, part in zip(template_parts, parts, strict=True):
            if template_part.startswith("{"):
                params[template_part.strip("{}")] = part
            elif template_part != part:
                break
        else:
            return name, params
    return None, {}


class FakeDeviceApi:
    def __init__(self, host: str = "127.0.0.1", port: int = 0, *, now=time.time, poll_after: int = 1) -> None:
        self._now = now
        self.poll_after = poll_after
        self.lock = threading.Lock()
        self.devices: dict[str, dict] = {}
        self.commands: dict[str, dict] = {}
        self.statuses: dict[str, list[dict]] = {}
        self.notifications: dict[tuple[str, str], dict] = {}
        self.registrations = 0
        self.result_status: int | None = None  # nur Tests: Ergebnis-Route antwortet mit diesem HTTP-Status
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                api._handle(self, "GET")

            def do_POST(self):
                api._handle(self, "POST")

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    # --- Steuerung (Tests, E2E) ---

    def _only(self, device_id):
        if device_id is None:
            (device_id,) = self.devices
        return device_id

    def claim(self, device_id=None) -> None:
        with self.lock:
            self.devices[self._only(device_id)]["state"] = "uebernommen"

    def release(self, device_id=None) -> None:
        with self.lock:
            self.devices[self._only(device_id)]["state"] = "nicht_uebernommen"

    def block(self, device_id=None) -> None:
        with self.lock:
            self.devices[self._only(device_id)]["state"] = "gesperrt"

    def enqueue(self, kind: str, payload: dict, device_id=None, expires_in=None) -> str:
        command = wire.COMMANDS[kind]
        seconds = expires_in or command.expires_seconds or (int(payload.get("stunden", 1)) * 3600 + 3600)
        with self.lock:
            command_id = uuid.uuid4().hex
            self.commands[command_id] = {
                "device_id": self._only(device_id), "kind": kind, "payload": payload, "result": None,
                "expires_at": self._now() + seconds,
            }
        return command_id

    def result_of(self, command_id: str):
        with self.lock:
            return self.commands[command_id]["result"]

    def last_status(self, device_id=None):
        with self.lock:
            entries = self.statuses.get(self._only(device_id), [])
            return entries[-1] if entries else None

    # --- HTTP ---

    def _handle(self, request, method: str) -> None:
        path = urlsplit(request.path).path
        length = int(request.headers.get("Content-Length") or 0)
        if length > wire.MAX_BODY_BYTES:
            return self._send(request, 413, {"error": "zu gross"})
        body = request.rfile.read(length) if length else b""
        if path.startswith("/_e2e/"):
            return self._control(request, method, path, body)
        name, params = _route(method, path)
        if name is None:
            return self._send(request, 404, {"error": "unbekannt"})
        try:
            data = json.loads(body) if body else None
        except ValueError:
            return self._send(request, 400, {"error": "kein JSON"})
        if not self._authenticated(request, method, request.path, body, name, data, params):
            return self._send(request, 401, {"error": wire.UNAUTHENTICATED_ERROR})
        if name == "result" and self.result_status is not None:
            return self._send(request, self.result_status, {"error": "test"})
        with self.lock:
            answer = getattr(self, f"_on_{name}")(params, data)
        self._send(request, 200, answer)

    def _authenticated(self, request, method, full_path, body, name, data, params) -> bool:
        device_id = request.headers.get(wire.HEADER_DEVICE, "")
        try:
            timestamp = int(request.headers.get(wire.HEADER_TIMESTAMP, ""))
            raw_signature = request.headers.get(wire.HEADER_SIGNATURE, "")
            signature = base64.urlsafe_b64decode(raw_signature + "=" * (-len(raw_signature) % 4))
        except ValueError:
            return False
        if abs(self._now() - timestamp) > wire.MAX_CLOCK_SKEW_SECONDS:
            return False
        with self.lock:
            device = self.devices.get(device_id)
        try:
            if name == "register":
                key = serialization.load_der_public_key(base64.b64decode(data["public_key"]))
                if device_id_for(key) != device_id or data.get("device_id") != device_id:
                    return False
                if device is not None and (device["public_key"] != data["public_key"] or device["state"] == "gesperrt"):
                    return False  # gesperrt: 401 wie jede andere Route (G3 2.1)
            else:
                if device is None or device["state"] == "gesperrt" or params.get("device_id") != device_id:
                    return False
                key = serialization.load_der_public_key(base64.b64decode(device["public_key"]))
            if not isinstance(key, ec.EllipticCurvePublicKey):
                return False
            key.verify(
                signature, wire.canonical_string(method, full_path, timestamp, body), ec.ECDSA(hashes.SHA256()),
            )
        except (InvalidSignature, ValueError, TypeError, KeyError, AttributeError):
            return False
        return True

    def _on_register(self, params, data):
        self.registrations += 1
        device = self.devices.setdefault(
            data["device_id"], {"state": "nicht_uebernommen", "public_key": data["public_key"]},
        )
        device.update(version=data["version"], capabilities=data["capabilities"], enc_public_key=data["enc_public_key"])
        if device["state"] == "nicht_uebernommen":
            device["claim_code_hash"] = data["claim_code_hash"]
        return {"device_state": device["state"], "poll_after": self.poll_after}

    def _on_commands(self, params, data):
        device_id = params["device_id"]
        now = self._now()
        open_commands = [
            {
                "command_id": cid, "kind": c["kind"], "payload": c["payload"],
                "expires_at": datetime.fromtimestamp(c["expires_at"], UTC).isoformat(),
            }
            for cid, c in self.commands.items()
            if c["device_id"] == device_id and c["result"] is None and c["expires_at"] > now
        ]
        return {
            "device_state": self.devices[device_id]["state"], "poll_after": self.poll_after, "commands": open_commands,
        }

    def _on_result(self, params, data):
        command = self.commands.get(params["command_id"])
        if command is not None and command["result"] is None and command["expires_at"] > self._now():
            command["result"] = data
        return {}

    def _on_status(self, params, data):
        self.statuses.setdefault(params["device_id"], []).append(data)
        return {}

    def _on_notifications(self, params, data):
        for item in data["items"]:
            self.notifications[(params["device_id"], item["key"])] = item
        return {}

    def _on_desired(self, params, data):
        return {"version": self.devices[params["device_id"]].get("version")}

    def _on_update_result(self, params, data):
        return {}

    def _control(self, request, method, path, body):
        data = json.loads(body) if body else {}
        if method == "POST" and path == "/_e2e/claim":
            self.claim(data.get("device_id"))
            return self._send(request, 200, {})
        if method == "POST" and path == "/_e2e/release":
            self.release(data.get("device_id"))
            return self._send(request, 200, {})
        if method == "POST" and path == "/_e2e/commands":
            command_id = self.enqueue(data["kind"], data.get("payload", {}), data.get("device_id"))
            return self._send(request, 200, {"command_id": command_id})
        if method == "GET" and path == "/_e2e/state":
            with self.lock:
                state = {
                    "devices": {k: {"state": v["state"], "version": v.get("version")} for k, v in self.devices.items()},
                    "results": {k: v["result"] for k, v in self.commands.items()},
                    "statuses": {k: v[-1] for k, v in self.statuses.items() if v},
                    "registrations": self.registrations,
                }
            return self._send(request, 200, state)
        return self._send(request, 404, {"error": "unbekannt"})

    @staticmethod
    def _send(request, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        request.send_response(code)
        request.send_header("Content-Type", "application/json")
        request.send_header("Content-Length", str(len(raw)))
        request.end_headers()
        request.wfile.write(raw)


def main() -> None:  # pragma: no cover - laeuft im E2E-Container
    api = FakeDeviceApi("0.0.0.0", int(os.environ.get("FAKE_DEVICE_API_PORT", "8090")))
    api.start()
    with contextlib.suppress(KeyboardInterrupt):
        api._thread.join()


if __name__ == "__main__":
    main()
