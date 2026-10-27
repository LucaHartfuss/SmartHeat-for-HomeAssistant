"""Diagnoseseite hinter dem Etikett-Passwort (SG-05, S-13, GW 8): nur lesend, statisches HTML ohne
JavaScript, Selbstaktualisierung alle 10 s. Alles ausser /anmelden verlangt eine Sitzung (anmeldung.py), auch /healthz
auf diesem Port; der Gesundheitsserver (nur_health, nur Loopback des Hosts) bleibt ohne Sitzung. Kein Server-Banner,
keine Version. Zeigt den Zustand des Geraetekerns (wie die LED), die Seriennummer, Hinweise des Dienstes
(Befehl hinweis), eigene Meldungen (Wunsch begrenzt, Soll-Quelle aus), eine abgelehnte Konfiguration und, solange das
Gateway nicht uebernommen ist (Anzeige.code_zeigen) und nach einem Knopfdruck das Code-Fenster (15 min) offen ist,
Geraete-ID und Uebernahme-Code (kein QR-Code, kein Portal-Link mit Code, GW 7.1) - nur lokal; sonst nach einem Druck den Hinweis "keine Verbindung"
bzw. "uebernommen".
Den Uebernahmestand meldet der Server (claim_state, new_claim_code); bis dahin gilt der Stand des Bootstraps. /healthz
fuer den Updater.

Schutz gegen DNS-Rebinding: jede Route antwortet nur, wenn der Host-Header eine IP-Adresse, `localhost`, der Hostname
aus werk.json oder ein Name aus SHG_DIAG_HOSTNAMES (kommagetrennt) ist; sonst 403 ohne Inhalt, auch mit gueltiger Sitzung. Sonst koennte eine fremde Webseite ueber einen auf
die LAN-Adresse umgebogenen Namen den Uebernahme-Code im Browser eines Besuchers lesen. Der Befehl `diagnostics`
liefert geraet_host.snapshot() OHNE Uebernahme-Code."""
import contextlib
import html
import ipaddress
import json
import logging
import socket
import sys
import threading
import urllib.parse
from collections.abc import Callable, Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from smartheat_gateway.anmeldung import Zugang

logger = logging.getLogger(__name__)

REFRESH_SECONDS = 10
VERBINDUNG_FRIST_SECONDS = 10.0  # Lebensdauer einer Verbindung, auch wenn der Client tropft (Angreifer im LAN)
SITZUNG_COOKIE = "shg_sitzung"
SITZUNG_ATTRIBUTE = "HttpOnly; SameSite=Strict; Path=/; Max-Age=3600"
ANMELDUNG_MAX_BYTES = 256  # Koerper des Anmeldeformulars; groesser wird nicht gelesen
LAENGE_STELLEN_MAX = len(str(ANMELDUNG_MAX_BYTES))  # mehr Ziffern ergeben ohnehin mehr als 256 (und kein int() ueber 4300 Stellen)
CROSS_SITE = ("cross-site", "same-site")  # Sec-Fetch-Site: eine fremde Seite loest die Anfrage aus
FORMULAR_TYP = "application/x-www-form-urlencoded"
MAX_VERBINDUNGEN = 64  # gleichzeitig bediente Verbindungen; weitere werden sofort geschlossen
ANFRAGE_MAX_BYTES = 16384  # alles, was eine Verbindung lesen darf (Anfragezeile, Kopf, Koerper); mehr schliesst sie
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


CODE_HINWEIS_TEXTE = {"keine_verbindung": "Keine Verbindung zum Server – kein Code", "uebernommen": "übernommen"}


def seite(geraet, device) -> dict:
    """Auszug fuer die lokale Seite: geraet_host.snapshot() plus Geraete-ID und Uebernahme-Code (nur hier, nur im LAN,
    nur solange das Geraet nicht uebernommen ist und das Code-Fenster nach einem Knopfdruck offen ist, S-2) und der
    Hinweis des letzten Drucks ohne Code (code_hinweis: keine_verbindung, uebernommen)."""
    data = geraet.snapshot()
    sichtbar = geraet.code_sichtbar()
    data["code"] = device.identity.claim_code if device is not None and sichtbar else None
    data["geraet"] = device.identity.device_id if device is not None and sichtbar else None
    data["code_hinweis"] = geraet.code_hinweis()
    return data


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


def _host_vergleich(header: str | None) -> str | None:
    """Der Host ohne Port, klein geschrieben und ohne Punkt am Ende, zum Vergleich von Origin und Host-Header."""
    name = _host_name(header.strip()) if header else None
    return name.rstrip(".").lower() if name else None


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


