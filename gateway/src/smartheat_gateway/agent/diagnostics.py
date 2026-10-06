"""Diagnoseseite (Spec SHG G2 6.5): nur lesend, ohne Login, ohne Geheimnisse, statisches HTML ohne JavaScript,
Selbstaktualisierung alle 10 s. QR-Code (segno, SVG) und Uebernahme-Code nur lokal und nur, wenn der Server zuletzt
`nicht_uebernommen` gemeldet hat (unbekannt, etwa nach einem Neustart ohne Server, zeigt keinen Code). /healthz fuer den
Updater (G2b).

Schutz gegen DNS-Rebinding: jede Route antwortet nur, wenn der Host-Header eine IP-Adresse, `localhost` oder ein Name
aus SHG_DIAG_HOSTNAMES (kommagetrennt) ist; sonst 403 ohne Inhalt. Sonst koennte eine fremde Webseite ueber einen auf
die LAN-Adresse umgebogenen Namen den Uebernahme-Code im Browser eines Besuchers lesen. Der Befehl `diagnostics` liefert command_snapshot: derselbe Auszug OHNE
Uebernahme-Code (gehoert nicht in den Befehlsverlauf des Servers). Aus der Host-Statusdatei (G2b) kommen nur die
bekannten Felder HOST_FIELDS, nie die ganze Datei."""
import html
import ipaddress
import json
import logging
import threading
from collections.abc import Callable, Iterable
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
    state = ctx.device_state
    return {
        "geraet": ctx.identity.device_id, "uebernommen": None if state is None else state == "uebernommen",
        "versionen": {"gateway": GATEWAY_VERSION}, "server_ok": loop.server_ok,
        "laufzeit": (ctx.runtime_status or {}).get("status"), "zigbee_stick": ctx.mirror.online is True,
        "host": {key: host[key] for key in HOST_FIELDS if key in host} if isinstance(host, dict) else {},
        "code": ctx.identity.claim_code if state == "nicht_uebernommen" else None,
    }


def command_snapshot(ctx: AgentContext, loop, host_status_path: Path) -> dict:
    """Ergebnis des Befehls diagnostics: wie snapshot, aber nie mit Uebernahme-Code."""
    return {key: value for key, value in snapshot(ctx, loop, host_status_path).items() if key != "code"}


def parse_hostnames(text: str) -> frozenset[str]:
    """SHG_DIAG_HOSTNAMES: kommagetrennte Namen, unter denen die Seite erreichbar sein darf."""
    return frozenset(name for name in (part.strip().rstrip(".").lower() for part in text.split(",")) if name)


def _host_name(header: str) -> str | None:
    if header.startswith("["):
        name, closed, rest = header[1:].partition("]")
        port_ok = rest == "" or (rest[:1] == ":" and rest[1:].isdigit())
        return name if closed and port_ok else None
    name, colon, port = header.partition(":")
    return None if colon and not port.isdigit() else name


def host_allowed(header: str | None, hostnames: frozenset[str]) -> bool:
    """IP-Literal (v4, v6 in Klammern), localhost oder ein erlaubter Name; alles andere ist moegliches Rebinding."""
    name = _host_name(header.strip()) if header else None
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
    except ValueError:
        name = name.rstrip(".").lower()
        return name == "localhost" or name in hostnames
    return True


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
    if data["uebernommen"] is False and data.get("code"):
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
    port: int, snapshot_fn: Callable[[], dict], qr_url_fn: Callable[[], str | None], hostnames: Iterable[str] = (),
) -> ThreadingHTTPServer:
    allowed = frozenset(hostnames)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _answer(self, code: int, raw: bytes = b"", kind: str | None = None) -> None:
            self.send_response(code)
            if kind:
                self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if not host_allowed(self.headers.get("Host"), allowed):
                self._answer(403)
                return
            if self.path not in ("/", "/healthz"):
                self._answer(404)
                return
            data = snapshot_fn()
            if self.path == "/healthz":
                body = json.dumps({"server_ok": data["server_ok"], "runtime_status": data["laufzeit"]})
                self._answer(200, body.encode(), "application/json")
                return
            claimable = data["uebernommen"] is False and bool(data.get("code"))
            body = render_html(data, qr_url_fn() if claimable else None)
            self._answer(200, body.encode(), "text/html; charset=utf-8")

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, name="diagnose", daemon=True).start()
    return server
