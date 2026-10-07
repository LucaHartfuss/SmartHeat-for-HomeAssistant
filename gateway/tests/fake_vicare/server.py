"""Fake-ViCare (Spec SHG G4 3): IAM-Token-Endpunkt und die Teile der ViCare-API, die vicare_cloud braucht, auf stdlib
http.server. Steuerung ueber POST /__control. Nur fuer Tests und die E2E; kein Anspruch auf Vollstaendigkeit."""
import copy
import json
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT = json.loads((Path(__file__).parent / "default_features.json").read_text())
INSTALLATION, GATEWAY, DEVICE = 2012345, "7637415000000001", "0"
GOOD_CODE = "ok-code"


class FakeVicare:
    def __init__(self, recordings: dict[str, list[dict]] | None = None, *, clock=time.time) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._recordings = recordings or {DEVICE: DEFAULT}
        self.reset()
        self._http: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def reset(self) -> None:
        with self._lock:
            self.features = {device: copy.deepcopy(items) for device, items in self._recordings.items()}
            self.calls: list[tuple[str, str]] = []
            self.commands: list[dict] = []
            self.tokens_issued = 0
            self.access: dict[str, float] = {}
            self.refresh: set[str] = set()
            self.pending: list[tuple[float, str, str, tuple[str, dict]]] = []  # (sichtbar_ab, dev, feature, aend.)
            self.switches = {"invalid_grant": False, "offline": False, "rate_limit_after": None, "token_ttl": 3600,
                             "settle": 0.0}

    # --- Token ---

    def token(self, form: dict) -> tuple[int, dict]:
        with self._lock:
            grant = form.get("grant_type")
            if self.switches["invalid_grant"]:
                return 400, {"error": "invalid_grant"}
            if grant == "authorization_code" and form.get("code") == GOOD_CODE:
                return 200, self._issue()
            if grant == "refresh_token" and form.get("refresh_token") in self.refresh:
                self.refresh.discard(form["refresh_token"])  # rotiert
                return 200, self._issue()
            return 400, {"error": "invalid_grant"}

    def _issue(self) -> dict:
        access, refresh = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        self.access[access] = self._clock() + self.switches["token_ttl"]
        self.refresh.add(refresh)
        self.tokens_issued += 1
        return {"access_token": access, "refresh_token": refresh, "expires_in": self.switches["token_ttl"],
                "token_type": "Bearer"}

    # --- API ---

    def api(self, method: str, path: str, token: str | None, body: dict | None) -> tuple[int, dict, dict]:
        with self._lock:
            if token is None or self.access.get(token, 0) <= self._clock():
                return 401, {"error": "EXPIRED TOKEN"}, {}
            self.calls.append((method, path))
            if self.switches["offline"]:
                return 503, {"error": "DEVICE_COMMUNICATION_ERROR"}, {}
            limit = self.switches["rate_limit_after"]
            if limit is not None and len(self.calls) > limit:
                return 429, {"error": "RATE_LIMIT_EXCEEDED"}, {"Retry-After": "60"}
            self._apply_pending()
            if path.startswith("/iot/v2/equipment/installations"):
                devices = [{"id": device, "deviceType": "heating", "modelId": "Fake"} for device in self.features]
                return 200, {"data": [{"id": INSTALLATION, "gateways": [{"serial": GATEWAY, "devices": devices}]}]}, {}
            parts = path.split("/")
            # /iot/v2/features/installations/{i}/gateways/{g}/devices/{d}/features[/{feature}/commands/{command}]
            device = parts[9] if len(parts) > 9 else ""
            if device not in self.features:
                return 404, {"error": "DEVICE_NOT_FOUND"}, {}
            if method == "GET" and len(parts) == 11:
                return 200, {"data": copy.deepcopy(self.features[device])}, {}
            if method == "POST" and len(parts) == 14 and parts[12] == "commands":
                return self._command(device, parts[11], parts[13], body or {})
            return 404, {"error": "NOT_FOUND"}, {}

    def _command(self, device: str, feature: str, command: str, params: dict) -> tuple[int, dict, dict]:
        item = next((f for f in self.features[device] if f["feature"] == feature), None)
        spec = ((item or {}).get("commands") or {}).get(command)
        if spec is None or spec.get("isExecutable") is False:
            return 400, {"error": "COMMAND_NOT_FOUND"}, {}
        for name, rule in (spec.get("params") or {}).items():
            value, limits = params.get(name), rule.get("constraints") or {}
            if rule.get("required") and value is None:
                return 400, {"error": f"{name} fehlt"}, {}
            if limits and value is not None:
                low, step = limits["min"], limits["stepping"]
                off_grid = abs(round((value - low) / step) * step + low - value) > 1e-9
                if not (low <= value <= limits["max"]) or off_grid:
                    return 400, {"error": f"{name} verletzt die Constraints"}, {}
        self.commands.append({"feature": feature, "command": command, "params": params})
        self.pending.append((self._clock() + self.switches["settle"], device, feature, (command, params)))
        self._apply_pending()
        return 200, {"data": {"success": True}}, {}

    def _apply_pending(self) -> None:
        keep = []
        for visible_at, device, feature, (command, params) in self.pending:
            if visible_at > self._clock():
                keep.append((visible_at, device, feature, (command, params)))
                continue
            item = next(f for f in self.features[device] if f["feature"] == feature)
            props = item.setdefault("properties", {})
            if command == "setCurve":
                props["shift"] = {"type": "number", "value": params["shift"]}
                props["slope"] = {"type": "number", "value": params["slope"]}
            elif command == "setTemperature":
                props["temperature"] = {"type": "number", "value": params["targetTemperature"]}
            elif command in ("activate", "deactivate"):
                program = feature.rsplit(".", 1)[-1]
                props["active"] = {"type": "boolean", "value": command == "activate"}
                active = next((f for f in self.features[device] if f["feature"].endswith("operating.programs.active")),
                              None)
                if active is not None:
                    active["properties"]["value"] = {"type": "string",
                                                     "value": program if command == "activate" else "normal"}
        self.pending = keep

    # --- HTTP ---

    def control(self, body: dict) -> None:
        with self._lock:
            if body.get("reset"):
                self.reset()
            self.switches.update({k: v for k, v in body.items() if k in self.switches})

    def start(self, host: str = "127.0.0.1", port: int = 0) -> str:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # still
                pass

            def _send(self, status: int, body: dict, headers: dict | None = None) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

            def _body(self) -> bytes:
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def _token(self) -> str | None:
                header = self.headers.get("Authorization", "")
                return header[7:] if header.startswith("Bearer ") else None

            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                if url.path == "/idp/v3/authorize":  # E2E: der "Browser" wird sofort zurueckgeleitet
                    query = dict(urllib.parse.parse_qsl(url.query))
                    code = urllib.parse.urlencode({"code": GOOD_CODE, "state": query["state"]})
                    self.send_response(302)
                    self.send_header("Location", query["redirect_uri"] + "?" + code)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self._send(*owner.api("GET", url.path, self._token(), None))

            def do_POST(self):
                url = urllib.parse.urlsplit(self.path)
                raw = self._body()
                if url.path == "/idp/v3/token":
                    self._send(*owner.token(dict(urllib.parse.parse_qsl(raw.decode()))))
                elif url.path == "/__control":
                    owner.control(json.loads(raw or b"{}"))
                    self._send(200, {})
                else:
                    self._send(*owner.api("POST", url.path, self._token(), json.loads(raw or b"{}")))

        self._http = ThreadingHTTPServer((host, port), Handler)
        self._thread = threading.Thread(target=self._http.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        return f"http://{host}:{self._http.server_address[1]}"

    def stop(self) -> None:
        if self._http is not None:
            self._http.shutdown()
            self._http.server_close()
            if self._thread is not None:
                self._thread.join(timeout=5)
            self._http = self._thread = None
