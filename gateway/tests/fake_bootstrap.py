"""Fake-Bootstrap fuer die Docker-Pruefungen des Gateways (Plan 5b D2 Task 10): register und certificate (Routen aus
wire.ROUTES) signieren den CSR mit der Test-CA aus ca_dir (ca.pem, ca.key; fake_ca) und nennen den Fake-Broker als
Endpunkt; die Antwort traegt genau die Felder aus wire.BOOTSTRAP_RESPONSE. Der Fake dient ausserdem als Release-Host
des Updater-Tests (/_e2e/files). Signaturen prueft er nicht (D1-Unit-Tests, echter Server in W Teil B). Steuer-Routen
/_e2e/* nur fuer Tests. Start im Container (Gateway-Image, tests als /gw-tests): python /gw-tests/fake_bootstrap.py
(Umgebung SHG_FAKE_CA_DIR, SHG_FAKE_MQTT_HOST, SHG_FAKE_PORT). Logs nennen nie Schluessel oder Zertifikate (Regel 6)."""
import base64
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography import x509
from cryptography.x509.oid import NameOID
from fake_ca import GeraeteCa

from smartheat_device import wire

_FILES = "/_e2e/files/"
_REGISTER = wire.ROUTES["register"][1]
_CERTIFICATE = re.compile("^" + re.escape(wire.ROUTES["certificate"][1]).replace(re.escape("{device_id}"),
                                                                                  "(?P<device_id>[^/]+)") + "$")


class FakeBootstrap:
    def __init__(self, ca_dir: Path, mqtt_host: str, *, host: str = "127.0.0.1", port: int = 0,
                 mqtt_port: int = 8883) -> None:
        self._ca = GeraeteCa.laden(ca_dir)
        self._mqtt_host, self._mqtt_port = mqtt_host, mqtt_port
        self.device_state = "uebernommen"
        self.device_ids: list[str] = []
        self.registrations = self.certificates = 0
        self.files: dict[str, bytes] = {}
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keine Zugriffslogs
                pass

            def do_GET(self):
                fake._get(self)

            def do_POST(self):
                fake._post(self)

        self._server = ThreadingHTTPServer((host, port), Handler)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> None:
        threading.Thread(target=self._server.serve_forever, name="fake-bootstrap", daemon=True).start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    @staticmethod
    def _cn(csr_pem) -> str | None:
        """CN (Geraete-ID) des CSR; None, wenn csr_pem kein CSR mit CN ist."""
        if not isinstance(csr_pem, str):
            return None
        try:
            names = x509.load_pem_x509_csr(csr_pem.encode()).subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        except ValueError:
            return None
        return str(names[0].value) if names else None

    def _answer(self, csr_pem: str, device_id: str) -> dict:
        pem = self._ca.sign_csr(csr_pem)  # behaelt das Subjekt des CSR
        with self._lock:
            if device_id not in self.device_ids:
                self.device_ids.append(device_id)
            state = self.device_state
        return {"device_state": state, "certificate": pem, "mqtt_endpoint": self._mqtt_host,
                "mqtt_port": self._mqtt_port, "mqtt_ca": self._ca.pem}

    @staticmethod
    def _body(request) -> dict:
        length = int(request.headers.get("Content-Length") or 0)
        try:
            data = json.loads(request.rfile.read(length) or b"{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _post(self, request) -> None:
        path, data = request.path, self._body(request)
        if path == "/_e2e/device_state":
            if data.get("device_state") not in wire.DEVICE_STATES:
                return self._send(request, 400, {"error": "device_state unbekannt"})
            with self._lock:
                self.device_state = data["device_state"]
            return self._send(request, 200, {"ok": True})
        if path == "/_e2e/files":
            with self._lock:
                self.files.update({name: base64.b64decode(raw) for name, raw in data.items()})
            return self._send(request, 200, {"ok": True})
        is_register = path == _REGISTER
        certificate = _CERTIFICATE.match(path)
        if not (is_register or certificate):
            return self._send(request, 404, {"error": "unbekannt"})
        device_id = self._cn(data.get("csr"))
        if device_id is None:
            return self._send(request, 400, {"error": "csr fehlt oder ungueltig"})
        if certificate and certificate["device_id"] != device_id:  # wie der Server: nur fuer das eigene Geraet
            return self._send(request, 403, {"error": "csr passt nicht zum Geraet"})
        answer = self._answer(data["csr"], device_id)
        with self._lock:
            if is_register:
                self.registrations += 1
            else:
                self.certificates += 1
        return self._send(request, 200, answer)

    def _get(self, request) -> None:
        if request.path == "/_e2e/state":
            with self._lock:
                state = {"registrations": self.registrations, "certificates": self.certificates,
                         "device_ids": list(self.device_ids), "device_state": self.device_state}
            return self._send(request, 200, state)
        name = request.path[len(_FILES):] if request.path.startswith(_FILES) else None
        with self._lock:
            raw = self.files.get(name) if name is not None else None
        if raw is None:
            return self._send(request, 404, {"error": "unbekannt"})
        request.send_response(200)
        request.send_header("Content-Length", str(len(raw)))
        request.end_headers()
        request.wfile.write(raw)
        return None

    @staticmethod
    def _send(request, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        request.send_response(code)
        request.send_header("Content-Type", "application/json")
        request.send_header("Content-Length", str(len(raw)))
        request.end_headers()
        request.wfile.write(raw)


def main() -> None:  # pragma: no cover - Container-Einstieg
    server = FakeBootstrap(Path(os.environ["SHG_FAKE_CA_DIR"]), os.environ["SHG_FAKE_MQTT_HOST"], host="0.0.0.0",
                           port=int(os.environ.get("SHG_FAKE_PORT", "8090")))
    server.start()
    print(f"Fake-Bootstrap auf {server.url}, Broker {os.environ['SHG_FAKE_MQTT_HOST']}", flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()
