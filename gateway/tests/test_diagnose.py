"""Diagnoseseite auf dem Geraetekern - Zustand statt Uebernahmestatus, Hinweise, eigene Meldungen,
abgelehnte Konfiguration, Code nur solange nicht eingerichtet und nach einem Knopfdruck. Schutz gegen DNS-Rebinding und
fremde Clients. Die Seite verlangt eine Sitzung (Anmeldung: test_anmeldung.py); die Tests hier fragen mit einer
gueltigen Sitzung (COOKIE), der Gesundheitsserver braucht keine."""
import http.client
import json
import socket
import threading
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
from geraet_fakes import geraet_fuer

from smartheat_core import dateien
from smartheat_device import wire
from smartheat_device.host import Anzeige
from smartheat_gateway import diagnose as diagnostics
from smartheat_gateway.anmeldung import Zugang
from smartheat_gateway.diagnose import (
    ZUSTAND_TEXTE,
    host_allowed,
    parse_hostnames,
    render_html,
    seite,
    start_server,
)
from smartheat_gateway.paths import Paths

ZUGANG = Zugang(Path("/gibt/es/nicht/werk.json"))  # ohne Werk; die Sitzung genuegt fuer die Seite
COOKIE = f"shg_sitzung={ZUGANG.sitzungen.neu()}"
SNAPSHOT = {
    "geraet": "shg-abcdefghijklmnop", "zustand": "nicht_uebernommen", "eingerichtet": False,
    "versionen": {"gateway": "0.6.0"}, "server_ok": True, "laufzeit": None, "zigbee_stick": True, "host": {},
    "hinweise": [], "meldungen": [], "konfiguration_abgelehnt": None, "code": "ABCDEFGHJKLM",
}


def _anzeige(zustand="regelt", **felder) -> Anzeige:
    werte = {"zustand": zustand, "server_ok": True, "device_state": "uebernommen", "eingerichtet": True,
             "hinweise": (), "meldungen": (), "konfiguration_abgelehnt": None}
    return Anzeige(**{**werte, **felder})


@pytest.fixture
def geraet(data_dir, clock, tmp_path):
    from fake_z2m import FakeZigbee2Mqtt
    from fakes import FakeBus

    from smartheat_gateway.zigbee import ZigbeeMirror
    bus = FakeBus()
    FakeZigbee2Mqtt(bus).bridge(online=True)
    mirror = ZigbeeMirror(bus, clock)
    mirror.start()
    paths = Paths(data_dir)
    dateien.schreiben_json(paths.data.parent / "host" / "status.json",
               {"netz": True, "dns": True, "zeit_synchron": False, "wlan_passwort": "test-wlan-secret"}, modus=dateien.LESBAR)
    return geraet_fuer(paths, mirror, None)


def test_every_state_has_a_text():
    assert set(ZUSTAND_TEXTE) == set(wire.ZUSTAENDE)


def test_html_shows_device_id_and_code_only_while_not_set_up():
    page = render_html(SNAPSHOT)
    assert "ABCDEFGHJKLM" in page and "shg-abcdefghijklmnop" in page and "<script" not in page
    eingerichtet = render_html({**SNAPSHOT, "eingerichtet": True, "zustand": "regelt", "code": None})
    assert "ABCDEFGHJKLM" not in eingerichtet


def test_the_page_has_no_qr_code_and_no_portal_link_with_the_code():
    """GW 7.1: kein Portal-Link mit Code. Weder ein Link (`href`, `claim?`) noch ein QR-Bild (svg) enthaelt den Code,
    auch nicht bei offenem Fenster."""
    page = render_html(SNAPSHOT)
    assert "ABCDEFGHJKLM" in page  # als Text, zum Abtippen im Portal
    assert "claim?" not in page and "href" not in page and "<svg" not in page and "<img" not in page
    assert page.count("ABCDEFGHJKLM") == 1


@pytest.mark.parametrize("zustand", ["gesperrt", "update_noetig"])
def test_blocking_states_are_explained_on_the_page(zustand):
    page = render_html({**SNAPSHOT, "zustand": zustand, "code": None})
    assert ZUSTAND_TEXTE[zustand] in page


