"""Updater des Gateways: liest alle 15 min die Soll-Version (Feld `software` des
Konfigurationsdokuments, von der Laufzeit nach data/software_soll.json geschrieben), laedt und prueft Manifest
(https, SHA-256, X.509-Kette gegen release-roots.pem (kette.py), Version, min_updater_version) und Bundle-Dateien,
holt das eigene Image als OCI-Archiv (Manifest-Feld `archiv`), laedt es mit `docker load` und prueft, dass der Docker
`smartheat-gateway@<Index-Digest>` kennt, schaltet per Compose um (das eigene Image hat `pull_policy: never`, `compose pull`
holt nur Images aus Registries), wartet bis zu 10 min auf "gesund" und rollt sonst auf das vorige Bundle zurueck. Nur streng neuere Versionen werden
geladen (Downgrade-Schutz, Grund version_zu_alt; der Betreiber gibt fuer einen Rueckweg der Flotte den alten Stand als
neue Version heraus). Fristen ueber die monotone Uhr (die Wanduhr springt beim Boot); ein Abruf hat zusaetzlich eine
Gesamtfrist (ABRUF_GESAMT_SECONDS) und geht nur an oeffentliche Adressen (is_public_address). Das Archiv (bis
bundles.MAX_ARCHIVE_BYTES) hat eine eigene Frist (archiv_frist: 60 s plus Groesse bei mindestens MIN_RATE_BYTES_PER_S),
liegt bis zum Laden unter archive/<version>.oci.tar und wird nach einem Abbruch per HTTP-Range fortgesetzt
(fetch_url_to_file; der Anfang bleibt als .part liegen). Ist das Image schon geladen (etwa nach einem gescheiterten pull),
wird das Archiv nicht noch einmal geholt.
Zustand in updater/state.json (atomar, 0644), Format:

    {"current": str|null, "previous": str|null, "rejected": [str],
     "rejected_context": {"updater": str, "roots": str}|null,
     "zertifikat_untergrenze": str|null,
     "in_progress": {"version": str, "previous": str|null, "since": float, "zertifikat_ab": str (optional),
                     "rollback": str (optional), "reject": false (optional)}|null,
     "pending_reports": [{"version": str, "result": str, "reason": str}]}

in_progress wird vor `compose up` geschrieben und uebersteht einen Stromausfall: recover() startet das Bundle dann
erneut (up ist idempotent) und wartet auf Gesundheit. Ist "rollback" gesetzt (Grund), scheiterte der Rueckweg auf
`previous`; recover()/run_once wiederholen ihn, bevor sonst etwas geschieht, und melden das Ergebnis erst danach
("reject": false = die Version kommt danach nicht nach rejected, siehe unten).
rejected hat je Eintrag "<version>@<sha256 ueber Fingerabdruck des Release-Zertifikats + manifest_sha256>": eine neu
signierte Fassung derselben Version (anderes Zertifikat) oder ein anderes Manifest wird wieder versucht (Updates-Spec
Abschnitt 4); anderer Leerraum im Signaturtext oder die zweite ECDSA-Form (s -> n - s) ergeben keine neue Kennung. rejected gilt nur im Kontext
rejected_context (Version des Updaters und Kennung des Schluesselbunds aus release-roots.pem, kette.roots_identity,
oder "placeholder"): aendert sich eines von beiden, wird rejected geleert (ein neuer Updater oder der echte
Schluesselbund darf es neu versuchen).

Zeit und Widerruf (SG-04, FR-25): ein Update wird nur mit synchroner Uhr versucht (uhr(), `zeit_aus_status`: Hoststatus
meldet zeit_synchron und ist hoechstens 15 min alt); sonst "wartet_auf_zeit", ohne Abruf und ohne Ablehnung.
zertifikat_untergrenze (ISO-Zeit) ist das hoechste `notBefore` eines angenommenen Release-Zertifikats, gesetzt erst nach
erfolgreichem Wechsel (in_progress.zertifikat_ab traegt den Wert bis dahin) und nur steigend: kette.verify lehnt aeltere
Zertifikate ab (Widerruf eines gestohlenen Schluessels), und eine Uhr darunter ist falsch ("wartet_auf_zeit").

Umgebung ist kein Fehler des Bundles:
- Die Gesundheitsfrist ruht, solange der Host den Broker nicht erreicht (je Takt eine TCP-Verbindung zu host:port aus
  data/device/endpoint.json; fehlt die Datei, gilt er als nicht erreichbar) - nach einem Stromausfall im ganzen Haus
  kommt der Router oft erst nach dem Pi. Hoechstens HEALTH_CAP_SECONDS insgesamt; ist der Server dann noch immer nicht
  erreichbar, geht es zurueck auf das vorige Bundle,
  ohne die Version abzulehnen (sie wird erneut versucht, solange der Server sie will), gemeldet als
  rollback/server_unerreichbar (RETRY_REASON; rollback/ungesund heisst dagegen immer abgelehnt).
- Fehlt ein Geraetepfad aus `devices:` des Ziel-Bundles (Zigbee-Stick abgezogen), wird nicht umgeschaltet
  ("vorlaeufig"); ein unterbrochenes Update wartet in recover() ebenso, statt zurueckzurollen.
- update_result geht ueber die Warteschlange pending_reports: erst mit dem Zustand gespeichert, dann gesendet; was nicht
  geschrieben werden kann (Datentraeger), wird zu Beginn des naechsten Durchlaufs vor allem anderen erneut gesendet, in
  Reihenfolge (mindestens einmal: ein Stromausfall zwischen Senden und Speichern sendet doppelt). Die Laufzeit sendet
  jede Meldung als up/result (dokument software) genau einmal je seq (smartheat_host.kanal)."""
import errno
import functools
import hashlib
import http.client
import ipaddress
import json
import logging
import os
import random
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from smartheat_core import dateien

from smartheat_gateway.version import GATEWAY_VERSION
from smartheat_host import bundles, kanal, kette

logger = logging.getLogger(__name__)

#: Grund eines Rueckwegs ohne Ablehnung: der Server war waehrend der ganzen Gesundheitspruefung nicht erreichbar, die
#: Version wird erneut versucht (G3-Restpunkt; der Server zeigt ihn dem Betreiber als "wird erneut versucht").
RETRY_REASON = "server_unerreichbar"
#: Gruende einer Ablehnung oder eines Rueckwegs (wire.SOFTWARE_ERRORS des Vertrags, test_vertrag_im_host.py). Der
#: Updater meldet nur diese Namen, nie ein Literal (test_updater.py::test_der_updater_meldet_nur_gruende_aus_reasons).
MANIFEST_UNGUELTIG = "manifest_ungueltig"
SIGNATUR_UNGUELTIG = "signatur_ungueltig"
UPDATER_ZU_ALT = "updater_zu_alt"
VERSION_ZU_ALT = "version_zu_alt"
START_FEHLGESCHLAGEN = "start_fehlgeschlagen"
UNGESUND = "ungesund"
REASONS = (MANIFEST_UNGUELTIG, SIGNATUR_UNGUELTIG, UPDATER_ZU_ALT, VERSION_ZU_ALT, START_FEHLGESCHLAGEN, UNGESUND,
           RETRY_REASON)
