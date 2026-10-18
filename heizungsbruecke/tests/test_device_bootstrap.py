"""Spec 5b 1: HTTPS-Bootstrap mit CSR, Rettungsweg, "gesperrt" nur bei 401 auf beides und synchroner Uhr."""
import base64
import json

import pytest
import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from device_helpers import GeraeteCa, csr_passt, zertifikat_fuer

from smartheat_device import bootstrap, identity, nachrichten, wire

NOW = 1_800_000_000
BASE = "https://accounts.example.test"
CAPS = {"drivers": ["simulation"], "zigbee": True}
ARGS = {"version": "0.6.0", "host": "gateway", "capabilities": CAPS}


class FakeResponse:
    def __init__(self, status: int, body=None) -> None:
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("kein JSON")
        return self._body


class FakeSession:
    def __init__(self, *answers) -> None:
        self.answers, self.calls = list(answers), []

    def request(self, method, url, data=None, headers=None, timeout=None):
        self.calls.append((method, url, data, headers, timeout))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def ident(tmp_path):
    return identity.load_or_create(tmp_path / "device")


@pytest.fixture
def ca():
    return GeraeteCa()


def _body(ident, ca, *, state="nicht_uebernommen", port=8883, alpn=None, cert=None):
    body = {"device_state": state, "poll_after": 60, "certificate": cert or zertifikat_fuer(ident, ca),
            "mqtt_endpoint": "mqtt.e2e.test", "mqtt_port": port, "mqtt_ca": ca.pem}
    if alpn:
        body["mqtt_alpn"] = alpn
    return body


def _client(ident, session):
    return bootstrap.BootstrapClient(BASE, ident, session=session, now=lambda: NOW)


def test_register_sends_a_signed_body_with_csr(ident, ca):
    session = FakeSession(FakeResponse(200, _body(ident, ca)))
    antwort = _client(ident, session).register(**ARGS)
    method, url, data, headers, timeout = session.calls[0]
    assert (method, url, timeout) == ("POST", f"{BASE}/devices/register", bootstrap.TIMEOUT_SECONDS)
    body = json.loads(data)
    assert set(body) == set(wire.REGISTER_FIELDS) and csr_passt(body["csr"], ident)
    assert headers[wire.HEADER_DEVICE] == ident.device_id and headers[wire.HEADER_TIMESTAMP] == str(NOW)
    signature = headers[wire.HEADER_SIGNATURE]
    der = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    ident.signing_key.public_key().verify(der, wire.canonical_string("POST", "/devices/register", NOW, data),
                                          ec.ECDSA(hashes.SHA256()))
    assert antwort.device_state == "nicht_uebernommen"
    assert antwort.zugang.endpoint == bootstrap.Endpoint("mqtt.e2e.test", 8883, ca.pem, None)


@pytest.mark.parametrize("wert", [float("nan"), float("inf"), float("-inf")])
def test_der_bootstrap_sendet_nie_eine_nicht_endliche_zahl(ident, ca, wert):
    """Testoffensive T2, Nr. 17: allow_nan=False; Bootstrap.holen faengt die Ausnahme (Backoff), die Anfrage geht nie raus."""
    session = FakeSession(FakeResponse(200, _body(ident, ca)))
    with pytest.raises(ValueError):
        _client(ident, session).register(**{**ARGS, "capabilities": {"drivers": [], "zigbee": wert}})
    assert session.calls == []
    assert _client(ident, session).register(**ARGS).zugang.endpoint.port == 8883    # Gegenprobe: endlich geht


def test_ein_zu_grosser_koerper_wird_nicht_gesendet(ident, ca):
    """V1 MAX_BODY_BYTES = 65536: der Bootstrap sendet keinen groesseren Koerper (ValueError wie bei allow_nan=False)."""
    session = FakeSession(FakeResponse(200, _body(ident, ca)))
    gross = {**ARGS, "capabilities": {**ARGS["capabilities"], "drivers": ["d" * 60] * 1200}}  # > 65536 Bytes
    with pytest.raises(ValueError):
        _client(ident, session).register(**gross)
    assert session.calls == []  # an der HTTP-Grenze kam nichts an


