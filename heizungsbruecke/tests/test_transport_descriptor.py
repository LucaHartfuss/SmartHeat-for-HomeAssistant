import json

import pytest

from smartheat_transport.descriptor import (
    ALPN_443,
    CREDENTIAL_CERTIFICATE,
    CREDENTIAL_PASSWORD,
    KIND_IOT_CORE,
    KIND_MOSQUITTO,
    Credential,
    Descriptor,
    TransportConfigError,
    credential_for,
    parse_descriptor,
)

CA = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"
MOSQUITTO = {"kind": KIND_MOSQUITTO, "host": "127.0.0.1", "port": 18830}
IOT = {"kind": KIND_IOT_CORE, "host": "abc-ats.iot.eu-central-1.amazonaws.com", "port": 8883, "alpn": None,
       "ca_pem": CA, "client_id": "client1"}


def test_mosquitto_descriptor_from_json_string():
    assert parse_descriptor(json.dumps(MOSQUITTO)) == Descriptor(KIND_MOSQUITTO, "127.0.0.1", 18830)


def test_iot_descriptor_on_8883_and_443():
    assert parse_descriptor(IOT).client_id == "client1"
    assert parse_descriptor({**IOT, "port": 443, "alpn": ALPN_443}).alpn == ALPN_443


@pytest.mark.parametrize("raw, fragment", [
    ("kein json", "kein JSON"),
    ("[1]", "kein Objekt"),
    ({**MOSQUITTO, "kind": "rabbitmq"}, "unbekannt"),
    ({**MOSQUITTO, "cloudflared": {}}, "unbekannt ['cloudflared']"),
    ({"kind": KIND_IOT_CORE, "host": "h", "port": 8883}, "fehlend"),
    ({**MOSQUITTO, "host": ""}, "host"),
    ({**MOSQUITTO, "port": True}, "port"),
    ({**MOSQUITTO, "port": 70000}, "port"),
    ({**IOT, "port": 1883}, "8883, 443"),
    ({**IOT, "port": 443}, "alpn"),
    ({**IOT, "alpn": ALPN_443}, "alpn"),
    ({**IOT, "ca_pem": "kein pem"}, "ca_pem"),
    ({**IOT, "client_id": ""}, "client_id"),
])
def test_broken_descriptors_are_rejected_with_a_reason(raw, fragment):
    with pytest.raises(TransportConfigError, match=fragment.replace("[", r"\[").replace("]", r"\]")):
        parse_descriptor(raw)


def test_password_credential_for_mosquitto():
    credential = credential_for(parse_descriptor(MOSQUITTO), username="u", password="geheim")
    assert credential == Credential(CREDENTIAL_PASSWORD, username="u", password="geheim")


def test_certificate_credential_for_iot():
    credential = credential_for(parse_descriptor(IOT), certificate_pem="C", private_key_pem="K")
    assert credential.kind == CREDENTIAL_CERTIFICATE


@pytest.mark.parametrize("descriptor, fields", [
    (MOSQUITTO, {"username": "u"}),
    (MOSQUITTO, {"username": "u", "password": "p", "private_key_pem": "K"}),
    (IOT, {"certificate_pem": "C"}),
    (IOT, {"certificate_pem": "C", "private_key_pem": "K", "password": "p"}),
    (IOT, {"username": "u", "password": "p"}),
])
def test_credentials_that_do_not_match_the_descriptor_are_rejected(descriptor, fields):
    with pytest.raises(TransportConfigError):
        credential_for(parse_descriptor(descriptor), **fields)


def test_secrets_are_not_in_the_repr():
    credential = Credential(CREDENTIAL_CERTIFICATE, password="pw-geheim", certificate_pem="CERT", private_key_pem="KEY-GEHEIM")
    text = repr(credential) + repr(parse_descriptor(IOT))
    assert "pw-geheim" not in text and "KEY-GEHEIM" not in text and "AAAA" not in text


def test_error_messages_never_quote_secret_values():
    with pytest.raises(TransportConfigError) as info:
        credential_for(parse_descriptor(IOT), certificate_pem="C", private_key_pem="KEY-GEHEIM", password="pw-geheim")
    assert "KEY-GEHEIM" not in str(info.value) and "pw-geheim" not in str(info.value)