def test_page_lists_hints_own_messages_and_a_rejected_configuration():
    data = {**SNAPSHOT, "eingerichtet": True, "zustand": "regelt", "code": None,
            "hinweise": [{"key": "wartung", "stufe": "warnung", "text": "Wartung <heute>"}],
            "meldungen": [{"key": "wunsch_begrenzt", "text": "Wunsch auf 26 °C begrenzt"}],
            "konfiguration_abgelehnt": "Raumfühler ungültig"}
    page = render_html(data)
    assert "Wartung &lt;heute&gt;" in page and "Wunsch auf 26 °C begrenzt" in page
    assert "Raumfühler ungültig" in page and "<heute>" not in page


def test_healthz_and_page_over_http():
    server = start_server(0, lambda: SNAPSHOT, zugang=ZUGANG)
    port = server.server_address[1]

    def abrufen(pfad):
        return urllib.request.urlopen(
            urllib.request.Request(f"http://127.0.0.1:{port}{pfad}", headers={"Cookie": COOKIE}), timeout=5)

    try:
        health = json.load(abrufen("/healthz"))
        assert health == {"server_ok": True, "runtime_status": None, "zustand": "nicht_uebernommen"}
        response = abrufen("/")
        page = response.read().decode()
        assert "shg-abcdefghijklmnop" in page and 'http-equiv="refresh"' in page
        assert response.headers["Cache-Control"] == "no-store"
    finally:
        server.shutdown()
        server.server_close()


def test_the_code_is_on_the_page_only_while_not_claimed(geraet):
    """Auch bei offenem Code-Fenster (nach einem Knopfdruck) zeigt die Seite den Code nur an einem nicht uebernommenen
    Geraet; ohne Fenster nie (test_geraet_host_code.py)."""
    sicht = geraet.device
    geraet.code_fenster_bis = geraet._clock() + 100.0
    for anzeige, code in (
        (None, False),  # startet: noch keine Anzeige
        (_anzeige("nicht_uebernommen", device_state="nicht_uebernommen", eingerichtet=False), True),
        (_anzeige("keine_verbindung", server_ok=False, device_state="nicht_uebernommen", eingerichtet=False), True),
        (_anzeige("wartet_auf_einrichtung", eingerichtet=False), False),  # uebernommen: Code nutzlos (F1)
        (_anzeige("keine_verbindung", server_ok=False, eingerichtet=False), False),  # uebernommen
        (_anzeige("gesperrt", device_state="gesperrt", eingerichtet=False), False),
        (_anzeige("gesperrt", device_state="nicht_uebernommen", eingerichtet=False), False),  # Sperre nur im Speicher
        (_anzeige("startet", device_state="nicht_uebernommen", eingerichtet=False), False),
        (_anzeige("regelt"), False),
    ):
        geraet.letzte_anzeige = anzeige
        data = seite(geraet, sicht)
        assert (data["code"] == sicht.identity.claim_code) is code, anzeige
        if not code:
            assert sicht.identity.claim_code not in render_html(data)


def test_without_an_open_window_the_page_shows_neither_code_nor_device_id(geraet):
    geraet.letzte_anzeige = _anzeige("nicht_uebernommen", device_state="nicht_uebernommen", eingerichtet=False)
    geraet.code_fenster_bis = None
    data = seite(geraet, geraet.device)
    assert data["code"] is None and data["geraet"] is None
    page = render_html(data)
    assert geraet.device.identity.claim_code not in page and geraet.device.identity.device_id not in page
    geraet.code_fenster_bis = geraet._clock() - 0.001  # abgelaufen
    assert seite(geraet, geraet.device)["code"] is None


def test_the_page_extract_takes_only_known_host_fields(geraet):
    geraet.letzte_anzeige = _anzeige()
    data = seite(geraet, geraet.device)
    assert data["host"] == {"netz": True, "dns": True, "zeit_synchron": False}
    assert "test-wlan-secret" not in json.dumps(data)