HEALTH_SECONDS = 600
HEALTH_POLL_SECONDS = 10
HEALTH_CAP_SECONDS = 3600  # Gesamtgrenze der Gesundheitspruefung, wenn die Frist wegen Unerreichbarkeit ruht
PERIOD_SECONDS = 900
JITTER_SECONDS = 300
FIRST_DELAY_SECONDS = 120
MAX_MANIFEST_BYTES = 65536
ABRUF_GESAMT_SECONDS = 300  # Gesamtfrist eines Abrufs
ABRUF_BLOCK_BYTES = 8192
SOCKET_TIMEOUT_SECONDS = 30  # je Leseoperation, hoechstens der Rest der Gesamtfrist
MAX_FILE_BYTES = 1048576
MIN_RATE_BYTES_PER_S = 250000  # 2 Mbit/s: so langsam darf die Leitung des Kunden hoechstens sein, damit das Archiv fertig wird
ARCHIV_GRUNDFRIST_SECONDS = 60
ARCHIV_BLOCK_BYTES = 65536
MAX_REJECTED = 20
MAX_PENDING_REPORTS = 20
OFFER_KEYS = ("manifest_url", "manifest_sha256", "signature")
STATUS_MAX_ALTER = timedelta(minutes=15)  # so alt darf host/status.json fuer die Uhr hoechstens sein
STATUS_MAX_VORSPRUNG = timedelta(minutes=1)  # so weit darf ts vor der Uhr liegen (Korrektur durch NTP)
WARTET_AUF_ZEIT = "wartet_auf_zeit"
_HEALTHY, _UNHEALTHY, _UNREACHABLE = "gesund", "ungesund", "unerreichbar"
REQUIRED_HEALTHY = ("runtime",)


class FetchError(Exception):
    pass


class _Refused(Exception):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


def _text_or_none(value) -> str | None:
    return value if isinstance(value, str) else None


def _report_entries(value) -> list[dict]:
    """Vorgemerkte Meldungen aus der Zustandsdatei (lokale Eingabe): unbrauchbare und solche mit einem Grund ausserhalb
    REASONS (ausser result "ok") entfallen; wirft nie, der Rueckweg darf an einer Meldung nicht scheitern."""
    if not isinstance(value, list):
        return []
    keys = ("version", "result", "reason")
    entries = [{key: item[key] for key in keys} for item in value
               if isinstance(item, dict) and all(isinstance(item.get(key), str) for key in keys)]
    valid = [entry for entry in entries if entry["result"] == "ok" or entry["reason"] in REASONS]
    if len(valid) != len(entries):
        logger.warning("%d vorgemerkte Meldung(en) mit einem Grund ausserhalb REASONS verworfen",
                       len(entries) - len(valid))
    return valid


def _zeit(value) -> datetime | None:
    """ISO-Zeit mit Zeitzone aus einem Text, sonst None (Zustandsdatei und Auftrag sind lokale Eingabe: wirft nie)."""
    if not isinstance(value, str):
        return None
    try:
        zeit = datetime.fromisoformat(value)
    except ValueError:
        return None
    return zeit if zeit.tzinfo is not None else None


def _zeit_text(value) -> str | None:
    return value if _zeit(value) is not None else None


