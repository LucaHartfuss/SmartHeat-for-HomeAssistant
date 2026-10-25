"""Diagnoseseite: nur lesend, ohne Login, ohne Geheimnisse, statisches HTML ohne
JavaScript, Selbstaktualisierung alle 10 s. Zeigt den Zustand des Geraetekerns (wie die LED), Hinweise des Dienstes
(Befehl hinweis), eigene Meldungen (Wunsch begrenzt, Soll-Quelle aus), eine abgelehnte Konfiguration und, solange das
Gateway nicht uebernommen ist (Anzeige.code_zeigen), QR-Code (segno, SVG) und Uebernahme-Code - nur lokal.
Den Uebernahmestand meldet der Server (claim_state, new_claim_code); bis dahin gilt der Stand des Bootstraps. /healthz
fuer den Updater.

Schutz gegen DNS-Rebinding: jede Route antwortet nur, wenn der Host-Header eine IP-Adresse, `localhost` oder ein Name
aus SHG_DIAG_HOSTNAMES (kommagetrennt) ist; sonst 403 ohne Inhalt. Sonst koennte eine fremde Webseite ueber einen auf
die LAN-Adresse umgebogenen Namen den Uebernahme-Code im Browser eines Besuchers lesen. Der Befehl `diagnostics`
liefert geraet_host.snapshot() OHNE Uebernahme-Code."""
import contextlib
import html
import ipaddress
import json
import logging
import socket
import threading
from collections.abc import Callable, Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

import segno

logger = logging.getLogger(__name__)

REFRESH_SECONDS = 10
VERBINDUNG_FRIST_SECONDS = 10.0  # Lebensdauer einer Verbindung, auch wenn der Client tropft (Angreifer im LAN)
MAX_VERBINDUNGEN = 64  # gleichzeitig bediente Verbindungen; weitere werden sofort geschlossen
ZUSTAND_TEXTE = {
    "startet": "Das Gateway startet.",
    "nicht_uebernommen": "Das Gateway ist noch keinem Konto zugeordnet. Bitte im Portal übernehmen.",
    "keine_verbindung": "Keine Verbindung zum SmartHeat-Server. Netz und Internetzugang prüfen.",
    "wartet_auf_einrichtung": "Das Gateway ist übernommen und wartet auf die Einrichtung im Portal.",
    "regelt": "Das Gateway regelt die Heizung.",
    "stoerung": "Die Regelung meldet eine Störung; Details im Portal.",
    "gesperrt": "Dieses Gateway ist gesperrt oder wurde ersetzt und verbindet sich nicht mehr. "
                "Bitte den Support kontaktieren.",
    "update_noetig": "Die Software ist zu alt für die Einrichtung; das Gateway aktualisiert sich. "
                     "Bleibt der Zustand, bitte den Support kontaktieren.",
}


def seite(geraet, device) -> dict:
    """Auszug fuer die lokale Seite: geraet_host.snapshot() plus Uebernahme-Code (nur hier, nur im LAN)."""
    data = geraet.snapshot()
    anzeige = geraet.letzte_anzeige
    zeigen = anzeige is not None and anzeige.code_zeigen  # Wie die HA-Discovery
    data["code"] = device.identity.claim_code if device is not None and zeigen else None
    return data


def qr_url(portal_base: str, device) -> str | None:
    portal = portal_base.rstrip("/")
    if not portal or device is None:
        return None
    return f"{portal}/#/claim?d={quote(device.identity.device_id)}&c={quote(device.identity.claim_code)}"


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