def test_without_a_device_the_page_still_renders(geraet):
    geraet.device = None
    data = seite(geraet, None)
    assert data["zustand"] == "startet" and data["code"] is None
    assert "SmartHeat-Gateway" in render_html(data)


def _get(port: int, path: str, host: str) -> tuple[int, bytes, http.client.HTTPMessage]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.putrequest("GET", path, skip_host=True)
        connection.putheader("Host", host)
        connection.putheader("Cookie", COOKIE)
        connection.endheaders()
        response = connection.getresponse()
        return response.status, response.read(), response.headers
    finally:
        connection.close()


@pytest.mark.parametrize("address, allowed", [
    ("127.0.0.1", True), ("192.168.178.20", True), ("10.0.0.5", True), ("fd12:3456::1", True), ("fe80::1", True),
    ("::ffff:192.168.1.5", True), ("::1", True),
    ("8.8.8.8", False), ("2a00:1450:4001::1", False), ("::ffff:8.8.8.8", False), ("kaputt", False),
])
def test_only_local_clients_may_see_the_page(address, allowed):
    # Nur lokale Clients sehen die Seite (Nutzer-Entscheidung).
    assert diagnostics.client_allowed(address) is allowed


def test_docker_bridge_and_zone_scoped_clients_are_local():
    # Der veroeffentlichte Docker-Port zeigt dem Handler je nach Pfad die Bridge-Adresse (172.16/12) oder den Link-Local mit Zone.
    assert diagnostics.client_allowed("172.17.0.1") and diagnostics.client_allowed("172.31.255.254")
    assert diagnostics.client_allowed("fe80::1%eth0")
    assert not diagnostics.client_allowed("172.32.0.1")


def test_foreign_client_gets_403_without_content_on_every_route(monkeypatch):
    monkeypatch.setattr(diagnostics, "client_allowed", lambda address: False)
    server = start_server(0, lambda: {"server_ok": True, "laufzeit": "regelt", "uebernommen": False, "code": "ABCDEFGHJKLM"},
                          zugang=ZUGANG)
    port = server.server_address[1]
    try:
        for path in ("/", "/healthz", "/unbekannt"):
            status, body, _ = _get(port, path, f"127.0.0.1:{port}")
            assert (status, body) == (403, b"")
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("path", ["/", "/healthz"])
def test_dns_rebinding_host_is_refused_on_every_route(path):
    server = start_server(0, lambda: SNAPSHOT, parse_hostnames("shg.lan"), zugang=ZUGANG)
    port = server.server_address[1]
    try:
        status, body, headers = _get(port, path, f"evil.example:{port}")
        assert status == 403 and body == b"" and headers["Cache-Control"] == "no-store"
        assert b"ABCDEFGHJKLM" not in body
        for host in (f"192.168.2.40:{port}", f"[::1]:{port}", "localhost", f"SHG.lan:{port}"):
            status, body, _ = _get(port, path, host)
            assert status == 200, host
        assert path != "/" or b"ABCDEFGHJKLM" in _get(port, "/", "192.168.2.40")[1]
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(("header", "allowed"), [
    ("192.168.2.40", True), ("192.168.2.40:80", True), ("[fe80::1]:8080", True), ("[::1]", True),
    ("localhost:8080", True), ("shg.lan", True), ("SHG.LAN.", True),
    ("evil.example", False), ("evil.example:80", False), ("192.168.2.40.evil.example", False),
    ("[evil.example]:80", False), ("[::1]x", False), ("::1", False), ("192.168.2.40:abc", False), ("", False),
    (None, False),
])
def test_host_allowed(header, allowed):
    assert host_allowed(header, parse_hostnames(" shg.lan , ,")) is allowed


# --- Angreifer im LAN: nur lesend, kein Zustand aendert sich, langsame und viele Verbindungen blockieren nicht ---


