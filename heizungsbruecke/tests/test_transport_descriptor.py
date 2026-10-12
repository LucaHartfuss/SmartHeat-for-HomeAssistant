import json

import pytest

from smartheat_transport.descriptor import (
    ALPN_443,
    CREDENTIAL_CERTIFICATE,
    KIND_IOT_CORE,
    Credential,
    Descriptor,
    TransportConfigError,
    parse_descriptor,
)

CA = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"
IOT = {"kind": KIND_IOT_CORE, "host": "abc-ats.iot.eu-central-1.amazonaws.com", "port": 8883, "alpn": None,
       "ca_pem": CA, "client_id": "client1"}


def test_iot_descriptor_from_json_string():
    assert parse_descriptor(json.dumps(IOT)) == Descriptor(
        KIND_IOT_CORE, IOT["host"], 8883, None, CA, "client1")


def test_the_removed_mosquitto_transport_is_unknown():
    with pytest.raises(TransportConfigError, match="unbekannt"):
        parse_descriptor({"kind": "mosquitto_cloudflared", "host": "127.0.0.1", "port": 18830})


def test_iot_descriptor_on_8883_and_443():
    assert parse_descriptor(IOT).client_id == "client1"
    assert parse_descriptor({**IOT, "port": 443, "alpn": ALPN_443}).alpn == ALPN_443


@pytest.mark.parametrize("raw, fragment", [
    ("kein json", "kein JSON"),
    ("[1]", "kein Objekt"),
    ({**IOT, "kind": "rabbitmq"}, "unbekannt"),
    ({**IOT, "cloudflared": {}}, "unbekannt ['cloudflared']"),
    ({"kind": KIND_IOT_CORE, "host": "h", "port": 8883}, "fehlend"),
    ({**IOT, "host": ""}, "host"),
    ({**IOT, "port": True}, "port"),
    ({**IOT, "port": 70000}, "port"),
    ({**IOT, "port": 1883}, "8883, 443"),
    ({**IOT, "port": 443}, "alpn"),
    ({**IOT, "alpn": ALPN_443}, "alpn"),
    ({**IOT, "ca_pem": "kein pem"}, "ca_pem"),
    ({**IOT, "client_id": ""}, "client_id"),
])
def test_broken_descriptors_are_rejected_with_a_reason(raw, fragment):
    with pytest.raises(TransportConfigError, match=fragment.replace("[", r"\[").replace("]", r"\]")):
        parse_descriptor(raw)


def test_secrets_are_not_in_the_repr():
    credential = Credential(CREDENTIAL_CERTIFICATE, certificate_pem="CERT", private_key_pem="KEY-GEHEIM")
    text = repr(credential) + repr(parse_descriptor(IOT))
    assert "KEY-GEHEIM" not in text and "AAAA" not in text

