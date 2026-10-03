import socket
import ssl
import threading
from unittest.mock import MagicMock

import pytest
from tls_helpers import ca_pem, issue, make_ca

from smartheat_transport.connect import (
    CONNECT_ERROR_NETWORK,
    CONNECT_ERROR_TLS,
    CONNECT_ERROR_UNKNOWN,
    apply,
    classify_connect_error,
    connect_options,
)
from smartheat_transport.descriptor import (
    ALPN_443,
    CREDENTIAL_PASSWORD,
    KIND_IOT_CORE,
    KIND_MOSQUITTO,
    Credential,
    Descriptor,
    TransportConfigError,
    credential_for,
)


@pytest.fixture(scope="module")
def pki():
    ca = make_ca()
    return {"ca": ca, "server": issue(ca, "localhost", server=True), "client": issue(ca, "client1")}


def _iot(pki, port=8883, alpn=None):
    return Descriptor(KIND_IOT_CORE, "127.0.0.1", port, alpn, ca_pem(pki["ca"]), "client1")


def _cert_credential(pki, name="client"):
    key, cert = pki[name]
    return Credential("certificate", certificate_pem=cert, private_key_pem=key)


def test_password_mode_has_no_tls_and_a_paho_generated_client_id():
    options = connect_options(Descriptor(KIND_MOSQUITTO, "127.0.0.1", 18830),
                              Credential(CREDENTIAL_PASSWORD, username="u", password="p"))
    assert (options.host, options.port, options.client_id, options.ssl_context) == ("127.0.0.1", 18830, "", None)
    client = MagicMock()
    apply(client, options)
    client.username_pw_set.assert_called_once_with("u", "p")
    client.tls_set_context.assert_not_called()


def test_certificate_mode_uses_tls_and_the_descriptor_client_id(pki):
    options = connect_options(_iot(pki), _cert_credential(pki))
    assert options.client_id == "client1" and options.username is None
    client = MagicMock()
    apply(client, options)
    client.tls_set_context.assert_called_once_with(options.ssl_context)
    client.username_pw_set.assert_not_called()


def test_tls_handshake_with_client_certificate_and_alpn_against_a_local_server(pki, tmp_path):
    server_key, server_cert = pki["server"]
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    (tmp_path / "s.pem").write_text(server_cert)
    (tmp_path / "s.key").write_text(server_key)
    server_ctx.load_cert_chain(tmp_path / "s.pem", tmp_path / "s.key")
    server_ctx.load_verify_locations(cadata=ca_pem(pki["ca"]))
    server_ctx.verify_mode = ssl.CERT_REQUIRED
    server_ctx.set_alpn_protocols([ALPN_443])
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    seen = {}

    listener.settimeout(5)  # ein Fehler darf die CI nicht haengen lassen

    def _serve():
        try:
            conn, _ = listener.accept()
            conn.settimeout(5)
            with server_ctx.wrap_socket(conn, server_side=True) as tls:
                seen["peer"] = dict(item[0] for item in tls.getpeercert()["subject"])["commonName"]
                seen["alpn"] = tls.selected_alpn_protocol()
        except (OSError, ssl.SSLError) as error:
            seen["error"] = type(error).__name__

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    options = connect_options(_iot(pki, port=443, alpn=ALPN_443), _cert_credential(pki))
    with (
        socket.create_connection(("127.0.0.1", port)) as raw,
        options.ssl_context.wrap_socket(raw, server_hostname="127.0.0.1") as tls,
    ):
        assert tls.selected_alpn_protocol() == ALPN_443
    thread.join(5)
    listener.close()
    assert not thread.is_alive()
    assert seen == {"peer": "client1", "alpn": ALPN_443}


def test_key_that_does_not_match_the_certificate_is_a_config_error(pki):
    other_key, _ = issue(pki["ca"], "fremd")
    _, cert = pki["client"]
    with pytest.raises(TransportConfigError, match="passen nicht"):
        connect_options(_iot(pki), Credential("certificate", certificate_pem=cert, private_key_pem=other_key))


def test_broken_ca_is_a_config_error(pki):
    descriptor = Descriptor(KIND_IOT_CORE, "h", 8883, None, "-----BEGIN CERTIFICATE-----\nkaputt\n-----END CERTIFICATE-----\n", "c")
    with pytest.raises(TransportConfigError, match="CA"):
        connect_options(descriptor, _cert_credential(pki))


def test_credential_of_the_wrong_kind_is_a_config_error(pki):
    with pytest.raises(TransportConfigError):
        connect_options(_iot(pki), Credential(CREDENTIAL_PASSWORD, username="u", password="p"))


def test_temporary_key_files_are_removed(pki, tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    connect_options(_iot(pki), credential_for(_iot(pki), certificate_pem=pki["client"][1], private_key_pem=pki["client"][0]))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("error, expected", [
    (ssl.SSLError("handshake"), CONNECT_ERROR_TLS),
    (ssl.SSLCertVerificationError("verify"), CONNECT_ERROR_TLS),
    (ConnectionRefusedError(), CONNECT_ERROR_NETWORK),
    (ConnectionResetError(), CONNECT_ERROR_NETWORK),
    (socket.gaierror(), CONNECT_ERROR_NETWORK),
    (TimeoutError(), CONNECT_ERROR_NETWORK),
    (None, CONNECT_ERROR_UNKNOWN),
    (ValueError(), CONNECT_ERROR_UNKNOWN),
])
def test_connect_errors_are_classified(error, expected):
    assert classify_connect_error(error) == expected


def _boom(*args, **kwargs):
    raise OSError("Platte voll: GEHEIM-PFAD")


@pytest.mark.parametrize("target", ["smartheat_transport.connect.tempfile.TemporaryDirectory",
                                    "smartheat_transport.connect.os.open"])
def test_unwritable_temp_storage_is_a_config_error_without_leaking_the_message(pki, monkeypatch, target):
    monkeypatch.setattr(target, _boom)
    with pytest.raises(TransportConfigError, match=r"nicht ablegbar \(OSError\)") as info:
        connect_options(_iot(pki), _cert_credential(pki))
    assert "GEHEIM-PFAD" not in str(info.value)


def test_nul_byte_inside_the_key_is_a_config_error(pki):
    key, cert = pki["client"]
    broken = key[:60] + "\x00" + key[60:]
    with pytest.raises(TransportConfigError):
        connect_options(_iot(pki), Credential("certificate", certificate_pem=cert, private_key_pem=broken))