@pytest.fixture(scope="module")
def _lan_server_module():
    """Ein Server fuer alle Tests dieses Abschnitts (das Binden dauert wegen getfqdn rund 1,4 s)."""
    calls: list[int] = []

    def snapshot() -> dict:
        calls.append(1)
        return SNAPSHOT

    server = start_server(0, snapshot, zugang=ZUGANG)
    try:
        yield SimpleNamespace(port=server.server_address[1], calls=calls)
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def lan_server(_lan_server_module):
    """Echter Server auf 127.0.0.1; `calls` zaehlt, wie oft der Zustand der Seite gelesen wurde (nur GET auf / und
    /healthz); `sockets` sind offene Verbindungen des Tests, die danach geschlossen werden."""
    _lan_server_module.calls.clear()
    sockets: list[socket.socket] = []
    try:
        yield SimpleNamespace(port=_lan_server_module.port, calls=_lan_server_module.calls, sockets=sockets)
    finally:
        for offen in sockets:
            offen.close()


def _anfrage(port: int, method: str, path: str, body: bytes | None = None, timeout: float = 5) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        kopf = {"Cookie": COOKIE, **({"Content-Type": "application/json"} if body else {})}
        connection.request(method, path, body=body, headers=kopf)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


@pytest.mark.parametrize("path", ["/", "/healthz"])
@pytest.mark.parametrize(("method", "body"), [
    ("POST", b'{"zustand": "regelt"}'), ("PUT", b'{"zustand": "regelt"}'), ("DELETE", None),
    ("PATCH", b'{"zustand": "regelt"}'), ("HEAD", None), ("OPTIONS", None)])
def test_other_methods_than_get_are_not_executed(lan_server, method, body, path):
    """POST, PUT, DELETE, PATCH, HEAD und OPTIONS: 501 ohne Zustandslesen und ohne den Uebernahme-Code; derselbe Pfad
    mit GET antwortet 200 und liest den Zustand (Gegenprobe: vorher war nichts gelesen)."""
    assert lan_server.calls == []
    status, antwort = _anfrage(lan_server.port, method, path, body)
    assert status == 501
    assert b"ABCDEFGHJKLM" not in antwort and b"shg-abcdefghijklmnop" not in antwort
    assert lan_server.calls == []
    assert _anfrage(lan_server.port, "GET", path)[0] == 200
    assert lan_server.calls == [1]


@pytest.mark.parametrize("path", ["/x", "/healthz/", "/index.html", "/admin", "/HEALTHZ", "/../etc/passwd",
                                  "/%2e%2e/etc/passwd"])
def test_unknown_paths_answer_404_without_content_and_without_reading_the_state(lan_server, path):
    assert _anfrage(lan_server.port, "GET", path) == (404, b"")
    assert lan_server.calls == []
    assert _anfrage(lan_server.port, "GET", "/healthz")[0] == 200
    assert lan_server.calls == [1]


def test_a_silent_or_half_sent_connection_does_not_block_a_second_request(lan_server):
    """Ein Client, der nichts sendet, und einer, der die Anfrage nie beendet: die naechste Anfrage wird trotzdem
    beantwortet (Frist 2 s; ein Server mit nur einem Faden bliebe am ersten Client haengen)."""
    lan_server.sockets.append(socket.create_connection(("127.0.0.1", lan_server.port), timeout=5))
    halb = socket.create_connection(("127.0.0.1", lan_server.port), timeout=5)
    lan_server.sockets.append(halb)
    halb.sendall(f"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\nCookie: {COOKIE}\r\n".encode())  # kein Zeilenende am Kopf: nie fertig
    beginn = time.monotonic()
    status, antwort = _anfrage(lan_server.port, "GET", "/healthz", timeout=2)
    assert status == 200 and json.loads(antwort)["zustand"] == "nicht_uebernommen"
    assert time.monotonic() - beginn < 2
    halb.sendall(b"\r\n")  # der langsame Client darf seine Anfrage beenden und wird ebenfalls beantwortet
    assert halb.recv(12).startswith(b"HTTP/1.0 200")


