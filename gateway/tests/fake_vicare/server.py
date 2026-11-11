"""Fake-ViCare: IAM-Token-Endpunkt und die Teile der ViCare-API, die vicare_cloud braucht, auf stdlib
http.server. Steuerung ueber POST /__control. Nur fuer Tests und die E2E; kein Anspruch auf Vollstaendigkeit.

Ohne `recordings` fuehrt der Fake das Haus H1 des Hausmodell-Kerns mit dem Vaillant-Potenzgesetz: Kurve und Soll-
Temperatur der Features sind die Eingaenge, Vorlauf, Raumfuehler, Brennerstunden und -starts werden bei jeder Abfrage
aus dem Kern fortgeschrieben (5-min-Schritte der Uhr `clock`, ein Rest bleibt fuer die naechste Abfrage liegen). Die
Aussentemperatur bleibt konstant (Feature `heating.sensors.temperature.outside`), die Tageszeit bestimmt nichts. Mit
`recordings` bleiben die Features statisch."""
import copy
import json
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from smartheat_core import hausmodell
from smartheat_core.zahlen import ist_endliche_zahl

DEFAULT = json.loads((Path(__file__).parent / "default_features.json").read_text())
INSTALLATION, GATEWAY, DEVICE = 2012345, "7637415000000001", "0"
GOOD_CODE = "ok-code"

CURVE = "heating.circuits.0.heating.curve"
NORMAL = "heating.circuits.0.operating.programs.normal"
SUPPLY = "heating.circuits.0.sensors.temperature.supply"
ROOM = "heating.circuits.0.sensors.temperature.room"
OUTSIDE = "heating.sensors.temperature.outside"
BURNER = "heating.burners.0"
STATISTICS = "heating.burners.0.statistics"
ERZEUGER = hausmodell.Potenzgesetz(a=2.54, b=0.78)  # Vaillant, wie der Simulationstreiber
HEAT_LIMIT = 16.0
KERN_STUNDE = 12.0  # ausserhalb der Warmwasserpausen: die Tageszeit darf die Lieferung nicht bestimmen
START_ROOM = 20.0
START_BURNER_HOURS = 1000.0
UNDELIVERED_SUPPLY = 29.5  # Vorlauf ohne Waermelieferung


def _zahl(feature: dict | None, name: str, default: float) -> float:
    raw = (((feature or {}).get("properties") or {}).get(name) or {}).get("value")
    return float(raw) if ist_endliche_zahl(raw) else default


class FakeVicare:
    def __init__(self, recordings: dict[str, list[dict]] | None = None, *, clock=time.time) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._model = recordings is None
        self._recordings = {DEVICE: DEFAULT} if recordings is None else recordings
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
            if self._model:
                self._start_model()

    # --- Hausmodell ---

    def _start_model(self) -> None:
        self._zustand = hausmodell.Anlagenzustand(room=START_ROOM, burner_hours=START_BURNER_HOURS)
        self._letzter: hausmodell.Anlagenschritt | None = None
        self._last_model = self._clock()
        self._rest = 0.0
        items = self.features[DEVICE]
        for name, properties in ((ROOM, {"value": {"type": "number", "value": START_ROOM}}),
                                 (STATISTICS, {"hours": {"type": "number", "value": START_BURNER_HOURS},
                                               "starts": {"type": "number", "value": self._zustand.burner_starts}})):
            if all(item["feature"] != name for item in items):
                items.append({"feature": name, "isEnabled": True, "properties": properties, "commands": {}})

    def _inputs(self) -> tuple[tuple[float, float, float], float, float]:
        """Eingaenge des Kerns aus den Features: (Steigung, Verschiebung, Heizgrenze), Mindestvorlauf, Aussen. Die
        Verschiebung der Heizkurve ist das Niveau, das Soll das Raum-Soll; fehlt ein Feature, gilt der Startwert."""
        by_name = {item["feature"]: item for item in self.features[DEVICE]}
        soll = _zahl(by_name.get(NORMAL), "temperature", START_ROOM)
        shift = _zahl(by_name.get(CURVE), "shift", 0.0)
        slope = _zahl(by_name.get(CURVE), "slope", 1.0)
        return (slope, soll + shift, HEAT_LIMIT), soll, _zahl(by_name.get(OUTSIDE), "value", 5.0)

    def _advance(self) -> None:
        now = self._clock()
        self._rest += max(now - self._last_model, 0.0)
        self._last_model = now
        steps = int(self._rest // hausmodell.DT)
        self._rest -= steps * hausmodell.DT
        hebel, minimum, outdoor = self._inputs()
        for _ in range(min(steps, hausmodell.STEPS_PER_DAY)):
            self._letzter = hausmodell.anlage_schritt(
                self._zustand, hausmodell.HOUSES["H1"], ERZEUGER, hebel, outdoor, 0.0, KERN_STUNDE, minimum,
                hausmodell.Betriebsflags(), None)

    def _publish(self) -> None:
        """Schreibt den Stand des Kerns in die Features, die der Fake fuehrt (vorhandene; fehlt eines, bleibt es weg)."""
        hebel, minimum, outdoor = self._inputs()
        flow = max(ERZEUGER.flow(hebel[0], hebel[1], outdoor), minimum)
        delivers = self._letzter is None or self._letzter.geliefert
        values = {
            (SUPPLY, "value"): round(flow, 1) if delivers else UNDELIVERED_SUPPLY,
            (ROOM, "value"): round(self._zustand.room, 2),
            (STATISTICS, "hours"): round(self._zustand.burner_hours, 3),
            (STATISTICS, "starts"): self._zustand.burner_starts,
        }
        for item in self.features[DEVICE]:
            for (feature, name), value in values.items():
                if item["feature"] == feature:
                    item.setdefault("properties", {})[name] = {"type": "number", "value": value}
            if item["feature"] == BURNER:
                item.setdefault("properties", {})["active"] = {
                    "type": "boolean", "value": bool(self._letzter and self._letzter.brenner)}

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
            if self._model:
                self._advance()  # mit den Eingaengen bis jetzt; ein Befehl wirkt erst ab seinem Zeitpunkt
            self._apply_pending()
            if self._model:
                self._publish()
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
