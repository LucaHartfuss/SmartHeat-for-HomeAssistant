"""Plan 5b D2 Task 6: Diagnoseseite auf dem Geraetekern - Zustand statt Uebernahmestatus, Hinweise, eigene Meldungen,
abgelehnte Konfiguration, QR-Code nur solange nicht eingerichtet. Schutz gegen DNS-Rebinding und fremde Clients wie
bis 0.5.0 (Tests unten unveraendert portiert)."""
import http.client
import json
import socket
import threading
import time
import urllib.request
from types import SimpleNamespace

import pytest
from geraet_fakes import geraet_fuer

from smartheat_core import dateien
from smartheat_device import wire
from smartheat_device.host import Anzeige
from smartheat_gateway import diagnose as diagnostics
from smartheat_gateway.diagnose import (
    ZUSTAND_TEXTE,
    host_allowed,
    parse_hostnames,
    qr_url,
    render_html,
    seite,
    start_server,
)
from smartheat_gateway.paths import Paths

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


def test_html_shows_the_qr_only_while_not_set_up():
    page = render_html(SNAPSHOT, "https://portal.example.test/#/claim?d=shg-abcdefghijklmnop&c=ABCDEFGHJKLM")
    assert "<svg" in page and "ABCDEFGHJKLM" in page and "<script" not in page
    eingerichtet = render_html({**SNAPSHOT, "eingerichtet": True, "zustand": "regelt", "code": None}, None)
    assert "<svg" not in eingerichtet and "ABCDEFGHJKLM" not in eingerichtet


def test_html_without_portal_url_shows_the_code_without_qr():
    page = render_html(SNAPSHOT, None)
    assert "ABCDEFGHJKLM" in page and "<svg" not in page


@pytest.mark.parametrize("zustand", ["gesperrt", "update_noetig"])
def test_blocking_states_are_explained_on_the_page(zustand):
    page = render_html({**SNAPSHOT, "zustand": zustand, "code": None}, None)
    assert ZUSTAND_TEXTE[zustand] in page


def test_page_lists_hints_own_messages_and_a_rejected_configuration():
    data = {**SNAPSHOT, "eingerichtet": True, "zustand": "regelt", "code": None,
            "hinweise": [{"key": "wartung", "stufe": "warnung", "text": "Wartung <heute>"}],
            "meldungen": [{"key": "wunsch_begrenzt", "text": "Wunsch auf 26 °C begrenzt"}],
            "konfiguration_abgelehnt": "Raumfühler ungültig"}
    page = render_html(data, None)
    assert "Wartung &lt;heute&gt;" in page and "Wunsch auf 26 °C begrenzt" in page
    assert "Raumfühler ungültig" in page and "<heute>" not in page


def test_healthz_and_page_over_http():
    server = start_server(0, lambda: SNAPSHOT, lambda: None)
    port = server.server_address[1]
    try:
        health = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5))
        assert health == {"server_ok": True, "runtime_status": None, "zustand": "nicht_uebernommen"}
        response = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5)
        page = response.read().decode()
        assert "shg-abcdefghijklmnop" in page and 'http-equiv="refresh"' in page
        assert response.headers["Cache-Control"] == "no-store"
    finally:
        server.shutdown()
        server.server_close()


def test_the_code_is_on_the_page_only_while_not_claimed(geraet):
    sicht = geraet.device
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
            assert sicht.identity.claim_code not in render_html(data, "https://portal.example.test/#/claim")


def test_the_page_extract_takes_only_known_host_fields(geraet):
    geraet.letzte_anzeige = _anzeige()
    data = seite(geraet, geraet.device)
    assert data["host"] == {"netz": True, "dns": True, "zeit_synchron": False}
    assert "test-wlan-secret" not in json.dumps(data)


def test_qr_url_needs_a_portal_and_escapes_the_parts():
    sicht = SimpleNamespace(identity=SimpleNamespace(device_id="shg-aaaaaaaaaaaaaaaa", claim_code="AB&CD"))
    assert qr_url("", sicht) is None
    assert qr_url("https://portal.example.test/", sicht) == (
        "https://portal.example.test/#/claim?d=shg-aaaaaaaaaaaaaaaa&c=AB%26CD")


def test_without_a_device_the_page_still_renders(geraet):
    geraet.device = None
    data = seite(geraet, None)
    assert data["zustand"] == "startet" and data["code"] is None
    assert "SmartHeat-Gateway" in render_html(data, None)


def _get(port: int, path: str, host: str) -> tuple[int, bytes, http.client.HTTPMessage]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.putrequest("GET", path, skip_host=True)
        connection.putheader("Host", host)
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
    # Audit 4, A4-37 (GW-10), Nutzer-Entscheidung E12; Review Focus 5
    assert diagnostics.client_allowed(address) is allowed


def test_docker_bridge_and_zone_scoped_clients_are_local():
    # Der veroeffentlichte Docker-Port zeigt dem Handler je nach Pfad die Bridge-Adresse (172.16/12) oder den Link-Local mit Zone.
    assert diagnostics.client_allowed("172.17.0.1") and diagnostics.client_allowed("172.31.255.254")
    assert diagnostics.client_allowed("fe80::1%eth0")
    assert not diagnostics.client_allowed("172.32.0.1")


def test_foreign_client_gets_403_without_content_on_every_route(monkeypatch):
    monkeypatch.setattr(diagnostics, "client_allowed", lambda address: False)
    server = start_server(0, lambda: {"server_ok": True, "laufzeit": "regelt", "uebernommen": False, "code": "ABCDEFGHJKLM"},
                          lambda: None)
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
    server = start_server(0, lambda: SNAPSHOT, lambda: "https://portal.example.test/#/claim", parse_hostnames("shg.lan"))
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

    server = start_server(0, snapshot, lambda: "https://portal.example.test/#/claim")
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
        connection.request(method, path, body=body, headers={"Content-Type": "application/json"} if body else {})
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
    halb.sendall(b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\n")  # kein Zeilenende am Kopf: nie fertig
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


def test_fifty_simultaneous_requests_are_all_answered(lan_server):
    ergebnisse: list[int] = []

    def frage() -> None:
        ergebnisse.append(_anfrage(lan_server.port, "GET", "/healthz", timeout=10)[0])

    faeden = [threading.Thread(target=frage) for _ in range(50)]
    for faden in faeden:
        faden.start()
    for faden in faeden:
        faden.join(timeout=15)
    assert ergebnisse == [200] * 50
    assert len(lan_server.calls) == 50