def test_ein_normaler_koerper_wird_gesendet(ident, ca):  # Gegenprobe
    session = FakeSession(FakeResponse(200, _body(ident, ca)))
    _client(ident, session).register(**ARGS)
    assert len(session.calls) == 1


def test_certificate_route_uses_the_device_path(ident, ca):
    session = FakeSession(FakeResponse(200, _body(ident, ca, state="uebernommen")))
    antwort = _client(ident, session).certificate()
    method, url, data, _, _ = session.calls[0]
    assert (method, url) == ("POST", f"{BASE}/devices/{ident.device_id}/certificate")
    body = json.loads(data)
    assert set(body) == {"csr"} and csr_passt(body["csr"], ident) and antwort.device_state == "uebernommen"


def test_alpn_is_kept_when_offered(ident, ca):
    session = FakeSession(FakeResponse(200, _body(ident, ca, alpn="x-amzn-mqtt-ca")))
    assert _client(ident, session).register(**ARGS).zugang.endpoint.alpn == "x-amzn-mqtt-ca"


@pytest.mark.parametrize("status, error", [
    (401, bootstrap.NotAuthenticated), (503, bootstrap.Unavailable), (429, bootstrap.Unavailable),
    (500, bootstrap.Unavailable), (400, bootstrap.Rejected), (404, bootstrap.Rejected),
])
def test_http_errors(ident, status, error):
    with pytest.raises(error):
        _client(ident, FakeSession(FakeResponse(status, {"error": "x"}))).register(**ARGS)


def test_network_errors_are_unavailable(ident):
    with pytest.raises(bootstrap.Unavailable, match="ConnectionError"):
        _client(ident, FakeSession(requests.ConnectionError("weg"))).register(**ARGS)


@pytest.mark.parametrize("change", [
    {"mqtt_ca": None}, {"mqtt_port": "8883"}, {"mqtt_port": True}, {"certificate": "kein PEM"}, {"mqtt_endpoint": ""},
])
def test_broken_answers_are_unavailable(ident, ca, change):
    with pytest.raises(bootstrap.Unavailable):
        _client(ident, FakeSession(FakeResponse(200, {**_body(ident, ca), **change}))).register(**ARGS)


def test_an_empty_endpoint_host_is_rejected(ident, ca):
    with pytest.raises(bootstrap.Unavailable, match="ungueltig"):
        _client(ident, FakeSession(FakeResponse(200, {**_body(ident, ca), "mqtt_endpoint": ""}))).register(**ARGS)


@pytest.mark.parametrize("port", [1883, 8884, 80, 0, 65535])
def test_only_the_iot_core_ports_are_accepted(ident, ca, port):
    with pytest.raises(bootstrap.Unavailable, match="ungueltig"):
        _client(ident, FakeSession(FakeResponse(200, _body(ident, ca, port=port)))).register(**ARGS)


@pytest.mark.parametrize("port", [8883, 443])
def test_the_iot_core_ports_are_accepted(ident, ca, port):
    session = FakeSession(FakeResponse(200, _body(ident, ca, port=port)))
    assert _client(ident, session).register(**ARGS).zugang.endpoint.port == port


def test_without_alpn_the_endpoint_has_none(ident, ca):
    session = FakeSession(FakeResponse(200, _body(ident, ca)))
    assert _client(ident, session).register(**ARGS).zugang.endpoint.alpn is None


@pytest.mark.parametrize("alpn", [
    "http/1.1", "", "X-AMZN-MQTT-CA", "x" * 300, "x-amzn-mqtt-c\u00e4", 443, ["x-amzn-mqtt-ca"],
])
def test_an_alpn_other_than_none_or_x_amzn_mqtt_ca_is_rejected(ident, ca, alpn):
    """Der Server sendet ALPN nur als "x-amzn-mqtt-ca" (device_ca.ALPN_443) oder gar nicht. Alles andere, auch
    ein ueberlanger oder nicht-ASCII-String, den set_alpn_protocols mit ValueError/UnicodeEncodeError ablehnen wuerde,
    wird vor dem TLS abgewiesen (Bedrohungsmodell: Server-Eingabe wird geprueft)."""
    body = {**_body(ident, ca), "mqtt_alpn": alpn}
    with pytest.raises(bootstrap.Unavailable, match="ungueltig"):
        _client(ident, FakeSession(FakeResponse(200, body))).register(**ARGS)