def client_allowed(address: str) -> bool:
    """Nur Loopback, private Netze (inkl. IPv6-ULA) und Link-Local: der veroeffentlichte Docker-Port
    umgeht die input-Kette der Firewall, die Seite zeigt vor der Uebernahme den Uebernahme-Code."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_link_local or (ip.is_private and not ip.is_global)


def _row(label: str, value) -> str:
    text = "unbekannt" if value is None else ("ja" if value is True else "nein" if value is False else str(value))
    return f"<tr><th>{html.escape(label)}</th><td>{html.escape(text)}</td></tr>"


def _liste(titel: str, eintraege: list[str]) -> str:
    if not eintraege:
        return ""
    items = "".join(f"<li>{html.escape(text)}</li>" for text in eintraege)
    return f"<h2>{html.escape(titel)}</h2><ul>{items}</ul>"


def render_html(data: dict, qr_url: str | None) -> str:
    host = data.get("host", {})
    zustand = data.get("zustand", "startet")
    rows = "".join([
        _row("Gerät", data["geraet"]), _row("Zustand", ZUSTAND_TEXTE.get(zustand, zustand)),
        _row("Eingerichtet", data["eingerichtet"]), _row("Version", data["versionen"].get("gateway")),
        _row("Server erreichbar", data["server_ok"]), _row("Netz", host.get("netz")), _row("DNS", host.get("dns")),
        _row("Uhrzeit synchron", host.get("zeit_synchron")), _row("Zigbee-Stick erkannt", data["zigbee_stick"]),
        _row("Regelung", data["laufzeit"]),
    ])
    abgelehnt = data.get("konfiguration_abgelehnt")
    hinweise = _liste("Hinweise", [eintrag.get("text", "") for eintrag in data.get("hinweise", [])])
    meldungen = _liste("Meldungen des Geräts", [eintrag.get("text", "") for eintrag in data.get("meldungen", [])])
    ablehnung = (f"<h2>Einrichtung abgelehnt</h2><p>{html.escape(abgelehnt)}</p>" if abgelehnt else "")
    claim = ""
    if data.get("code"):
        code = html.escape(data["code"])
        qr = segno.make(qr_url, error="m").svg_inline(scale=4) if qr_url else ""
        claim = f"<h2>Gerät übernehmen</h2>{qr}<p>Übernahme-Code: <strong>{code}</strong></p>"
    return (
        '<!doctype html><html lang="de"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}"><title>SmartHeat-Gateway</title>'
        "<style>body{font-family:sans-serif;margin:16px;max-width:640px}th{text-align:left;padding-right:12px}</style>"
        f"</head><body><h1>SmartHeat-Gateway</h1><table>{rows}</table>{ablehnung}{hinweise}{meldungen}{claim}"
        "</body></html>"
    )


class _BegrenzterServer(ThreadingHTTPServer):
    """Ein Faden je Verbindung, aber hoechstens `maximum` gleichzeitig; jede weitere Verbindung wird sofort
    geschlossen, statt Speicher und Faeden des Prozesses zu belegen, der auch den Geraetekern traegt."""

    def __init__(self, adresse, handler, maximum: int) -> None:
        self._plaetze = threading.BoundedSemaphore(maximum)
        super().__init__(adresse, handler)

    def process_request(self, request, client_address):
        if not self._plaetze.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._plaetze.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._plaetze.release()


def start_server(
    port: int, snapshot_fn: Callable[[], dict], qr_url_fn: Callable[[], str | None], hostnames: Iterable[str] = (),
    *, nur_health: bool = False,
) -> ThreadingHTTPServer:
    """`nur_health`: der Gesundheitsserver fuer Compose-Healthcheck und Updater antwortet nur auf /healthz (kein HTML,
    kein Uebernahme-Code). Er hat eigene Plaetze und Fristen; die Compose-Datei veroeffentlicht ihn nur auf dem
    Loopback des Hosts: die Plaetze der Diagnoseseite zu fuellen nimmt ihm keine (S-10). Ob ein Gast im selben Netz ihn
    mit Docker vor Version 28 ueber direktes Routing trotzdem erreicht, ist offen (gateway-host.md, Bekannte Grenzen)."""
    allowed = frozenset(hostnames)
    frist = VERBINDUNG_FRIST_SECONDS

    class Handler(BaseHTTPRequestHandler):
        timeout = frist  # je Leseoperation; die Uhr in setup/finish begrenzt die ganze Verbindung

        def setup(self):
            super().setup()
            self._uhr = threading.Timer(frist, self._abbrechen)
            self._uhr.daemon = True
            self._uhr.start()

        def finish(self):
            self._uhr.cancel()
            super().finish()

        def _abbrechen(self):
            with contextlib.suppress(OSError):  # schon geschlossen
                self.connection.shutdown(socket.SHUT_RDWR)

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
            if not client_allowed(self.client_address[0]):
                self._answer(403)
                return
            if not host_allowed(self.headers.get("Host"), allowed):
                self._answer(403)
                return
            if self.path not in (("/healthz",) if nur_health else ("/", "/healthz")):
                self._answer(404)
                return
            data = snapshot_fn()
            if self.path == "/healthz":
                body = json.dumps({"server_ok": data["server_ok"], "runtime_status": data["laufzeit"],
                                   "zustand": data["zustand"]})
                self._answer(200, body.encode(), "application/json")
                return
            body = render_html(data, qr_url_fn() if data.get("code") else None)
            self._answer(200, body.encode(), "text/html; charset=utf-8")

    server = _BegrenzterServer(("0.0.0.0", port), Handler, MAX_VERBINDUNGEN)
    threading.Thread(target=server.serve_forever, name="diagnose", daemon=True).start()
    return server