def test_fifty_silent_connections_leave_healthz_answering(lan_server):
    """Eine Flut von 50 gleichzeitigen Verbindungen ohne Anfrage haelt den Server nicht von /healthz ab."""
    for _ in range(50):
        lan_server.sockets.append(socket.create_connection(("127.0.0.1", lan_server.port), timeout=5))
    beginn = time.monotonic()
    status, antwort = _anfrage(lan_server.port, "GET", "/healthz", timeout=2)
    assert status == 200 and json.loads(antwort)["server_ok"] is True
    assert time.monotonic() - beginn < 2


def test_fifty_simultaneous_requests_are_all_answered():
    """50 Anfragen auf /healthz, deren Zustandslesen erst endet, wenn alle 50 zugleich im Handler sind (Barriere): ein
    Server mit nur einem Faden lieferte nie alle 50 und scheitert an der Frist; alle bekommen 200."""
    barriere = threading.Barrier(50, timeout=10)

    def snapshot() -> dict:
        barriere.wait()
        return SNAPSHOT

    server = start_server(0, snapshot, zugang=ZUGANG)
    port = server.server_address[1]
    ergebnisse: list[int] = []

    def frage() -> None:
        try:
            ergebnisse.append(_anfrage(port, "GET", "/healthz", timeout=15)[0])
        except (OSError, http.client.HTTPException):
            ergebnisse.append(-1)

    faeden = [threading.Thread(target=frage) for _ in range(50)]
    try:
        for faden in faeden:
            faden.start()
        for faden in faeden:
            faden.join(timeout=30)
        assert ergebnisse == [200] * 50
    finally:
        barriere.abort()
        server.shutdown()
        server.server_close()


# --- Obergrenzen: Verbindungsfrist und gleichzeitige Verbindungen (BR-GW-3) ---


@pytest.fixture
def server_mit_grenzen(monkeypatch):
    """Fabrik: Server auf 127.0.0.1 mit eigenem Port, kleiner Verbindungsfrist und kleiner Obergrenze, damit die Tests
    nicht Sekunden warten. `nur_health` startet den Gesundheitsserver."""
    gestartet: list = []
    sockets: list[socket.socket] = []

    def starten(frist: float, maximum: int, *, nur_health: bool = False):
        monkeypatch.setattr(diagnostics, "VERBINDUNG_FRIST_SECONDS", frist)
        monkeypatch.setattr(diagnostics, "MAX_VERBINDUNGEN", maximum)
        server = start_server(0, lambda: SNAPSHOT, nur_health=nur_health, zugang=None if nur_health else ZUGANG)
        gestartet.append(server)
        return SimpleNamespace(port=server.server_address[1], sockets=sockets)

    try:
        yield starten
    finally:
        for offen in sockets:
            offen.close()
        for server in gestartet:
            server.shutdown()
            server.server_close()


@pytest.fixture
def kurze_grenzen(server_mit_grenzen):
    return server_mit_grenzen(0.5, 3)


def _geschlossen(verbindung: socket.socket, frist: float) -> bool:
    """Wahr, wenn der Server die Verbindung binnen `frist` schliesst (recv liefert b'')."""
    verbindung.settimeout(frist)
    try:
        return verbindung.recv(1) == b""
    except TimeoutError:
        return False
    except ConnectionError:
        return True


def test_a_silent_connection_is_closed_after_the_connection_deadline(kurze_grenzen):
    stumm = socket.create_connection(("127.0.0.1", kurze_grenzen.port), timeout=5)
    kurze_grenzen.sockets.append(stumm)
    assert _geschlossen(stumm, 3)


def test_a_connection_that_drips_the_header_is_closed_after_the_deadline_too(kurze_grenzen):
    """Ein Byte alle 0,2 s hielte jede Leseoperation einzeln unter der Frist: sie gilt fuer die ganze Verbindung."""
    tropf = socket.create_connection(("127.0.0.1", kurze_grenzen.port), timeout=5)
    kurze_grenzen.sockets.append(tropf)
    beginn = time.monotonic()
    try:
        for byte in b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Langsam: " + b"a" * 100:
            tropf.sendall(bytes([byte]))
            time.sleep(0.2)
            if time.monotonic() - beginn > 4:
                break
    except OSError:
        pass  # der Server hat geschlossen
    assert time.monotonic() - beginn < 3