@pytest.mark.parametrize("ca_pem", ["kein PEM", "", None])
def test_a_broker_ca_without_a_pem_certificate_is_rejected(ident, ca, ca_pem):
    with pytest.raises(bootstrap.Unavailable, match="ungueltig"):
        _client(ident, FakeSession(FakeResponse(200, {**_body(ident, ca), "mqtt_ca": ca_pem}))).register(**ARGS)


def test_a_non_json_answer_is_unavailable(ident):
    with pytest.raises(bootstrap.Unavailable):
        _client(ident, FakeSession(FakeResponse(200, None))).register(**ARGS)


def test_storage_roundtrip(tmp_path, ident, ca):
    answer = bootstrap._antwort(_body(ident, ca, state="uebernommen", alpn="x-amzn-mqtt-ca"))
    assert bootstrap.laden(tmp_path) is None and bootstrap.device_state(tmp_path) is None
    bootstrap.speichern(tmp_path, answer)
    assert bootstrap.laden(tmp_path) == answer.zugang
    assert bootstrap.device_state(tmp_path) == "uebernommen"
    assert (tmp_path / bootstrap.CERTIFICATE_FILE).stat().st_mode & 0o077 == 0


def test_a_broken_endpoint_file_means_no_access(tmp_path, ident, ca):
    bootstrap.speichern(tmp_path, bootstrap._antwort(_body(ident, ca)))
    (tmp_path / bootstrap.ENDPOINT_FILE).write_text("{")
    assert bootstrap.laden(tmp_path) is None


# --- Ablauf ---

class ScriptedClient:
    def __init__(self, **script) -> None:
        self.script = {route: list(answers) for route, answers in script.items()}
        self.calls: list[str] = []

    def __call__(self):
        return self

    def register(self, **kwargs):
        assert kwargs == ARGS
        return self._next("register")

    def certificate(self):
        return self._next("certificate")

    def _next(self, route):
        self.calls.append(route)
        answer = self.script[route].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _flow(tmp_path, ident, client, *, synchron=True, clock=None):
    return bootstrap.Bootstrap(tmp_path, client, register_args=lambda: dict(ARGS), uhr_synchron=lambda: synchron,
                               identity=lambda: ident, clock=clock or Clock())


def _ok(ident, ca, **kwargs):
    return bootstrap._antwort(_body(ident, ca, **kwargs))


def test_first_start_registers_and_stores(tmp_path, ident, ca):
    client = ScriptedClient(register=[_ok(ident, ca)])
    zugang = _flow(tmp_path, ident, client).holen(rettung=False)
    assert zugang is not None and client.calls == ["register"] and bootstrap.laden(tmp_path) == zugang


def test_rescue_takes_a_new_certificate(tmp_path, ident, ca):
    client = ScriptedClient(certificate=[_ok(ident, ca, state="uebernommen")])
    assert _flow(tmp_path, ident, client).holen(rettung=True) is not None and client.calls == ["certificate"]


def test_rescue_registers_when_the_server_forgot_the_device(tmp_path, ident, ca):
    client = ScriptedClient(certificate=[bootstrap.NotAuthenticated("certificate")], register=[_ok(ident, ca)])
    assert _flow(tmp_path, ident, client).holen(rettung=True) is not None
    assert client.calls == ["certificate", "register"]


def test_401_on_both_with_a_synchronised_clock_means_blocked(tmp_path, ident, ca):
    clock = Clock()
    client = ScriptedClient(certificate=[bootstrap.NotAuthenticated("c"), _ok(ident, ca)],
                            register=[bootstrap.NotAuthenticated("r")])
    flow = _flow(tmp_path, ident, client, clock=clock)
    assert flow.holen(rettung=True) is None and flow.gesperrt and not flow.faellig()
    clock.now += bootstrap.GESPERRT_SECONDS - 1
    assert not flow.faellig()
    clock.now += 1
    assert flow.faellig()
    assert flow.holen(rettung=True) is not None and not flow.gesperrt  # entsperrt: der Server stellt wieder aus


