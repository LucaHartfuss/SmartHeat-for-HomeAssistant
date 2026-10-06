import http.client
import json
import urllib.request
from types import SimpleNamespace

import pytest
from configs import apply_config, write_runtime_files
from fake_z2m import FakeZigbee2Mqtt
from fakes import FakeBus

from smartheat_gateway.agent import identity
from smartheat_gateway.agent.context import AgentContext
from smartheat_gateway.agent.diagnostics import (
    command_snapshot,
    host_allowed,
    parse_hostnames,
    render_html,
    snapshot,
    start_server,
)
from smartheat_gateway.files import write_json
from smartheat_gateway.paths import Paths
from smartheat_gateway.zigbee import ZigbeeMirror

SNAPSHOT = {
    "geraet": "shg-abcdefghijklmnop", "uebernommen": False, "versionen": {"gateway": "0.1.0"},
    "server_ok": True, "laufzeit": None, "zigbee_stick": True, "host": {}, "code": "ABCDEFGHJKLM",
}


def test_html_shows_the_qr_only_while_unclaimed():
    html = render_html(SNAPSHOT, "https://portal.example.test/#/claim?d=shg-abcdefghijklmnop&c=ABCDEFGHJKLM")
    assert "<svg" in html and "ABCDEFGHJKLM" in html and "<script" not in html
    claimed = render_html({**SNAPSHOT, "uebernommen": True}, None)
    assert "<svg" not in claimed and "ABCDEFGHJKLM" not in claimed


def test_html_without_portal_url_shows_the_code_without_qr():
    html = render_html(SNAPSHOT, None)
    assert "ABCDEFGHJKLM" in html and "<svg" not in html


def test_healthz_and_page_over_http():
    server = start_server(0, lambda: SNAPSHOT, lambda: None)
    port = server.server_address[1]
    try:
        health = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5))
        assert health == {"server_ok": True, "runtime_status": None}
        response = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5)
        page = response.read().decode()
        assert "shg-abcdefghijklmnop" in page and 'http-equiv="refresh"' in page
        assert response.headers["Cache-Control"] == "no-store"
    finally:
        server.shutdown()
        server.server_close()


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


def test_claim_code_only_while_the_server_says_unclaimed(ctx):
    context, host = ctx
    loop = SimpleNamespace(server_ok=False)
    for state, uebernommen in ((None, None), ("uebernommen", True), ("gesperrt", False)):
        context.device_state = state
        data = snapshot(context, loop, host)
        assert data["code"] is None and data["uebernommen"] is uebernommen, state
        page = render_html(data, "https://portal.example.test/#/claim")
        assert context.identity.claim_code not in page and "<svg" not in page, state
    context.device_state = "nicht_uebernommen"
    assert context.identity.claim_code in render_html(snapshot(context, loop, host), None)


@pytest.fixture
def ctx(data_dir, clock, tmp_path):
    bus = FakeBus()
    FakeZigbee2Mqtt(bus).bridge(online=True)
    mirror = ZigbeeMirror(bus, clock)
    mirror.start()
    paths = Paths(data_dir)
    context = AgentContext(paths, bus, mirror, identity.load_or_create(paths), clock=clock)
    context.start()
    write_runtime_files(paths, apply_config())
    host = tmp_path / "host" / "status.json"
    write_json(host, {"netz": True, "dns": True, "zeit_synchron": False, "wlan_passwort": "test-wlan-secret"})
    return context, host


def test_snapshot_for_the_local_page_carries_the_code(ctx):
    context, host = ctx
    context.device_state = "nicht_uebernommen"
    data = snapshot(context, SimpleNamespace(server_ok=True), host)
    assert data["code"] == context.identity.claim_code
    assert data["host"] == {"netz": True, "dns": True, "zeit_synchron": False}
    assert data["zigbee_stick"] is True and data["uebernommen"] is False


def test_command_snapshot_never_contains_the_code_or_secrets(ctx):
    context, host = ctx
    context.device_state = "nicht_uebernommen"
    data = command_snapshot(context, SimpleNamespace(server_ok=False), host)
    text = json.dumps(data)
    assert "code" not in data and context.identity.claim_code not in text
    for secret in ("test-password", "test-token", "test-secret", "test-wlan-secret"):
        assert secret not in text
    assert data["geraet"] == context.identity.device_id and data["server_ok"] is False


def test_broken_host_status_counts_as_empty(ctx):
    context, host = ctx
    host.write_text("{kaputt")
    assert command_snapshot(context, SimpleNamespace(server_ok=True), host)["host"] == {}
