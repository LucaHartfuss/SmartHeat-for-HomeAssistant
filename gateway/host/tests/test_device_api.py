import base64
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fake_device_api import FakeDeviceApi

from smartheat_gateway.agent import identity
from smartheat_gateway.paths import Paths
from smartheat_host import device_api


@pytest.fixture
def api():
    server = FakeDeviceApi()
    server.start()
    yield server
    server.stop()


def _registered(tmp_path, api):
    """Wie der Agent: Identitaet anlegen und registrieren (der Updater legt selbst nie Schluessel an)."""
    from smartheat_gateway.agent.api_client import DeviceApiClient
    ident = identity.load_or_create(Paths(tmp_path))
    DeviceApiClient(api.url, ident).register("0.2.0", {"drivers": [], "zigbee": True})
    return ident


def test_desired_without_update_is_the_reported_version(tmp_path, api):
    _registered(tmp_path, api)
    client = device_api.DeviceApi(api.url, device_api.load_device_key(tmp_path / "device"))
    assert client.desired() == {"version": "0.2.0"}


def test_desired_with_update_and_result(tmp_path, api):
    ident = _registered(tmp_path, api)
    api.set_desired(version="0.3.0", manifest_url="https://example.test/m.json", manifest_sha256="0" * 64,
                    signature="test-sig")
    client = device_api.DeviceApi(api.url, device_api.load_device_key(tmp_path / "device"))
    assert client.desired()["manifest_url"] == "https://example.test/m.json"
    client.update_result("0.3.0", "rollback", "ungesund")
    assert api.update_results == [{"device_id": ident.device_id, "version": "0.3.0", "result": "rollback",
                                   "reason": "ungesund"}]


def test_device_id_matches_the_agent_identity(tmp_path, api):
    ident = _registered(tmp_path, api)
    client = device_api.DeviceApi(api.url, device_api.load_device_key(tmp_path / "device"))
    assert client.device_id == ident.device_id


def test_wrong_clock_is_not_authenticated(tmp_path, api):
    _registered(tmp_path, api)
    client = device_api.DeviceApi(api.url, device_api.load_device_key(tmp_path / "device"),
                                  now=lambda: 0.0)  # 1970: weit ausserhalb der erlaubten Abweichung
    with pytest.raises(device_api.NotAuthenticated):
        client.desired()


def test_blocked_device_is_not_authenticated(tmp_path, api):
    _registered(tmp_path, api)
    api.block()
    client = device_api.DeviceApi(api.url, device_api.load_device_key(tmp_path / "device"))
    with pytest.raises(device_api.NotAuthenticated):
        client.desired()


def test_missing_key_is_a_temporary_error(tmp_path):
    with pytest.raises(device_api.DeviceApiError, match="noch nicht registriert"):
        device_api.load_device_key(tmp_path / "device")


def test_missing_key_is_not_created(tmp_path):
    with pytest.raises(device_api.DeviceApiError):
        device_api.load_device_key(tmp_path / "device")
    assert not (tmp_path / "device").exists()


def test_garbage_key_is_a_temporary_error_without_content(tmp_path):
    (tmp_path / "device").mkdir()
    (tmp_path / "device" / "key.pem").write_text("test-geheim-kein-pem")
    with pytest.raises(device_api.DeviceApiError) as raised:
        device_api.load_device_key(tmp_path / "device")
    assert "test-geheim" not in str(raised.value)


def test_unreachable_server_is_a_temporary_error(tmp_path, api):
    _registered(tmp_path, api)
    client = device_api.DeviceApi("http://127.0.0.1:9", device_api.load_device_key(tmp_path / "device"), timeout=1)
    with pytest.raises(device_api.DeviceApiError):
        client.desired()


def test_unknown_device_is_not_authenticated(tmp_path, api):
    _registered(tmp_path, api)
    client = device_api.DeviceApi(api.url, device_api.load_device_key(tmp_path / "device"))
    api.devices.clear()
    with pytest.raises(device_api.NotAuthenticated):
        client.desired()


