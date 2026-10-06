"""Diagnoseseite (Spec SHG G2 6.5): nur lesend, ohne Login, ohne Geheimnisse, statisches HTML ohne JavaScript,
Selbstaktualisierung alle 10 s. QR-Code (segno, SVG) und Uebernahme-Code nur lokal und nur solange nicht uebernommen.
/healthz fuer den Updater (G2b). Der Befehl `diagnostics` liefert command_snapshot: derselbe Auszug OHNE
Uebernahme-Code (gehoert nicht in den Befehlsverlauf des Servers). Aus der Host-Statusdatei (G2b) kommen nur die
bekannten Felder HOST_FIELDS, nie die ganze Datei."""
import html
import json
import logging
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import segno

from smartheat_gateway.agent.context import AgentContext
from smartheat_gateway.files import read_json
from smartheat_gateway.version import GATEWAY_VERSION

logger = logging.getLogger(__name__)

HOST_FIELDS = ("netz", "dns", "zeit_synchron")
REFRESH_SECONDS = 10


def snapshot(ctx: AgentContext, loop, host_status_path: Path) -> dict:
    """Auszug fuer die lokale Seite, mit Uebernahme-Code (nur hier, nur im LAN)."""
    host = read_json(host_status_path)
    return {
        "geraet": ctx.identity.device_id, "uebernommen": ctx.device_state == "uebernommen",
        "versionen": {"gateway": GATEWAY_VERSION}, "server_ok": loop.server_ok,
        "laufzeit": (ctx.runtime_status or {}).get("status"), "zigbee_stick": ctx.mirror.online is True,
        "host": {key: host[key] for key in HOST_FIELDS if key in host} if isinstance(host, dict) else {},
        "code": ctx.identity.claim_code,
    }


def command_snapshot(ctx: AgentContext, loop, host_status_path: Path) -> dict:
    """Ergebnis des Befehls diagnostics: wie snapshot, aber nie mit Uebernahme-Code."""
    return {key: value for key, value in snapshot(ctx, loop, host_status_path).items() if key != "code"}


def _row(label: str, value) -> str:
    text = "unbekannt" if value is None else ("ja" if value is True else "nein" if value is False else str(value))
    return f"<tr><th>{html.escape(label)}</th><td>{html.escape(text)}</td></tr>"


def render_html(data: dict, qr_url: str | None) -> str:
    host = data.get("host", {})
    rows = "".join([
        _row("Gerät", data["geraet"]), _row("Übernommen", data["uebernommen"]),
        _row("Version", data["versionen"].get("gateway")), _row("Server erreichbar", data["server_ok"]),
        _row("Netz", host.get("netz")), _row("DNS", host.get("dns")),
        _row("Uhrzeit synchron", host.get("zeit_synchron")),
        _row("Zigbee-Stick erkannt", data["zigbee_stick"]), _row("Regelung", data["laufzeit"]),
    ])
    claim = ""
    if not data["uebernommen"]:
        code = html.escape(data["code"])
        qr = segno.make(qr_url, error="m").svg_inline(scale=4) if qr_url else ""
        claim = f"<h2>Gerät übernehmen</h2>{qr}<p>Übernahme-Code: <strong>{code}</strong></p>"
    return (
        '<!doctype html><html lang="de"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}"><title>SmartHeat-Gateway</title>'
        "<style>body{font-family:sans-serif;margin:16px;max-width:640px}th{text-align:left;padding-right:12px}</style>"
        f"</head><body><h1>SmartHeat-Gateway</h1><table>{rows}</table>{claim}</body></html>"
    )


def start_server(
    port: int, snapshot_fn: Callable[[], dict], qr_url_fn: Callable[[], str | None],
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            data = snapshot_fn()
            if self.path == "/healthz":
                body = json.dumps({"server_ok": data["server_ok"], "runtime_status": data["laufzeit"]})
                kind = "application/json"
            elif self.path == "/":
                body = render_html(data, qr_url_fn() if not data["uebernommen"] else None)
                kind = "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            raw = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, name="diagnose", daemon=True).start()
    return server