@pytest.mark.parametrize("synchron", [False, None])
def test_401_with_an_unsynchronised_clock_is_not_blocked(tmp_path, ident, synchron):
    clock = Clock()
    client = ScriptedClient(certificate=[bootstrap.NotAuthenticated("c")], register=[bootstrap.NotAuthenticated("r")])
    flow = _flow(tmp_path, ident, client, synchron=synchron, clock=clock)
    assert flow.holen(rettung=True) is None
    assert not flow.gesperrt
    clock.now += bootstrap.RETRY_MIN_SECONDS
    assert flow.faellig()


def test_an_unavailable_server_backs_off_up_to_15_minutes(tmp_path, ident):
    clock = Clock()
    client = ScriptedClient(register=[bootstrap.Unavailable("x")] * 10)
    flow = _flow(tmp_path, ident, client, clock=clock)
    delays = []
    for _ in range(8):
        start = clock.now
        flow.holen(rettung=False)
        while not flow.faellig():
            clock.now += 1
        delays.append(clock.now - start)
    assert delays == [30, 60, 120, 240, 480, 900, 900, 900]


def test_a_rejected_request_is_retried_later_and_not_blocked(tmp_path, ident):
    flow = _flow(tmp_path, ident, ScriptedClient(register=[bootstrap.Rejected("register: HTTP 400")]))
    assert flow.holen(rettung=False) is None and not flow.gesperrt and not flow.faellig()


def test_a_request_outside_the_contract_is_retried_later_and_not_blocked_and_logged_without_text(
        tmp_path, ident, caplog):
    fehler = nachrichten.Vertragsfehler("register: fremd ['GEHEIM-FELD']")
    flow = _flow(tmp_path, ident, ScriptedClient(register=[fehler]))
    assert flow.holen(rettung=False) is None and not flow.gesperrt and not flow.faellig()
    assert "Anfrage nicht gebaut (Vertragsfehler)" in caplog.text and "GEHEIM-FELD" not in caplog.text


def test_a_host_callback_value_error_is_not_logged_with_its_text_and_is_retried_later(tmp_path, ident, caplog):
    def args() -> dict:
        raise ValueError("geheimer Parameter")  # z. B. host.faehigkeiten()

    flow = bootstrap.Bootstrap(tmp_path, ScriptedClient(register=[]), register_args=args, uhr_synchron=lambda: True,
                               identity=lambda: ident, clock=Clock())
    assert flow.holen(rettung=False) is None and not flow.gesperrt and not flow.faellig()  # Backoff, nicht weiterwerfen
    assert "unerwarteter Fehler (ValueError) in " in caplog.text and "args" in caplog.text
    assert "geheimer Parameter" not in caplog.text


def test_ein_zu_grosser_koerper_wird_spaeter_wiederholt_nicht_gesperrt_und_nicht_gesendet(tmp_path, ident, ca, caplog):
    """MAX_BODY_BYTES (V1) mit dem echten Client: Bootstrap.holen faengt den ValueError (Backoff), gesendet wird nichts."""
    session = FakeSession()
    gross = {**ARGS, "capabilities": {**ARGS["capabilities"], "drivers": ["d" * 60] * 1200}}
    flow = bootstrap.Bootstrap(tmp_path, lambda: _client(ident, session), register_args=lambda: gross,
                               uhr_synchron=lambda: True, identity=lambda: ident, clock=Clock())
    assert flow.holen(rettung=False) is None and not flow.gesperrt and not flow.faellig()
    assert session.calls == [] and "d" * 60 not in caplog.text


def test_a_failing_host_callback_in_the_rescue_after_401_is_retried_later_too(tmp_path, ident, caplog):
    """Rettung: certificate -> 401 -> register baut seine Argumente (Host-Rueckruf) und scheitert: Backoff, keine Sperre."""
    def args() -> dict:
        raise ValueError("geheimer Parameter")

    client = ScriptedClient(register=[], certificate=[bootstrap.NotAuthenticated("certificate")])
    flow = bootstrap.Bootstrap(tmp_path, client, register_args=args, uhr_synchron=lambda: True,
                               identity=lambda: ident, clock=Clock())
    assert flow.holen(rettung=True) is None and not flow.gesperrt and not flow.faellig()
    assert "geheimer Parameter" not in caplog.text