@pytest.mark.parametrize(("status", "body"), [(500, b"{}"), (200, b"kein json"), (200, b"[1]")])
def test_server_error_and_bad_answers_are_temporary_errors(tmp_path, api, status, body):
    _registered(tmp_path, api)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = device_api.DeviceApi(f"http://127.0.0.1:{server.server_address[1]}",
                                      device_api.load_device_key(tmp_path / "device"))
        with pytest.raises(device_api.DeviceApiError) as raised:
            client.desired()
        assert not isinstance(raised.value, device_api.NotAuthenticated)
    finally:
        server.shutdown()
        server.server_close()


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, response.read()


def _post(url, body):
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, response.read()


def test_fake_serves_files_with_slashes_and_404_for_unknown(api):
    api.files["0.9.1/manifest.json"] = b"{}"
    assert _get(f"{api.url}/_e2e/files/0.9.1/manifest.json") == (200, b"{}")
    with pytest.raises(urllib.error.HTTPError) as raised:
        _get(f"{api.url}/_e2e/files/0.9.1/fehlt")
    assert raised.value.code == 404


def test_fake_control_routes_for_desired_and_files(tmp_path, api):
    _registered(tmp_path, api)
    _post(f"{api.url}/_e2e/files", {"0.9.1/a.bin": base64.b64encode(b"\x00\x01").decode()})
    assert api.files == {"0.9.1/a.bin": b"\x00\x01"}
    _post(f"{api.url}/_e2e/desired", {"version": "0.9.1", "manifest_url": "http://x/m", "manifest_sha256": "1" * 64,
                                      "signature": "test-sig"})
    client = device_api.DeviceApi(api.url, device_api.load_device_key(tmp_path / "device"))
    assert client.desired()["version"] == "0.9.1"
    client.update_result("0.9.1", "ok", "")
    state = json.loads(_get(f"{api.url}/_e2e/state")[1])
    assert state["update_results"][0]["result"] == "ok"


def _raw_server(reply: bytes):
    """Stub-Server auf 127.0.0.1: liest die Anfrage und schickt rohe Bytes zurueck (kaputte Antworten)."""
    import socket
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def serve():
        try:
            connection, _ = listener.accept()
        except OSError:
            return
        with connection:
            connection.recv(65536)
            connection.sendall(reply)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return listener, thread


@pytest.mark.parametrize("reply", [
    b"das ist kein http\r\n\r\n",  # BadStatusLine
    b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n10\r\nabc",  # IncompleteRead
], ids=["garbage-status-line", "truncated-chunked-body"])
def test_malformed_http_is_a_temporary_error(tmp_path, api, reply):
    _registered(tmp_path, api)
    listener, thread = _raw_server(reply)
    try:
        client = device_api.DeviceApi(f"http://127.0.0.1:{listener.getsockname()[1]}",
                                      device_api.load_device_key(tmp_path / "device"), timeout=2)
        with pytest.raises(device_api.DeviceApiError) as raised:
            client.desired()
        assert not isinstance(raised.value, device_api.NotAuthenticated)
    finally:
        listener.close()
        thread.join(timeout=5)


def test_oversized_answer_is_a_temporary_error(tmp_path, api):
    from smartheat_gateway.agent import wire
    _registered(tmp_path, api)
    body = b'{"x":"' + b"a" * wire.MAX_BODY_BYTES + b'"}'
    listener, thread = _raw_server(
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
        + str(len(body)).encode() + b"\r\n\r\n" + body)
    try:
        client = device_api.DeviceApi(f"http://127.0.0.1:{listener.getsockname()[1]}",
                                      device_api.load_device_key(tmp_path / "device"), timeout=2)
        with pytest.raises(device_api.DeviceApiError, match="zu gross"):
            client.desired()
    finally:
        listener.close()
        thread.join(timeout=5)