def render_html(data: dict) -> str:
    host = data.get("host", {})
    zustand = data.get("zustand", "startet")
    rows = "".join([
        _row("Zustand", ZUSTAND_TEXTE.get(zustand, zustand)),
        _row("Eingerichtet", data["eingerichtet"]), _row("Seriennummer", data.get("seriennummer")),
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
        claim = (f"<h2>Gerät übernehmen</h2><p>Gerät: <strong>{html.escape(str(data.get('geraet')))}</strong></p>"
                 f"<p>Übernahme-Code: <strong>{code}</strong></p>")
    elif data.get("code_hinweis") in CODE_HINWEIS_TEXTE:
        claim = f"<h2>Gerät übernehmen</h2><p>{html.escape(CODE_HINWEIS_TEXTE[data['code_hinweis']])}</p>"
    return (
        '<!doctype html><html lang="de"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}"><title>SmartHeat-Gateway</title>'
        "<style>body{font-family:sans-serif;margin:16px;max-width:640px}th{text-align:left;padding-right:12px}</style>"
        f"</head><body><h1>SmartHeat-Gateway</h1><table>{rows}</table>{ablehnung}{hinweise}{meldungen}{claim}"
        "</body></html>"
    )


ANMELDUNG_MELDUNGEN = {
    "falsch": "Das Passwort ist falsch.",
    "gesperrt": "Zu viele Fehlversuche. Bitte später erneut versuchen.",
    "belegt": "Es läuft gerade eine andere Anmeldung. Bitte gleich erneut versuchen.",
    "kein_werk": "Dieses Gerät ist nicht bespielt; eine Anmeldung ist nicht möglich.",
}


def render_anmeldung(ergebnis: str | None = None) -> str:
    """Das Anmeldeformular: nur das Passwort, keine Daten des Geraets, keine Version."""
    meldung = f"<p>{html.escape(ANMELDUNG_MELDUNGEN[ergebnis])}</p>" if ergebnis in ANMELDUNG_MELDUNGEN else ""
    return (
        '<!doctype html><html lang="de"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1"><title>SmartHeat-Gateway</title>'
        "<style>body{font-family:sans-serif;margin:16px;max-width:640px}</style></head><body>"
        f"<h1>SmartHeat-Gateway</h1>{meldung}"
        '<form method="post" action="/anmelden"><p><label>Passwort (siehe Etikett am Gerät) '
        '<input type="password" name="passwort" maxlength="128" autocomplete="current-password" autofocus></label></p>'
        '<p><button type="submit">Anmelden</button></p></form></body></html>'
    )


class _BegrenzterLeser:
    """Der Lesestrom einer Verbindung mit festem Budget (ANFRAGE_MAX_BYTES). `http.server` sammelt Anfragezeile und Kopf
    sonst bis zu 100 Zeilen zu je 64 KiB, bevor es Host, Sitzung oder Anmeldung prueft: 64 Verbindungen brachten den
    Prozess, der auch die Regelung traegt, ueber 3 GB (A-01, S-13). Ist das Budget aufgebraucht oder endet eine Zeile
    darin ohne Zeilenende, wirft der Leser ConnectionAbortedError (ein OSError: handle_error des Servers schliesst die
    Verbindung ohne Traceback)."""

    def __init__(self, strom, budget: int) -> None:
        self._strom = strom
        self._rest = budget

    def readline(self, groesse: int = -1) -> bytes:
        zeile = self._strom.readline(self._erlaubt(groesse))
        self._rest -= len(zeile)
        if self._rest <= 0 and not zeile.endswith(b"\n"):
            raise ConnectionAbortedError("Anfrage ueber dem Lesebudget")  # eine Zeile, die das Budget sprengt
        return zeile

    def read(self, groesse: int = -1) -> bytes:
        daten = self._strom.read(self._erlaubt(groesse))
        self._rest -= len(daten)
        return daten

    def _erlaubt(self, groesse: int) -> int:
        if self._rest <= 0:
            raise ConnectionAbortedError("Anfrage ueber dem Lesebudget")
        return self._rest if groesse < 0 else min(groesse, self._rest)

    @property
    def closed(self) -> bool:
        return self._strom.closed

    def close(self) -> None:
        self._strom.close()


class _BegrenzterServer(ThreadingHTTPServer):
    """Ein Faden je Verbindung, aber hoechstens `maximum` gleichzeitig; jede weitere Verbindung wird sofort
    geschlossen, statt Speicher und Faeden des Prozesses zu belegen, der auch den Geraetekern traegt."""

    def __init__(self, adresse, handler, maximum: int) -> None:
        self.request_queue_size = maximum  # Rueckstau wie die Plaetze; der Vorgabewert 5 weist Schuebe schon im Kernel ab
        self._plaetze = threading.BoundedSemaphore(maximum)
        super().__init__(adresse, handler)

    def handle_error(self, request, client_address):
        """Ein Client, der mitten in der Antwort trennt oder zuruecksetzt, oder dessen Anfrage das Lesebudget sprengt
        (ConnectionAbortedError aus _BegrenzterLeser), ist kein Fehler des Dienstes: ohne Traceback. Jeder andere Fehler bleibt mit Traceback im Journal (ein Angreifer im LAN soll das Journal nicht mit
        Tracebacks fluten, S-12)."""
        if isinstance(sys.exc_info()[1], OSError):
            logger.debug("Verbindung von %s abgebrochen", client_address[0])
            return
        super().handle_error(request, client_address)

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
    port: int, snapshot_fn: Callable[[], dict], hostnames: Iterable[str] = (),
    *, nur_health: bool = False, zugang: Zugang | None = None,
) -> ThreadingHTTPServer:
    """`nur_health`: der Gesundheitsserver fuer Compose-Healthcheck und Updater antwortet nur auf /healthz (kein HTML,
    kein Uebernahme-Code, keine Sitzung). Er hat eigene Plaetze und Fristen; die Compose-Datei veroeffentlicht ihn nur
    auf dem Loopback des Hosts: die Plaetze der Diagnoseseite zu fuellen nimmt ihm keine (S-10). Ob ein Gast im selben
    Netz ihn mit Docker vor Version 28 ueber direktes Routing trotzdem erreicht, ist offen (gateway-host.md, Bekannte
    Grenzen).
    Die Seite (ohne `nur_health`) verlangt `zugang`: ohne ihn gaebe es keine Anmeldung, und der Server wuerde nicht
    gestartet (nie offen)."""
    if zugang is None and not nur_health:
        raise ValueError("Die Diagnoseseite braucht einen Zugang (Anmeldung)")
    allowed = frozenset(hostnames)
    frist = VERBINDUNG_FRIST_SECONDS

    class Handler(BaseHTTPRequestHandler):
        timeout = frist  # je Leseoperation; die Uhr in setup/finish begrenzt die ganze Verbindung
        server_version = ""  # kein Server-Banner (S-13)
        sys_version = ""

        def setup(self):
            super().setup()
            self.rfile = _BegrenzterLeser(self.rfile, ANFRAGE_MAX_BYTES)  # pyright: ignore[reportAttributeAccessIssue]
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

        def send_response(self, code, message=None):
            self.send_response_only(code, message)  # ohne Server-Kopf der Basisklasse
            self.send_header("Date", self.date_time_string())

        def send_error(self, code, message=None, explain=None):
            self.close_connection = True
            self._answer(code)  # kein Fehlertext der Basisklasse (nennt die angefragte Methode)

        def _answer(self, code: int, raw: bytes = b"", kind: str | None = None,
                    extra: tuple[tuple[str, str], ...] = ()) -> None:
            self.send_response(code)
            if kind:
                self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            for name, value in extra:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(raw)

        def _seite(self, code: int, text: str, extra: tuple[tuple[str, str], ...] = ()) -> None:
            self._answer(code, text.encode(), "text/html; charset=utf-8", extra)

        def _namen(self) -> frozenset[str]:
            """Erlaubte Host-Namen: SHG_DIAG_HOSTNAMES und der Hostname aus werk.json."""
            if zugang is not None and (werk := zugang.werk()) is not None:
                return allowed | {werk["hostname"]}
            return allowed

        def _fremder_ursprung(self) -> bool:
            """Loest eine fremde Webseite im Browser eines Bewohners diese Anfrage aus? Dann wuerde sie die globale Sperre
            der Anmeldung herbeifuehren (Aussperren des Kunden). Sec-Fetch-Site cross-site/same-site, ein Origin `null`,
            ein Origin mit Host ausserhalb der erlaubten Namen (wie der Host-Header) oder mit einem anderen Host als der
            Host-Header der Anfrage zaehlt dazu (ein IP-Literal einer fremden Seite besteht die Namensliste, aber nicht
            den Vergleich); ohne Origin (curl, aeltere Clients) bleibt der POST erlaubt."""
            if (self.headers.get("Sec-Fetch-Site") or "").strip().lower() in CROSS_SITE:
                return True
            namen = self._namen()
            eigener = _host_vergleich(self.headers.get("Host"))
            for ursprung in self.headers.get_all("Origin", []):
                try:
                    teile = urllib.parse.urlsplit(ursprung.strip())
                except ValueError:
                    return True
                if (teile.scheme not in ("http", "https") or not host_allowed(teile.netloc, namen)
                        or _host_vergleich(teile.netloc) != eigener):
                    return True
            return False

        def _zugelassen(self) -> bool:
            """Client im privaten Netz und erlaubter Host-Header (auch der Hostname aus werk.json); sonst 403."""
            if not client_allowed(self.client_address[0]):
                self._answer(403)
                return False
            if not host_allowed(self.headers.get("Host"), self._namen()):
                self._answer(403)
                return False
            return True

        def _angemeldet(self) -> bool:
            if zugang is None:
                return True  # Gesundheitsserver: nur Loopback des Hosts
            for kopf in self.headers.get_all("Cookie", []):
                for teil in kopf.split(";"):
                    name, _, wert = teil.strip().partition("=")
                    if name == SITZUNG_COOKIE and zugang.sitzungen.gueltig(wert):
                        return True
            return False

        def do_GET(self):
            if not self._zugelassen():
                return
            if self.path == "/anmelden" and not nur_health:
                self._seite(200, render_anmeldung())
                return
            if self.path not in (("/healthz",) if nur_health else ("/", "/healthz")):
                if nur_health or self._angemeldet():
                    self._answer(404)
                else:
                    self._answer(303, extra=(("Location", "/anmelden"),))
                return
            if not self._angemeldet():
                if self.path == "/healthz":
                    self._answer(401)
                else:
                    self._answer(303, extra=(("Location", "/anmelden"),))
                return
            data = snapshot_fn()
            if self.path == "/healthz":
                body = json.dumps({"server_ok": data["server_ok"], "runtime_status": data["laufzeit"],
                                   "zustand": data["zustand"]})
                self._answer(200, body.encode(), "application/json")
                return
            werk = zugang.werk() if zugang is not None else None
            data = {**data, "seriennummer": werk["seriennummer"] if werk is not None else None}
            self._seite(200, render_html(data))

        def do_POST(self):
            if not self._zugelassen():
                return
            if self.path != "/anmelden" or zugang is None:
                if zugang is None or self._angemeldet():
                    self.send_error(501)
                else:
                    self._answer(401)
                return
            self._anmelden(zugang)

        def _anmelden(self, zugang: Zugang) -> None:
            """Formular mit nur dem Passwort: Typ und Laenge werden vor dem Lesen geprueft (hoechstens 256 Byte)."""
            self.close_connection = True  # ein ungelesener Koerper darf nicht als naechste Anfrage gelten
            if self._fremder_ursprung():
                self._answer(403)  # ohne Pruefung und ohne Zaehlung
                return
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != FORMULAR_TYP:
                self._answer(415)
                return
            laenge = self.headers.get("Content-Length", "")
            if self.headers.get("Transfer-Encoding") is not None or not (laenge.isascii() and laenge.isdigit()):
                self._answer(411)
                return
            if len(laenge) > LAENGE_STELLEN_MAX or int(laenge) > ANMELDUNG_MAX_BYTES:  # erst die Stellen, dann int()
                self._answer(413)
                return
            try:
                felder = urllib.parse.parse_qs(self.rfile.read(int(laenge)).decode(), keep_blank_values=True,
                                               strict_parsing=True, max_num_fields=4, errors="strict")
            except ValueError:
                self._answer(400)
                return
            passwort = felder.get("passwort", [])
            if len(passwort) != 1:
                self._answer(400)
                return
            ergebnis = zugang.pruefstelle.anmelden(passwort[0])
            if ergebnis == "ok":
                token = zugang.sitzungen.neu()
                self._answer(303, extra=(("Location", "/"),
                                         ("Set-Cookie", f"{SITZUNG_COOKIE}={token}; {SITZUNG_ATTRIBUTE}")))
                return
            if ergebnis == "falsch":
                logger.warning("Anmeldung fehlgeschlagen von %s", self.client_address[0])  # nie die Eingabe
            code = {"falsch": 401, "gesperrt": 429, "belegt": 429, "kein_werk": 503}[ergebnis]
            warten = (("Retry-After", str(zugang.pruefstelle.sperre_rest() if ergebnis == "gesperrt" else 1)),)
            self._seite(code, render_anmeldung(ergebnis), warten if code == 429 else ())

    server = _BegrenzterServer(("0.0.0.0", port), Handler, MAX_VERBINDUNGEN)
    threading.Thread(target=server.serve_forever, name="diagnose", daemon=True).start()
    return server