@pytest.mark.parametrize("feld", wire.BOOTSTRAP_RESPONSE)
def test_an_answer_without_a_required_field_is_unavailable(ident, ca, feld):
    body = _body(ident, ca)
    del body[feld]
    with pytest.raises(bootstrap.Unavailable, match="unvollstaendig"):
        _client(ident, FakeSession(FakeResponse(200, body))).register(**ARGS)


def test_register_with_capabilities_outside_the_contract_is_not_built(ident):
    session = FakeSession(FakeResponse(200, {}))
    with pytest.raises(ValueError, match="capabilities"):
        _client(ident, session).register(**{**ARGS, "capabilities": {"drivers": [], "zigbee": True, "frei": 1}})
    assert session.calls == []


def test_a_certificate_for_another_key_is_not_stored(tmp_path, ident, ca):
    other = identity.load_or_create(tmp_path / "anderes")
    client = ScriptedClient(register=[_ok(ident, ca, cert=zertifikat_fuer(other, ca))])
    assert _flow(tmp_path, ident, client).holen(rettung=False) is None and bootstrap.laden(tmp_path) is None


def test_after_a_foreign_certificate_from_register_the_next_try_renews_for_the_own_csr(tmp_path, ident, ca):
    clock = Clock()
    other = identity.load_or_create(tmp_path / "anderes")
    client = ScriptedClient(register=[_ok(ident, ca, cert=zertifikat_fuer(other, ca))], certificate=[_ok(ident, ca)])
    flow = _flow(tmp_path, ident, client, clock=clock)
    assert flow.holen(rettung=False) is None and not flow.faellig()
    clock.now += bootstrap.RETRY_MIN_SECONDS
    assert flow.holen(rettung=False) is not None and client.calls == ["register", "certificate"]


def test_success_resets_the_backoff(tmp_path, ident):
    clock = Clock()
    client = ScriptedClient(certificate=[bootstrap.NotAuthenticated("c")], register=[bootstrap.NotAuthenticated("r")])
    flow = _flow(tmp_path, ident, client, synchron=False, clock=clock)
    assert flow.holen(rettung=True) is None and not flow.faellig()
    flow.erfolg()  # der Link verbindet mit dem bisherigen Zugang
    assert flow.faellig() and not flow.gesperrt


def test_an_unusable_access_counts_as_a_failure_with_backoff(tmp_path, ident, ca):
    clock = Clock()
    flow = _flow(tmp_path, ident, ScriptedClient(certificate=[_ok(ident, ca)]), clock=clock)
    assert flow.holen(rettung=True) is not None and flow.faellig()
    flow.spaeter()  # Link mit dem neuen Zugang startet nicht (TransportConfigError)
    assert not flow.faellig() and not flow.gesperrt
    clock.now += bootstrap.RETRY_MIN_SECONDS
    assert flow.faellig()


@pytest.mark.parametrize("feld, wert", [("mqtt_endpoint", "h\ud800"), ("mqtt_ca", "-----BEGIN CERTIFICATE-----\udfff")])
def test_a_response_with_a_lone_surrogate_is_unusable_and_backs_off(tmp_path, ident, ca, feld, wert):
    """speichern() koennte ein einzelnes Surrogat (json.loads aus "\\ud800") nicht schreiben: UnicodeEncodeError
    ausserhalb des try in holen, kein Backoff, register in jedem Takt (Testoffensive T2, Task 13, Fix-Runde 1)."""
    antwort = {**_body(ident, ca), feld: wert}
    with pytest.raises(bootstrap.Unavailable):
        _client(ident, FakeSession(FakeResponse(200, antwort))).register(**ARGS)
    flow = bootstrap.Bootstrap(tmp_path, lambda: _client(ident, FakeSession(FakeResponse(200, antwort))),
                               register_args=lambda: dict(ARGS), uhr_synchron=lambda: True, identity=lambda: ident,
                               clock=Clock())
    assert flow.holen(rettung=False) is None and not flow.gesperrt and not flow.faellig()  # Backoff
    assert not (tmp_path / bootstrap.ENDPOINT_FILE).exists() and not (tmp_path / bootstrap.CERTIFICATE_FILE).exists()
