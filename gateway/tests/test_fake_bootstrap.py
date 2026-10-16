"""Plan 5b D2 Task 10: Fake-Bootstrap fuer die Docker-Pruefungen (signiert CSRs, nennt den Fake-Broker)."""
import json
import subprocess
import urllib.error
import urllib.request

import pytest
from cryptography import x509
from cryptography.x509.oid import NameOID
from fake_bootstrap import FakeBootstrap

from smartheat_device import bootstrap, identity, wire


@pytest.fixture
def ca_dir(tmp_path):
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                    "-keyout", str(tmp_path / "ca.key"), "-out", str(tmp_path / "ca.pem"), "-days", "2",
                    "-subj", "/CN=fake-ca"], check=True, capture_output=True)
    return tmp_path


@pytest.fixture
def fake(ca_dir):
    server = FakeBootstrap(ca_dir, "fake-broker")
    server.start()
    yield server
    server.stop()


def _post(url: str, body: dict) -> dict:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read())


def test_register_signs_the_csr_and_names_the_broker(fake, tmp_path, ca_dir):
    ident = identity.load_or_create(tmp_path / "device")
    body = identity.register_body(ident, version="0.6.0", host="gateway", capabilities={"drivers": [], "zigbee": True})
    answer = _post(f"{fake.url}/devices/register", body)
    assert set(answer) == set(wire.BOOTSTRAP_RESPONSE)  # genau der Vertrag, kein poll_after
    assert answer["device_state"] == "uebernommen" and answer["mqtt_endpoint"] == "fake-broker"
    assert answer["mqtt_port"] == 8883 and answer["mqtt_ca"] == (ca_dir / "ca.pem").read_text()
    cert = x509.load_pem_x509_certificate(answer["certificate"].encode())
    assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == ident.device_id
    assert identity.passt_zum_schluessel(ident, answer["certificate"])
    assert fake.device_ids == [ident.device_id] and fake.registrations == 1


def test_the_d1_bootstrap_client_accepts_the_answers(fake, tmp_path):
    ident = identity.load_or_create(tmp_path / "device")
    client = bootstrap.BootstrapClient(fake.url, ident)
    antwort = client.register(version="0.6.0", host="gateway", capabilities={"drivers": [], "zigbee": True})
    assert antwort.device_state == "uebernommen" and antwort.zugang.endpoint.host == "fake-broker"
    assert antwort.zugang.endpoint.port == 8883 and antwort.zugang.endpoint.alpn is None
    assert client.certificate().device_state == "uebernommen"
    assert (fake.registrations, fake.certificates) == (1, 1)


def test_certificate_route_and_controls(fake, tmp_path):
    ident = identity.load_or_create(tmp_path / "device")
    _post(f"{fake.url}/_e2e/device_state", {"device_state": "nicht_uebernommen"})
    answer = _post(f"{fake.url}/devices/{ident.device_id}/certificate", {"csr": identity.csr_pem(ident)})
    assert answer["device_state"] == "nicht_uebernommen"
    _post(f"{fake.url}/_e2e/files", {"0.9.1/manifest.json": "eyJ4IjogMX0="})
    with urllib.request.urlopen(f"{fake.url}/_e2e/files/0.9.1/manifest.json", timeout=5) as response:
        assert json.loads(response.read()) == {"x": 1}
    with urllib.request.urlopen(f"{fake.url}/_e2e/state", timeout=5) as response:
        assert json.loads(response.read())["certificates"] == 1


def test_a_request_without_csr_is_400(fake):
    with pytest.raises(urllib.error.HTTPError) as error:
        _post(f"{fake.url}/devices/register", {"device_id": "x"})
    assert error.value.code == 400


def test_a_broken_csr_and_an_unknown_device_state_are_400(fake):
    with pytest.raises(urllib.error.HTTPError) as error:
        _post(f"{fake.url}/devices/register", {"csr": "kein CSR"})
    assert error.value.code == 400
    with pytest.raises(urllib.error.HTTPError) as error:
        _post(f"{fake.url}/_e2e/device_state", {"device_state": "irgendwas"})
    assert error.value.code == 400
    assert fake.device_state == "uebernommen" and fake.registrations == 0


def test_the_certificate_route_refuses_a_csr_for_another_device(fake, tmp_path):
    ident = identity.load_or_create(tmp_path / "device")
    other = identity.load_or_create(tmp_path / "other")
    with pytest.raises(urllib.error.HTTPError) as error:
        _post(f"{fake.url}/devices/{other.device_id}/certificate", {"csr": identity.csr_pem(ident)})
    assert error.value.code == 403
    assert fake.certificates == 0 and fake.device_ids == []