def test_connections_over_the_limit_are_closed_at_once_and_a_freed_slot_serves_again(server_mit_grenzen):
    server = server_mit_grenzen(2.0, 3)  # grosse Frist: der Test haengt nicht an der Geschwindigkeit des Runners
    belegt = [socket.create_connection(("127.0.0.1", server.port), timeout=5) for _ in range(3)]
    server.sockets.extend(belegt)
    time.sleep(0.1)  # die drei Verbindungen sind im Server angenommen
    zuviel = socket.create_connection(("127.0.0.1", server.port), timeout=5)
    server.sockets.append(zuviel)
    assert _geschlossen(zuviel, 1.0)  # vor Ablauf der Frist von 2 s der belegten Verbindungen
    # Gegenprobe: nach Ablauf der Frist sind die Plaetze frei, eine Anfrage wird beantwortet
    assert all(_geschlossen(verbindung, 5) for verbindung in belegt)
    assert _anfrage(server.port, "GET", "/healthz")[0] == 200


def test_the_health_server_answers_while_every_place_of_the_diagnostic_page_is_taken(server_mit_grenzen):
    """S-10: ein Gast im LAN fuellt die Plaetze der Diagnoseseite (Port 80). Compose-Healthcheck und Updater fragen den
    Gesundheitsserver mit eigenen Plaetzen (nur auf dem Loopback des Hosts veroeffentlicht) und bekommen weiter 200."""
    diagnose = server_mit_grenzen(5.0, 3)
    health = server_mit_grenzen(5.0, 3, nur_health=True)
    belegt = [socket.create_connection(("127.0.0.1", diagnose.port), timeout=5) for _ in range(3)]
    diagnose.sockets.extend(belegt)
    time.sleep(0.1)
    zuviel = socket.create_connection(("127.0.0.1", diagnose.port), timeout=5)
    diagnose.sockets.append(zuviel)
    assert _geschlossen(zuviel, 1.0)  # die Plaetze der Diagnoseseite sind erschoepft (Gegenprobe)
    status, antwort = _anfrage(health.port, "GET", "/healthz", timeout=2)
    assert status == 200 and json.loads(antwort)["server_ok"] is True


@pytest.mark.parametrize("path", ["/", "/x", "/index.html"])
def test_the_health_server_serves_nothing_but_healthz(server_mit_grenzen, path):
    health = server_mit_grenzen(5.0, 3, nur_health=True)
    assert _anfrage(health.port, "GET", path) == (404, b"")


def test_code_und_geraete_id_kommen_nie_ohne_sitzung(geraet):
    """S-2, S-13: auch bei offenem Code-Fenster an einem nicht uebernommenen Geraet liefert der Server Code und
    Geraete-ID nur mit Sitzung - nie auf der Anmeldeseite, nie in einer Weiterleitung, nie auf /healthz."""
    geraet.letzte_anzeige = _anzeige("nicht_uebernommen", device_state="nicht_uebernommen", eingerichtet=False)
    geraet.code_fenster_bis = geraet._clock() + 100.0
    code, kennung = geraet.device.identity.claim_code, geraet.device.identity.device_id
    assert seite(geraet, geraet.device)["code"] == code  # das Fenster ist offen
    server = start_server(0, lambda: seite(geraet, geraet.device), zugang=ZUGANG)
    port = server.server_address[1]
    try:
        for pfad in ("/", "/healthz", "/anmelden", "/x"):
            status, koerper, kopf = _anfrage_ohne_sitzung(port, pfad)
            text = koerper + str(kopf).encode()
            assert code.encode() not in text and kennung.encode() not in text, pfad
        assert _anfrage_ohne_sitzung(port, "/")[0] == 303
        mit = _anfrage(port, "GET", "/")
        assert mit[0] == 200 and code.encode() in mit[1] and kennung.encode() in mit[1]
    finally:
        server.shutdown()
        server.server_close()


def _anfrage_ohne_sitzung(port: int, path: str) -> tuple[int, bytes, http.client.HTTPMessage]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read(), response.headers
    finally:
        connection.close()