def zeit_aus_status(status_datei: Path, jetzt: Callable[[], datetime] = lambda: datetime.now(UTC)) -> datetime | None:
    """Die Uhr des Geraets, wenn man ihr trauen darf, sonst None: der Hoststatus (hoststatus.py, je Minute) meldet
    `zeit_synchron` und sein Zeitstempel `ts` ist hoechstens STATUS_MAX_ALTER alt. Ein `ts` ueber STATUS_MAX_VORSPRUNG in
    der Zukunft heisst: die Uhr wurde seit dem Schreiben zurueckgestellt; auch dann keine Zeit. Wirft nie."""
    try:
        status = json.loads(status_datei.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(status, dict) or status.get("zeit_synchron") is not True:
        return None
    stempel = _zeit(status.get("ts"))
    if stempel is None:
        return None
    zeit = jetzt()
    if not -STATUS_MAX_VORSPRUNG <= zeit - stempel <= STATUS_MAX_ALTER:
        return None
    return zeit


@dataclass
class State:
    current: str | None = None
    previous: str | None = None
    rejected: list[str] = field(default_factory=list)
    in_progress: dict | None = None
    rejected_context: dict | None = None
    pending_reports: list[dict] = field(default_factory=list)
    zertifikat_untergrenze: str | None = None

    @classmethod
    def load(cls, path: Path) -> "State":
        """Kaputt oder unbrauchbar gilt als leer; ein in_progress ohne Versionsangabe wird verworfen."""
        try:
            data = json.loads(path.read_text())
            in_progress = data.get("in_progress")
            if not (isinstance(in_progress, dict) and isinstance(in_progress.get("version"), str)):
                in_progress = None
            rejected = [item for item in data.get("rejected") or [] if isinstance(item, str)]
            context = data.get("rejected_context")  # fehlt in aelteren Dateien
            if not (isinstance(context, dict) and all(isinstance(value, str) for value in context.values())):
                context = None
            return cls(_text_or_none(data.get("current")), _text_or_none(data.get("previous")), rejected, in_progress,
                       context, _report_entries(data.get("pending_reports")),
                       _zeit_text(data.get("zertifikat_untergrenze")))
        except (OSError, ValueError, AttributeError, TypeError):
            return cls()

    def save(self, path: Path) -> None:
        dateien.schreiben_json(path, asdict(self), modus=dateien.LESBAR)


_REDIRECT_REFUSED = "Weiterleitung auf ein Schema, das nicht erlaubt ist (nur https)"


def _scheme_allowed(url: str, allow_http: bool) -> bool:
    scheme = urllib.parse.urlsplit(url).scheme
    return scheme == "https" or (allow_http and scheme == "http")


class TooLarge(FetchError):
    """Der Server liefert mehr, als erlaubt ist (Content-Length oder gelesene Bytes ueber der Grenze)."""


class _DeadlineExceeded(FetchError):
    pass


class _Deadline:
    """Gesamtfrist eines Abrufs auf der eingespeisten monotonen Uhr (Standard ABRUF_GESAMT_SECONDS, fuer das Archiv die
    laengere `total`). Sie gilt fuer alles, was zum Abruf gehoert:
    Verbindungsaufbau, TLS-Handshake, Kopfzeilen, jede Weiterleitung und den Koerper. Der Socket-Timeout wird vor jeder
    Leseoperation auf den Rest der Frist (hoechstens SOCKET_TIMEOUT_SECONDS) gesetzt; ein Server, der langsam tropft, kommt
    so nirgends an der Frist vorbei."""

    def __init__(self, clock: Callable[[], float], total: float | None = None) -> None:
        self._clock = clock
        self._total = ABRUF_GESAMT_SECONDS if total is None else total
        self._end = clock() + self._total

    def expired(self) -> bool:
        return self._clock() >= self._end

    def check(self) -> None:
        self.socket_timeout()

    def socket_timeout(self) -> float:
        left = self._end - self._clock()
        if left <= 0:
            raise _DeadlineExceeded(f"Gesamtfrist von {self._total:g} s ueberschritten")
        return min(SOCKET_TIMEOUT_SECONDS, left)


class _SchemeCheckedRedirects(urllib.request.HTTPRedirectHandler):
    """urllib folgt auch einer Weiterleitung von https auf http. Ein Ziel, dessen Schema nicht erlaubt ist, wird abgelehnt,
    bevor es angefragt wird (ein kompromittierter Server soll keinen Abruf ins LAN des Kunden ausloesen). Ist die
    Gesamtfrist des Abrufs abgelaufen, folgt der Handler auch einem erlaubten Ziel nicht mehr."""

    def __init__(self, allow_http: bool, deadline: _Deadline | None = None) -> None:
        super().__init__()
        self._allow_http, self._deadline = allow_http, deadline

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            if self._deadline is not None:
                self._deadline.check()
            if not _scheme_allowed(newurl, self._allow_http):
                raise FetchError(f"{req.full_url}: {_REDIRECT_REFUSED}")
        except FetchError:
            fp.close()  # urllib schliesst die 302-Antwort erst nach einem erfolgreichen redirect_request
            raise
        follow_up = super().redirect_request(req, fp, code, msg, headers, newurl)
        fp.close()  # urllib liest den Koerper der 302-Antwort sonst ohne Grenze (`fp.read()`); geschlossen liefert er b""
        return follow_up


#: Eigener User-Agent: die Browser-Integritaetspruefung von Cloudflare sperrt den Standard von urllib (Python-urllib,
#: HTTP 403, Fehler 1010; Labor 2026-10-20). urllib setzt ihn auch auf dem Ziel einer Weiterleitung.
USER_AGENT = f"SmartHeat-Gateway-Updater/{GATEWAY_VERSION}"


_NAT64_WELL_KNOWN = ipaddress.ip_network("64:ff9b::/96")  # RFC 6052: IPv4 in den letzten 32 Bit
_NAT64_LOCAL_USE = ipaddress.ip_network("64:ff9b:1::/48")  # RFC 8215: Praefixlaenge /32 bis /96 waehlt der Betreiber


def _address(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Adresse ohne Scope-Kennung (fe80::1%eth0); eine IPv4-Adresse in IPv6-Schreibweise (`::ffff:a.b.c.d`) als IPv4.
    Wirft ValueError bei Unlesbarem."""
    address = ipaddress.ip_address(text.split("%", 1)[0])
    mapped = getattr(address, "ipv4_mapped", None)
    return address if mapped is None else mapped


def is_public_address(text: str) -> bool:
    """Ist das eine global erreichbare Adresse (`is_global`)? Nicht: RFC 1918, Loopback, Link-Local, CGNAT, ULA und
    andere Sonderbereiche. Eine IPv4-Adresse in IPv6-Schreibweise (`::ffff:a.b.c.d`) zaehlt wie die IPv4-Adresse,
    unabhaengig davon, wie die Python-Version sie selbst einstuft; bei NAT64-Adressen des festen Praefixes `64:ff9b::/96`
    entscheidet die eingebettete IPv4-Adresse (eine globale bleibt erlaubt, IPv6-Netze ohne IPv4 brauchen das). Das
    betreiberlokale Praefix `64:ff9b:1::/48` ist immer abgelehnt: seine Praefixlaenge (und damit die Lage der IPv4-Adresse)
    waehlt der Betreiber, ein Raten waere ein Auslegungsunterschied zum Uebersetzer.
    Unlesbares gilt als nicht erlaubt. Das allein deckt NICHT ab: globale IPv6-Adressen des eigenen Netzes (Router, Geraete
    im eigenen /64, der Rechner selbst; dafuer `is_own_network`) und Geraete in anderen IPv6-Praefixen des Kundennetzes
    mit globaler Adresse."""
    try:
        address = _address(text)
    except ValueError:
        return False
    if address.version == 6:
        packed = address.packed
        if address in _NAT64_WELL_KNOWN:
            return ipaddress.IPv4Address(packed[12:]).is_global
        if address in _NAT64_LOCAL_USE:
            return False
    return address.is_global


def is_own_network(peer: str, own: str) -> bool:
    """Liegt die Gegenadresse bei der eigenen Adresse des Sockets? Gleiche Adresse (auch IPv4 gegen gemappt) oder bei
    IPv6 im selben /64 (SLAAC: Router, LAN-Geraete und der Rechner selbst teilen sich das Praefix einer globalen
    Adresse, die `is_global` durchlaesst). Unlesbares gilt als eigenes Netz (fail closed)."""
    try:
        theirs, mine = _address(peer), _address(own)
    except ValueError:
        return True
    if theirs == mine:
        return True
    return theirs.version == 6 and mine.version == 6 and theirs in ipaddress.ip_network(f"{mine}/64", strict=False)


class _DeadlineSocket(socket.socket):
    """Klartext-Socket (nur http, also nur Tests), der vor jedem Lesen den Timeout auf den Rest der Gesamtfrist
    setzt. Uebernimmt die Verbindung des uebergebenen Sockets."""

    def __init__(self, sock: socket.socket, deadline: _Deadline) -> None:
        super().__init__(sock.family, sock.type, sock.proto, fileno=sock.detach())
        self._deadline = deadline
        self.settimeout(deadline.socket_timeout())

    def recv_into(self, buffer, nbytes=0, flags=0):
        self.settimeout(self._deadline.socket_timeout())
        return super().recv_into(buffer, nbytes, flags)


def _tls_context(deadline: _Deadline) -> ssl.SSLContext:
    """Standardkontext (Zertifikat- und Namenspruefung) mit einem Socket, der vor jedem Lesen den Timeout auf den Rest der
    Gesamtfrist setzt (wie `_DeadlineSocket`, nach dem TLS-Handshake)."""

    class DeadlineSSLSocket(ssl.SSLSocket):
        def recv_into(self, buffer, nbytes=None, flags=0):
            self.settimeout(deadline.socket_timeout())
            return super().recv_into(buffer, nbytes, flags)

    context = ssl.create_default_context()
    context.set_alpn_protocols(["http/1.1"])  # wie der Standardkontext von http.client
    context.sslsocket_class = DeadlineSSLSocket
    return context


def _guard_connection(connection: http.client.HTTPConnection, deadline: _Deadline, *, public_only: bool,
                      plain: bool) -> None:
    """Haengt an `_create_connection` der Verbindung (Instanzattribut von http.client, sonst `socket.create_connection`):
    - Verbindungsaufbau mit dem Rest der Gesamtfrist als Timeout (eine abgelaufene Frist baut keine Verbindung auf);
    - public_only: Gegenadresse des verbundenen Sockets nur oeffentlich (`is_public_address`) und nicht die eigene
      Adresse oder das eigene IPv6-/64 (`is_own_network`), geprueft nach dem TCP-Aufbau und vor dem TLS-Handshake: der
      TCP-Handshake erreicht auch eine abgelehnte Adresse, Nutzdaten (ClientHello, Anfrage) gehen nicht hin, der Socket wird
      vor dem Fehler geschlossen. Geprueft wird die Adresse des Sockets, nicht der Name aus der URL: der Name kann beim
      Aufloesen auf jede Adresse zeigen. Der Verbindungsaufbau hat den Rest der Frist als Timeout je aufgeloester
      Adresse (`socket.create_connection` setzt ihn fuer jede neu);
    - plain: Klartext-Socket mit Frist je Leseoperation (bei https setzt der TLS-Kontext sie)."""
    connect = vars(connection)["_create_connection"]  # fehlt es in einer anderen Python-Version, scheitert das laut

    def create(address, timeout=None, source_address=None):
        sock = connect(address, deadline.socket_timeout(), source_address)
        if public_only:
            try:
                peer, own = sock.getpeername(), sock.getsockname()
                found = peer[0] if isinstance(peer, tuple) and peer else ""
                mine = own[0] if isinstance(own, tuple) and own else ""
                public = isinstance(found, str) and is_public_address(found) and not is_own_network(found, mine)
            except OSError:
                found, public = "", False
            if not public:
                sock.close()
                raise FetchError(f"{connection.host}: Gegenadresse {found or 'unbekannt'} nicht oeffentlich")
        return _DeadlineSocket(sock, deadline) if plain else sock

    vars(connection)["_create_connection"] = create


class _FetchHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, deadline: _Deadline, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        _guard_connection(self, deadline, public_only=False, plain=True)


class _FetchHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, deadline: _Deadline, public_only: bool, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        _guard_connection(self, deadline, public_only=public_only, plain=False)


class _FetchHTTPHandler(urllib.request.HTTPHandler):
    """Nur mit `allow_http` im Opener (nur Tests): http mit Gesamtfrist."""

    def __init__(self, deadline: _Deadline) -> None:
        super().__init__()
        self._deadline = deadline

    def http_open(self, req):
        return self.do_open(functools.partial(_FetchHTTPConnection, deadline=self._deadline), req)


class _FetchHTTPSHandler(urllib.request.HTTPSHandler):
    """urllib baut je Anfrage (auch je Weiterleitung) eine neue Verbindung ueber diesen Handler: Gesamtfrist und
    Adresspruefung gelten fuer jede."""

    def __init__(self, deadline: _Deadline, public_only: bool) -> None:
        super().__init__()
        self._deadline, self._public_only, self._tls = deadline, public_only, _tls_context(deadline)

    def https_open(self, req):
        connection = functools.partial(_FetchHTTPSConnection, deadline=self._deadline, public_only=self._public_only)
        return self.do_open(connection, req, context=self._tls)


def checked_opener(allow_http: bool, deadline: _Deadline | None = None) -> urllib.request.OpenerDirector:
    """Opener des Updaters: Weiterleitungen nur auf erlaubte Schemata, alles innerhalb der Gesamtfrist, https nur zu
    oeffentlichen Adressen (mit `allow_http`, nur Tests, ohne diese Pruefung), nie ueber einen Proxy aus der
    Umgebung (ein Proxy liesse die Pruefung nur dessen Adresse sehen und das Ziel von ihm aufloesen)."""
    deadline = deadline or _Deadline(time.monotonic)
    handlers: list = [_SchemeCheckedRedirects(allow_http, deadline), urllib.request.ProxyHandler({}),
                      _FetchHTTPSHandler(deadline, public_only=not allow_http)]
    if allow_http:
        handlers.append(_FetchHTTPHandler(deadline))
    opener = urllib.request.build_opener(*handlers)
    opener.addheaders = [("User-Agent", USER_AGENT)]
    return opener


def fetch_url(url: str, max_bytes: int, allow_http: bool, clock: Callable[[], float] = time.monotonic) -> bytes:
    """Holt `url` (hoechstens `max_bytes`) in hoechstens ABRUF_GESAMT_SECONDS insgesamt (monotone Uhr): Verbindung,
    TLS, Kopfzeilen, Weiterleitungen und Koerper laufen auf derselben Frist (`_Deadline`); gelesen wird in Bloecken.
    Grenzen: das Aufloesen des Namens (getaddrinfo) hat keine eigene Frist; der Verbindungsaufbau hat den Rest der Frist
    (hoechstens 30 s) als Timeout je aufgeloester Adresse, bei vielen toten Adressen also mehrfach."""
    if not _scheme_allowed(url, allow_http):
        raise FetchError(f"nur https erlaubt: {url}")
    deadline = _Deadline(clock)
    try:
        with checked_opener(allow_http, deadline).open(url, timeout=SOCKET_TIMEOUT_SECONDS) as response:
            # zweite Sicherung: die Adresse, von der der Inhalt kam, gilt wie die angefragte; der Inhalt wird danach
            # zusaetzlich nur nach SHA-256 und Signatur angenommen
            if not _scheme_allowed(response.geturl(), allow_http):
                raise FetchError(f"{url}: {_REDIRECT_REFUSED}")
            blocks: list[bytes] = []
            size = 0
            while size <= max_bytes:
                deadline.check()
                block = response.read1(min(ABRUF_BLOCK_BYTES, max_bytes + 1 - size))  # read1: kein Warten auf einen vollen Block
                if not block:
                    break
                blocks.append(block)
                size += len(block)
    except _DeadlineExceeded as error:
        raise FetchError(f"{url}: {error}") from None
    except FetchError:
        raise
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as error:
        # ein Socket-Timeout am Ende der Frist ist die Frist (der Rest der Frist war der Timeout)
        detail = "Gesamtfrist ueberschritten" if deadline.expired() else type(error).__name__
        raise FetchError(f"{url}: {detail}") from None
    if size > max_bytes:
        raise FetchError(f"{url}: zu gross")
    return b"".join(blocks)


def archiv_frist(size: int) -> float:
    """Frist des Archivabrufs in Sekunden: ARCHIV_GRUNDFRIST_SECONDS plus die Zeit fuer `size` Byte bei
    MIN_RATE_BYTES_PER_S. Langsamer als 2 Mbit/s wird ein Archiv nie fertig; der Abruf setzt dann in jedem Durchlauf fort."""
    return ARCHIV_GRUNDFRIST_SECONDS + size / MIN_RATE_BYTES_PER_S


_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+|\*)")
_CONTENT_RANGE_UNSATISFIED = re.compile(r"bytes \*/(\d+)")


def _hash_file(path: Path):
    """SHA-256-Objekt ueber den Inhalt der Datei (in Bloecken: ein Archiv passt nicht in den Speicher eines Pi)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while block := handle.read(ARCHIV_BLOCK_BYTES):
            digest.update(block)
    return digest


def _file_sha256(path: Path) -> str:
    return _hash_file(path).hexdigest()


class _DiskError(Exception):
    """Ein Fehler der Platte (OSError) mitten im Abruf: er soll nicht als Netzfehler gelten (URLError ist selbst ein
    OSError), sondern unveraendert nach oben, wo run_once ihn als Dateifehler meldet."""


def _disk(operation, *args):
    try:
        return operation(*args)
    except OSError as error:
        raise _DiskError(error) from None


def _open_teil(teil: Path, anhaengen: bool):
    """Oeffnet `ziel.part` zum Schreiben: ans Ende (Fortsetzen) oder neu."""
    return open(teil, "ab" if anhaengen else "wb")


def fetch_url_to_file(url: str, max_bytes: int, allow_http: bool, ziel: Path, frist: float,
                      clock: Callable[[], float] = time.monotonic) -> str:
    """Holt `url` (hoechstens `max_bytes`) nach `ziel` in hoechstens `frist` Sekunden und gibt die SHA-256 (Hex) der Datei
    zurueck. Gleiche Grenzen wie fetch_url (Schema, oeffentliche Adresse, Weiterleitungen, Frist ueber Verbindung, TLS,
    Kopfzeilen und Koerper), aber auf die Platte: Geschrieben wird in `ziel.part`, umbenannt erst nach vollstaendigem
    Abruf (laut Content-Length bzw. Content-Range; ohne Laengenangabe gilt das Ende des Stroms, die Groesse prueft der
    Aufrufer). Nach einem Abbruch (Netz, Frist) bleibt `ziel.part` liegen, und der naechste Aufruf setzt per
    `Range: bytes=<Groesse>-` dahinter fort; antwortet der Server mit 200 statt 206, beginnt der Abruf von vorn, ein
    `.part` ueber `max_bytes` wird verworfen, ebenso eines, zu dem der Server nichts sagen kann (416 mit anderer Groesse).
    Eine Antwort ueber `max_bytes` ist TooLarge und hinterlaesst nichts. Wirft FetchError (nie ein anderer Fehler der
    Antwort); Fehler beim Schreiben der Platte sind OSError."""
    if not _scheme_allowed(url, allow_http):
        raise FetchError(f"nur https erlaubt: {url}")
    teil = ziel.with_name(ziel.name + ".part")
    deadline = _Deadline(clock, frist)
    try:
        have = teil.stat().st_size
    except FileNotFoundError:
        have = 0
    if have > max_bytes:
        teil.unlink()
        have = 0
    request = urllib.request.Request(url)
    if have:
        request.add_header("Range", f"bytes={have}-")
    digest = hashlib.sha256()
    try:
        with checked_opener(allow_http, deadline).open(request, timeout=SOCKET_TIMEOUT_SECONDS) as response:
            if not _scheme_allowed(response.geturl(), allow_http):
                raise FetchError(f"{url}: {_REDIRECT_REFUSED}")
            written, total = 0, None
            length = response.headers.get("Content-Length", "")
            if have and response.status == 206:
                match = _CONTENT_RANGE.fullmatch(response.headers.get("Content-Range", ""))
                if match is None or int(match.group(1)) != have:
                    teil.unlink(missing_ok=True)
                    raise FetchError(f"{url}: Teilantwort beginnt nicht bei {have}")
                written = have
                total = int(match.group(3)) if match.group(3) != "*" else None
                digest = _disk(_hash_file, teil)
            elif length.isdigit():
                total = int(length)
            if total is not None and total > max_bytes:
                raise TooLarge(f"{url}: zu gross")
            out = _disk(_open_teil, teil, bool(written))
            try:
                while True:
                    deadline.check()
                    block = response.read1(ARCHIV_BLOCK_BYTES)
                    if not block:
                        break
                    written += len(block)
                    if written > max_bytes:
                        raise TooLarge(f"{url}: zu gross")
                    _disk(out.write, block)
                    digest.update(block)
                _disk(out.flush)
                _disk(os.fsync, out.fileno())
            finally:
                _disk(out.close)
            if total is not None and written != total:
                raise FetchError(f"{url}: abgebrochen nach {written} von {total} Byte")
    except TooLarge:
        teil.unlink(missing_ok=True)
        raise
    except urllib.error.HTTPError as error:
        match = _CONTENT_RANGE_UNSATISFIED.fullmatch(error.headers.get("Content-Range", "")) if error.code == 416 else None
        if match is None or not have:
            raise FetchError(f"{url}: HTTP {error.code}") from None
        if int(match.group(1)) != have:  # das .part gehoert nicht zu dieser Datei
            teil.unlink(missing_ok=True)
            raise FetchError(f"{url}: Teilabruf passt nicht zur Datei") from None
        digest = _hash_file(teil)  # das .part ist schon vollstaendig (Abbruch vor dem Umbenennen); OSError geht nach oben
    except _DiskError as error:
        raise error.args[0] from None
    except _DeadlineExceeded as error:
        raise FetchError(f"{url}: {error}") from None
    except FetchError:
        raise
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as error:
        detail = "Gesamtfrist ueberschritten" if deadline.expired() else type(error).__name__
        raise FetchError(f"{url}: {detail}") from None
    os.replace(teil, ziel)
    return digest.hexdigest()


def fetch_health(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            data = json.loads(response.read(65536))
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def health_ok(data: dict | None, containers: dict[str, str]) -> bool:
    """Gesund: Serverkontakt laut /healthz, Laufzeit nicht im Zustand "startet"
    und der Compose-Healthcheck der Laufzeit meldet "healthy". Der Healthcheck der Laufzeit (Lebenszeichen
    der Konfigurationswache) unterscheidet eine ruhende ("nicht eingerichtet": Status abgeraeumt, runtime_status None)
    von einer abstuerzenden Laufzeit."""
    if not isinstance(data, dict) or data.get("server_ok") is not True or data.get("runtime_status") == "startet":
        return False
    return all(containers.get(name) == "healthy" for name in REQUIRED_HEALTHY)


def _roots_identity(roots_text: str) -> str:
    """Kennung des Schluesselbunds aus release-roots.pem (kette.roots_identity) oder "placeholder"/"ungueltig"; Teil des
    Kontexts der abgelehnten Versionen."""
    try:
        return kette.roots_identity(roots_text)
    except kette.SignatureError:
        return "placeholder" if kette.PLACEHOLDER_MARKER in roots_text else "ungueltig"


def _rejected_key(version: str, manifest_sha256: object, signatur: object) -> str:
    """Eintrag in `rejected`: "<version>@<sha256 (hex) ueber Fingerabdruck des Release-Zertifikats + manifest_sha256>".
    Die Kennung haengt an Zertifikat und Manifest, nicht an den Bytes der Signatur: anderer Leerraum im Signaturtext oder
    die zweite ECDSA-Form (s -> n - s) ergeben keine neue Kennung; eine Neusignatur mit anderem Zertifikat oder ein
    anderes Manifest schon. Fehlt eines von beiden oder ist es kein Text, zaehlt es als leer."""
    text = signatur.decode(errors="replace") if isinstance(signatur, bytes) else signatur
    abdruck = kette.zertifikat_abdruck(text) if isinstance(text, str) else ""
    sha = manifest_sha256.lower() if isinstance(manifest_sha256, str) else ""
    return f"{version}@{hashlib.sha256((abdruck + sha).encode()).hexdigest()}"


def _hoehere_untergrenze(bisher: str | None, neu: object) -> str | None:
    """Die Untergrenze steigt nur: das spaetere von `bisher` und `neu` (ISO-Zeiten); ein unbrauchbares `neu` aendert
    nichts."""
    neue_zeit, alte_zeit = _zeit(neu), _zeit(bisher)
    if neue_zeit is None or (alte_zeit is not None and neue_zeit <= alte_zeit):
        return bisher
    return neue_zeit.isoformat()


def _older_or_equal(version: str, current: str | None) -> bool:
    """Downgrade-Schutz: nur streng neuere Soll-Versionen. Der eigene Rueckweg (_rollback auf
    previous) ist davon unberuehrt; unlesbare Versionen entscheidet die Manifest-Pruefung."""
    if current is None:
        return False
    try:
        return bundles.parse_version(version) <= bundles.parse_version(current)
    except bundles.ManifestError:
        return False


@dataclass(frozen=True)
class _Paket:
    """Was der Updater nach der Kettenpruefung in der Hand hat: die Bundle-Dateien samt Manifest und Signatur (alles fuer
    bundles/<version>/), das Release-Zertifikat, das gepruefte Manifest und die Adresse, neben der das Archiv liegt."""
    files: dict[str, bytes]
    zertifikat: x509.Certificate
    manifest: bundles.Manifest
    basis_url: str


class Updater:
    def __init__(self, *, kanal: kanal.LokalerKanal, store: bundles.BundleStore, compose,
                 fetch: Callable[[str, int], bytes], fetch_datei: Callable[[str, int, Path, float], str],
                 roots_text: str, uhr: Callable[[], datetime | None],
                 state_path: Path, version: str, health: Callable[[str], bool],
                 docker_load: Callable[[Path], None] = bundles.docker_load,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, allow_http: bool = False,
                 health_seconds: float = HEALTH_SECONDS, path_exists: Callable[[str], bool] = os.path.exists) -> None:
        self._kanal, self._store, self._compose, self._fetch = kanal, store, compose, fetch
        self._fetch_datei, self._docker_load = fetch_datei, docker_load
        self._uhr, self._state_path, self._version = uhr, state_path, version
        self._health, self._clock, self._sleep, self._allow_http = health, clock, sleep, allow_http
        self._health_seconds, self._path_exists = health_seconds, path_exists
        self._context = {"updater": version, "roots": _roots_identity(roots_text)}
        try:
            self._roots, self._roots_fehler = kette.load_roots(roots_text), None
        except kette.SignatureError as error:  # Platzhalter oder unbrauchbar: jedes Angebot wird abgelehnt
            self._roots, self._roots_fehler = [], str(error)

    def _load(self) -> State:
        """Zustand laden; eine Ablehnung gilt nur fuer denselben Updater und denselben Schluesselbund."""
        state = State.load(self._state_path)
        if state.rejected_context != self._context:
            state.rejected = []
        state.rejected_context = dict(self._context)
        return state

    # --- Ablauf ---

    def recover(self) -> None:
        """Nach einem Neustart: siehe `_recover`; ein Dateifehler wird geloggt und beendet den Aufruf."""
        try:
            self._recover()
        except OSError as error:
            self._file_error("recover", error)

    def run_once(self) -> str:
        """Ein Durchlauf (Ergebnis: aktuell, vorlaeufig, wartet_auf_zeit, abgelehnt, aktualisiert, zurueckgerollt). Ein
        Dateifehler (Datentraeger voll oder schreibgeschuetzt, Zustandsdatei) ist kein Fehler des Bundles: ERROR mit dem
        Namen des Fehlers, Ergebnis "vorlaeufig"; der Zustand bleibt, wie die letzte erfolgreiche Speicherung ihn liess
        (ein unterbrochenes Update setzt recover() im naechsten Durchlauf fort)."""
        try:
            return self._run_once()
        except OSError as error:
            self._file_error("run_once", error)
            return "vorlaeufig"

    @staticmethod
    def _file_error(where: str, error: OSError) -> None:
        name = errno.errorcode.get(error.errno or 0, type(error).__name__)
        logger.error("%s: Dateifehler %s (%s), Durchlauf vorlaeufig, naechster Durchlauf versucht es erneut", where, name,
                     error.strerror or error)

    def _recover(self) -> None:
        """Nach einem Neustart: ein unterbrochenes Update zu Ende bringen oder zuruecknehmen. Nach einem Stromausfall
        laufen eventuell noch die alten Container (restart: unless-stopped) und wirken gesund; deshalb wird das neue
        Bundle erst erneut hochgefahren (up ist idempotent), bevor die Gesundheit zaehlt."""
        state = self._load()
        self._flush(state)  # offene Meldungen vor allen neuen
        progress = state.in_progress
        if not progress:
            return
        version = progress["version"]
        if isinstance(progress.get("rollback"), str):
            logger.warning("Rueckweg von %s war nicht abgeschlossen, wiederhole ihn", version)
            self._rollback(state, version, progress["rollback"], reject=progress.get("reject") is not False)
            return
        if self._store.exists(version):
            missing = self._missing_devices((self._store.dir(version) / "docker-compose.yml").read_text())
            if missing:
                logger.warning("Unterbrochenes Update auf %s wartet: Geraet fehlt (%s)", version, ", ".join(missing))
                return
        logger.warning("Unterbrochenes Update auf %s gefunden, starte es erneut und pruefe Gesundheit", version)
        try:
            self._compose.up(version)
        except bundles.ComposeError as error:
            logger.error("Start von %s gescheitert: %s", version, error)
            self._rollback(state, version, START_FEHLGESCHLAGEN)
            return
        self._settle(state, version)

    def _run_once(self) -> str:
        if State.load(self._state_path).in_progress:  # offener Auftrag (z. B. gescheiterter Rueckweg) zuerst
            self.recover()  # sendet zuerst die offenen Meldungen
            if State.load(self._state_path).in_progress:
                return "vorlaeufig"
        else:
            self._flush(self._load())  # offene Meldungen vor allem anderen
        state = self._load()
        try:
            desired = self._kanal.desired()
        except kanal.KanalFehler as error:  # Soll-Software noch nicht da oder unlesbar: vorlaeufig, nie ein Grund zum Ablehnen
            logger.info("Soll-Version nicht abrufbar (%s), naechster Durchlauf", error)
            return "vorlaeufig"
        version = desired.get("version")
        signatur = desired.get("signature")
        manifest_sha = desired.get("manifest_sha256")
        if (not isinstance(version, str) or version == state.current
                or _rejected_key(version, manifest_sha, signatur) in state.rejected):
            return "aktuell"
        if not any(desired.get(key) for key in OFFER_KEYS):  # nur eine Version, kein Bundle angeboten
            return "aktuell"
        # keine verlaessliche Uhr (oder eine vor dem juengsten angenommenen Zertifikat): kein Versuch und keine
        # Ablehnung - die Zeit ist kein Fehler des Bundles
        jetzt = self._uhr()
        untergrenze = _zeit(state.zertifikat_untergrenze)
        if jetzt is None:
            logger.info("Update %s wartet: die Uhr ist nicht synchron", version)
            return WARTET_AUF_ZEIT
        if untergrenze is not None and jetzt < untergrenze:
            logger.warning("Update %s wartet: die Uhr (%s) steht vor dem juengsten angenommenen Zertifikat (%s)", version,
                           jetzt.isoformat(), untergrenze.isoformat())
            return WARTET_AUF_ZEIT
        if _older_or_equal(version, state.current):
            logger.error("Soll-Version %s ist nicht neuer als %s, abgelehnt (Downgrade-Schutz)", version, state.current)
            self._reject(state, version, VERSION_ZU_ALT, manifest_sha, signatur)
            return "abgelehnt"
        try:
            paket = self._download(desired, version, jetzt, untergrenze)
            missing = self._missing_devices(paket.files["docker-compose.yml"].decode("utf-8"))
            if missing:  # z. B. Zigbee-Stick abgezogen: up und Rueckweg scheiterten beide, kein Fehler des Bundles
                logger.warning("Bundle %s wartet: Geraet fehlt (%s), naechster Durchlauf", version, ", ".join(missing))
                return "vorlaeufig"  # vor dem Archiv: die rund 200 MB nicht umsonst holen
            self._image_bereitstellen(paket, version)
        except FetchError as error:
            logger.warning("Bundle %s nicht ladbar (%s), naechster Durchlauf", version, error)
            return "vorlaeufig"
        except bundles.LoadError as error:  # Datentraeger voll, Daemon beschaeftigt: kein Fehler des Angebots
            logger.error("Image von %s nicht geladen (%s), naechster Durchlauf", version, error)
            return "vorlaeufig"
        except _Refused as refused:
            logger.error("Bundle %s abgelehnt: %s", version, refused)
            self._reject(state, version, refused.reason, manifest_sha, signatur)
            return "abgelehnt"
        zertifikat = paket.zertifikat
        self._store.install(version, paket.files)  # erst nach allen Pruefungen, atomar
        try:
            self._compose.pull(version)
        except bundles.ComposeError as error:
            logger.warning("docker compose pull fuer %s gescheitert (%s), naechster Durchlauf", version, error)
            return "vorlaeufig"
        state.in_progress = {"version": version, "previous": state.current, "since": time.time(),
                             "zertifikat_ab": zertifikat.not_valid_before_utc.isoformat()}
        state.save(self._state_path)  # vor dem Umschalten: ein Stromausfall findet den Auftrag wieder
        try:
            self._compose.up(version)
        except bundles.ComposeError as error:
            logger.error("Start von %s gescheitert: %s", version, error)
            return "zurueckgerollt" if self._rollback(state, version, START_FEHLGESCHLAGEN) else "vorlaeufig"
        return self._settle(state, version)

    def _settle(self, state: State, version: str) -> str:
        """Auf Gesundheit warten, dann abschliessen oder zurueckrollen (ohne Ablehnung, wenn nur der Server fehlte)."""
        outcome = self._wait_healthy(version)
        if outcome == _HEALTHY:
            self._finish(state, version)
            return "aktualisiert"
        if outcome == _UNREACHABLE:
            logger.error("Server nach %d s nicht erreichbar, %s wird spaeter erneut versucht", HEALTH_CAP_SECONDS,
                         version)
        if outcome == _UNHEALTHY:
            done = self._rollback(state, version, UNGESUND)
        else:
            done = self._rollback(state, version, RETRY_REASON, reject=False)
        return "zurueckgerollt" if done else "vorlaeufig"

    # --- Schritte ---

    def _download(self, desired: dict, version: str, jetzt: datetime, untergrenze: datetime | None) -> _Paket:
        """Prueft und laedt; liefert die Bundle-Dateien plus Manifest und Signatur (alles fuer bundles/<version>/), das
        Release-Zertifikat, mit dem das Manifest signiert ist, das gepruefte Manifest und seine Adresse. Das Archiv des
        Images holt erst `_image_bereitstellen`."""
        url, expected_sha, signature = (desired.get(key) for key in OFFER_KEYS)
        if not isinstance(url, str):
            raise _Refused(MANIFEST_UNGUELTIG, "Manifest-URL fehlt oder ist kein Text")
        try:
            parts = urllib.parse.urlsplit(url)
        except ValueError:
            raise _Refused(MANIFEST_UNGUELTIG, f"Manifest-URL unlesbar: {url!r}") from None
        if parts.scheme != "https" and not (self._allow_http and parts.scheme == "http"):
            raise _Refused(MANIFEST_UNGUELTIG, f"Manifest-URL muss https sein: {url}")
        if not parts.hostname:
            raise _Refused(MANIFEST_UNGUELTIG, f"Manifest-URL ohne Host: {url}")
        if not (isinstance(expected_sha, str) and len(expected_sha) == 64):
            raise _Refused(MANIFEST_UNGUELTIG, "manifest_sha256 fehlt oder ist kein SHA-256")
        if not isinstance(signature, str):
            raise _Refused(SIGNATUR_UNGUELTIG, "Signatur fehlt oder ist kein Text")
        raw = self._fetch(url, MAX_MANIFEST_BYTES)
        if hashlib.sha256(raw).hexdigest() != expected_sha.lower():
            raise _Refused(MANIFEST_UNGUELTIG, "SHA-256 des Manifests passt nicht")
        if self._roots_fehler is not None:
            raise _Refused(SIGNATUR_UNGUELTIG, self._roots_fehler)
        try:
            zertifikat = kette.verify(self._roots, raw, signature, jetzt, untergrenze)
        except kette.SignatureError as error:
            raise _Refused(SIGNATUR_UNGUELTIG, str(error)) from None
        try:
            manifest = bundles.parse_manifest(raw)
        except bundles.ManifestError as error:
            raise _Refused(MANIFEST_UNGUELTIG, str(error)) from None
        if manifest.version != version:
            raise _Refused(MANIFEST_UNGUELTIG, f"Manifest nennt {manifest.version}, Soll ist {version}")
        if bundles.parse_version(manifest.min_updater_version) > bundles.parse_version(self._version):
            raise _Refused(UPDATER_ZU_ALT, f"Bundle verlangt Updater {manifest.min_updater_version}")
        files = {}
        for name, sha in manifest.files.items():
            data = self._fetch(urllib.parse.urljoin(url, name), MAX_FILE_BYTES)
            if hashlib.sha256(data).hexdigest() != sha:
                raise _Refused(MANIFEST_UNGUELTIG, f"SHA-256 von {name} passt nicht")
            files[name] = data
        try:
            bundles.check_compose(files["docker-compose.yml"].decode("utf-8"), manifest.archiv)
        except (bundles.ManifestError, UnicodeDecodeError) as error:
            raise _Refused(MANIFEST_UNGUELTIG, str(error)) from None
        files[bundles.MANIFEST_NAME] = raw
        files[bundles.SIGNATURE_NAME] = signature.encode()
        return _Paket(files, zertifikat, manifest, url)

    def _image_bereitstellen(self, paket: _Paket, version: str) -> None:
        """Sorgt dafuer, dass der Docker des Geraets das eigene Image `smartheat-gateway@<Index-Digest>` kennt: ist es schon
        da (etwa nach einem gescheiterten pull), ist nichts zu holen. Sonst Archiv holen (Frist nach Groesse, Fortsetzen
        nach Abbruch), gegen das signierte Manifest pruefen (Groesse, SHA-256), mit `docker load` laden und pruefen, dass
        das Image nun da ist (sonst MANIFEST_UNGUELTIG: das Archiv enthaelt nicht das signierte Image); das Archiv wird
        danach geloescht. Ein Fehler des Ladens (LoadError) laesst das gepruefte Archiv fuer den naechsten Durchlauf liegen."""
        archiv = paket.manifest.archiv
        if self._compose.has_image(archiv.ref):
            logger.info("Image %s ist schon geladen, das Archiv wird nicht geholt", archiv.ref)
            self._store.clean_archives(None)  # ein liegengebliebenes Archiv wird nicht mehr gebraucht
            return
        self._store.clean_archives(version)  # Reste frueherer Versionen
        pfad = self._store.archive_path(version)
        pfad.parent.mkdir(parents=True, exist_ok=True)
        if pfad.is_file() and (pfad.stat().st_size != archiv.bytes or _file_sha256(pfad) != archiv.sha256):
            pfad.unlink()  # ein Rest, der nicht zum jetzigen Angebot gehoert
        if not pfad.is_file():
            self._hole_archiv(urllib.parse.urljoin(paket.basis_url, archiv.datei), archiv, pfad)
        self._docker_load(pfad)
        if not self._compose.has_image(archiv.ref):
            pfad.unlink(missing_ok=True)
            raise _Refused(MANIFEST_UNGUELTIG, f"Image {archiv.ref} fehlt nach dem Laden des Archivs")
        pfad.unlink()

    def _hole_archiv(self, url: str, archiv: bundles.Archiv, pfad: Path) -> None:
        teil = pfad.with_name(pfad.name + ".part")
        fortgesetzt = teil.exists()
        try:
            sha = self._fetch_datei(url, archiv.bytes, pfad, archiv_frist(archiv.bytes))
        except TooLarge:
            teil.unlink(missing_ok=True)
            pfad.unlink(missing_ok=True)
            raise _Refused(MANIFEST_UNGUELTIG, f"Archiv ist groesser als im Manifest ({archiv.bytes} Byte)") from None
        size = pfad.stat().st_size
        if size < archiv.bytes:  # Strom ohne Laengenangabe endete vor der Zeit: das Stueck ist der Anfang fuer den Rest
            os.replace(pfad, teil)
            raise FetchError(f"{url}: nur {size} von {archiv.bytes} Byte")
        if size != archiv.bytes or sha != archiv.sha256:
            pfad.unlink(missing_ok=True)
            if fortgesetzt:  # der Anfang stammte vielleicht aus einem anderen Angebot: neu laden statt ablehnen
                raise FetchError(f"{url}: Pruefsumme nach dem Fortsetzen falsch, der Abruf beginnt neu")
            raise _Refused(MANIFEST_UNGUELTIG, "SHA-256 oder Groesse des Archivs passt nicht")

    def _missing_devices(self, compose_text: str) -> list[str]:
        return [path for path in bundles.compose_devices(compose_text) if not self._path_exists(path)]

    def _reachable(self) -> bool:
        """Erreicht der Host den Broker? (Netz und DNS; das neue Bundle spielt dafuer keine Rolle.)"""
        return self._kanal.erreichbar()

    def _wait_healthy(self, version: str) -> str:
        """gesund | ungesund (Frist abgelaufen) | unerreichbar (Gesamtgrenze erreicht, Server weiter nicht erreichbar).
        Ein Takt, in dem der Server nicht erreichbar war, verschiebt die Frist um seine Dauer."""
        start = last = self._clock()
        deadline = start + self._health_seconds
        cap = start + max(HEALTH_CAP_SECONDS, self._health_seconds)
        while True:
            if self._health(version):
                return _HEALTHY
            reachable = self._reachable()
            now = self._clock()
            if reachable:
                if now >= deadline:
                    return _UNHEALTHY
            else:
                if now >= cap:
                    return _UNREACHABLE
                deadline = min(deadline + (now - last), cap)
            last = now
            self._sleep(HEALTH_POLL_SECONDS)

    # --- Meldungen ---

    def _queue(self, state: State, version: str, result: str, reason: str) -> None:
        """Meldung vormerken; der Aufrufer speichert den Zustand und ruft danach _flush."""
        entry = {"version": version, "result": result, "reason": reason}
        state.pending_reports = (state.pending_reports + [entry])[-MAX_PENDING_REPORTS:]

    def _flush(self, state: State) -> None:
        """Vorgemerkte Meldungen in Reihenfolge senden; beim ersten Fehler aufhoeren, der Rest bleibt fuer spaeter."""
        sent = 0
        for entry in state.pending_reports:
            try:
                self._kanal.update_result(entry["version"], entry["result"], entry["reason"])
            except kanal.KanalFehler as error:
                logger.warning("update_result fuer %s nicht gemeldet (%s), naechster Durchlauf", entry["version"],
                               error)
                break
            sent += 1
        if sent:
            state.pending_reports = state.pending_reports[sent:]
            state.save(self._state_path)

    def _installed_angebot(self, version: str) -> tuple[str | None, bytes | None]:
        """SHA-256 des Manifests und Signatur, mit denen das Bundle `version` installiert wurde (je None, wenn die Datei
        fehlt)."""
        ordner = self._store.dir(version)
        try:
            manifest = hashlib.sha256((ordner / bundles.MANIFEST_NAME).read_bytes()).hexdigest()
        except OSError:
            manifest = None
        try:
            signatur = (ordner / bundles.SIGNATURE_NAME).read_bytes()
        except OSError:
            signatur = None
        return manifest, signatur

    def _reject(self, state: State, version: str, reason: str, manifest_sha256: object, signatur: object) -> None:
        """Version samt Zertifikat und Manifest ablehnen und melden: nur dieses Angebot ist abgelehnt, eine mit einem
        anderen Zertifikat neu signierte Fassung oder ein anderes Manifest derselben Version wird wieder versucht."""
        state.rejected = (state.rejected + [_rejected_key(version, manifest_sha256, signatur)])[-MAX_REJECTED:]
        self._queue(state, version, "rollback", reason)
        state.save(self._state_path)
        self._flush(state)

    def _finish(self, state: State, version: str) -> None:
        progress = state.in_progress or {}
        previous = progress.get("previous") if state.in_progress else state.current
        state.zertifikat_untergrenze = _hoehere_untergrenze(state.zertifikat_untergrenze, progress.get("zertifikat_ab"))
        state.previous, state.current, state.in_progress = previous, version, None
        self._queue(state, version, "ok", "")
        state.save(self._state_path)
        keep: set[str] = set()
        for kept in (version, previous):
            if kept and self._store.exists(kept):
                keep |= set(bundles.compose_images((self._store.dir(kept) / "docker-compose.yml").read_text()))
        if keep:
            self._compose.prune(keep)
        self._flush(state)
        logger.info("Update auf %s abgeschlossen", version)

    def _rollback(self, state: State, version: str, reason: str, reject: bool = True) -> bool:
        """Zurueck auf das vorige Bundle. Scheitert `up`, bleibt in_progress (mit "rollback") erhalten und der
        naechste recover()/run_once wiederholt den Rueckweg; abgelehnt und gemeldet wird erst, wenn er gelang.
        reject=False (Server nur nicht erreichbar): gemeldet, aber nicht nach rejected."""
        progress = state.in_progress or {}
        previous = progress.get("previous") if state.in_progress else state.current
        if previous and self._store.exists(previous):
            try:
                self._compose.up(previous)
            except bundles.ComposeError as error:
                logger.error("Rueckweg auf %s gescheitert (%s), wird wiederholt", previous, error)
                since = progress.get("since")
                state.in_progress = {"version": version, "previous": previous,
                                     "since": since if isinstance(since, float) else time.time(), "rollback": reason}
                if not reject:
                    state.in_progress["reject"] = False
                state.save(self._state_path)
                return False
        else:
            logger.error("Kein Rueckweg moeglich fuer %s (voriges Bundle: %s), laufende Container bleiben unveraendert",
                         version, previous or "keines")
        state.current, state.in_progress = previous, None
        logger.error("Update auf %s zurueckgerollt (%s)", version, reason)
        if reject:
            self._reject(state, version, reason, *self._installed_angebot(version))
        else:
            self._queue(state, version, "rollback", reason)
            state.save(self._state_path)
            self._flush(state)
        return True


@dataclass(frozen=True)
class Einstellungen:
    """Eingaenge von `main`, ausser `SHG_ROOT`. Die Standardwerte sind die des Geraets (install.sh); der Dienst
    ruft `main` ohne Einstellungen, `allow_http` ist dort aus und aus der Umgebung nicht einzuschalten. Nur Tests reichen
    eigene Werte durch (`host/tests/updater_testlauf.py` fuer den Docker-Test; sie ist auf keinem Geraet installiert)."""
    release_roots: Path = Path("/opt/smartheat/host/release-roots.pem")
    allow_http: bool = False
    health_url: str = "http://127.0.0.1:8081/healthz"
    health_seconds: float = HEALTH_SECONDS
    compose_project: str = bundles.COMPOSE_PROJECT


def main(argv: list[str] | None = None, *, einstellungen: Einstellungen | None = None) -> None:
    logging.basicConfig(level=logging.INFO)
    args = sys.argv[1:] if argv is None else argv
    settings = Einstellungen() if einstellungen is None else einstellungen
    root = Path(os.environ.get("SHG_ROOT", "/var/lib/smartheat"))
    roots_text = settings.release_roots.read_text()

    store = bundles.BundleStore(root)
    compose = bundles.ComposeRunner(root, project=settings.compose_project)
    run = Updater(
        kanal=kanal.LokalerKanal(root / "data"), store=store, compose=compose,
        fetch=lambda url, limit: fetch_url(url, limit, settings.allow_http),
        fetch_datei=lambda url, limit, ziel, frist: fetch_url_to_file(url, limit, settings.allow_http, ziel, frist),
        roots_text=roots_text,
        uhr=lambda: zeit_aus_status(root / "host" / "status.json"),
        state_path=root / "updater" / "state.json", version=GATEWAY_VERSION,
        health=lambda version: health_ok(fetch_health(settings.health_url), compose.health(version)),
        allow_http=settings.allow_http, health_seconds=settings.health_seconds,
    )
    try:
        run.recover()
    except Exception:  # Fangnetz wie in der Schleife: kein Neustart-Kreis, der Durchlauf kommt trotzdem
        logger.exception("Wiederaufnahme beim Start mit unerwartetem Fehler abgebrochen")
    if "--once" in args:
        print(run.run_once())
        return
    time.sleep(FIRST_DELAY_SECONDS)
    while True:
        try:
            logger.info("Durchlauf: %s", run.run_once())
        except Exception:  # Fangnetz: ein unerwarteter Fehler beendet den Dienst nicht, der naechste Durchlauf folgt
            logger.exception("Durchlauf mit unerwartetem Fehler abgebrochen")
        time.sleep(PERIOD_SECONDS + random.uniform(0, JITTER_SECONDS))


if __name__ == "__main__":
    main()
