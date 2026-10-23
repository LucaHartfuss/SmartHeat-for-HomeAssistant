"""Die eine MQTT-Verbindung - MQTT 5 mit persistenter Session (AN-9), Abo down/# (AN-6), Anmeldefehler
fuer den Rettungsweg (AN-1), Rueckfall auf 443 mit ALPN (AN-3)."""
import ssl
from types import SimpleNamespace

import pytest
from tls_helpers import ca_pem, issue, make_ca

from smartheat_device import link as link_module
from smartheat_device import wire
from smartheat_device.bootstrap import Endpoint, Zugang
from smartheat_device.tls import TransportConfigError

DEVICE = "shg-aaaaaaaaaaaaaaaa"


class FakePaho:
    """Gleiche Oberflaeche wie paho.mqtt.client.Client (soweit der Link sie nutzt); Rueckrufe loest der Test aus."""

    def __init__(self, client_id):
        self.client_id = client_id
        self.context = None
        self.connect_args = None
        self.started = self.stopped = self.connected = False
        self.published, self.subscribed = [], []
        self.rc = 0
        self._mid = 0

    def tls_set_context(self, context):
        self.context = context

    def username_pw_set(self, username, password):
        raise AssertionError("Geraete melden sich nur mit Zertifikat an")

    def reconnect_delay_set(self, min_delay, max_delay):
        self.delays = (min_delay, max_delay)

    def connect_async(self, host, port, keepalive=60, clean_start=True, properties=None):
        self.connect_args = {"host": host, "port": port, "keepalive": keepalive, "clean_start": clean_start,
                             "properties": properties}

    def loop_start(self):
        self.started = True

    def loop_stop(self):
        self.stopped = True

    def disconnect(self):
        self.connected = False

    def is_connected(self):
        return self.connected

    def subscribe(self, topic, qos=0):
        self.subscribed.append((topic, qos))

    def publish(self, topic, payload=None, qos=0, retain=False):
        self._mid += 1
        self.published.append((topic, payload, qos, retain))
        return SimpleNamespace(rc=self.rc, mid=self._mid)

    # --- Rueckrufe wie paho ---

    def connack(self, failure=False):
        self.connected = not failure
        self.on_connect(self, None, {}, SimpleNamespace(is_failure=failure, value=135 if failure else 0), None)

    def drop(self):
        self.connected = False
        self.on_disconnect(self, None, {}, SimpleNamespace(is_failure=True, value=0), None)

    def fail(self, error):
        try:
            raise error
        except Exception:
            self.on_connect_fail(self, None)  # paho ruft es im except-Block (sys.exception())

    def message(self, topic, payload=b"{}", retain=False):
        self.on_message(self, None, SimpleNamespace(topic=topic, payload=payload, retain=retain))


@pytest.fixture(scope="module")
def tls():
    ca = make_ca()
    key, cert = issue(ca, DEVICE)
    return ca_pem(ca), cert, key


def _link(tls, *, port=8883, alpn=None):
    ca, cert, key = tls
    events, clients = [], []

    def factory(client_id):
        clients.append(FakePaho(client_id))
        return clients[-1]

    link = link_module.Link(DEVICE, Zugang(cert, Endpoint("mosquitto", port, ca, alpn)), key,
                            lambda *event: events.append(event), client_factory=factory)
    link.start()
    return link, clients, events


def test_connects_persistently_with_the_device_certificate(tls):
    link, clients, _ = _link(tls)
    client = clients[-1]
    args = client.connect_args
    assert (client.client_id, args["host"], args["port"], args["clean_start"]) == (DEVICE, "mosquitto", 8883, False)
    assert args["properties"].SessionExpiryInterval == 3600 and args["keepalive"] == link_module.KEEPALIVE_SECONDS
    assert client.started and isinstance(client.context, ssl.SSLContext)
    assert client.delays == (1, link_module.RECONNECT_MAX_DELAY_SECONDS)


def test_subscribes_the_down_filter_after_each_connect_and_reports(tls):
    _, clients, events = _link(tls)
    clients[-1].connack()
    clients[-1].drop()
    clients[-1].connack()
    assert clients[-1].subscribed == [(wire.topic(DEVICE, wire.DEVICE_SUBSCRIPTION), wire.QOS)] * 2
    assert wire.topic(DEVICE, wire.DEVICE_SUBSCRIPTION) == f"smartheat/{DEVICE}/down/#"
    assert events == [("verbunden",), ("getrennt",), ("verbunden",)]


def test_messages_on_own_down_topics_only(tls):
    _, clients, events = _link(tls)
    client = clients[-1]
    client.connack()
    events.clear()
    client.message(f"smartheat/{DEVICE}/down/config", b'{"version": 1}')
    client.message(f"smartheat/{DEVICE}/down/zukunft", b"{}")
    client.message("smartheat/shg-bbbbbbbbbbbbbbbb/down/config", b"{}")
    client.message(f"smartheat/{DEVICE}/down/config", b"{}", retain=True)
    client.message(f"smartheat/{DEVICE}/up/hello", b"{}")
    client.message("anderes/topic", b"{}")
    assert events == [("nachricht", "down/config", b'{"version": 1}'), ("nachricht", "down/zukunft", b"{}")]


def test_publish_only_while_connected(tls):
    link, clients, _ = _link(tls)
    assert link.publish("up/hello", {"a": 1}) is None and clients[-1].published == []
    clients[-1].connack()
    assert link.publish("up/hello", {"a": "ä"}) == 1
    assert clients[-1].published == [(f"smartheat/{DEVICE}/up/hello", '{"a":"ä"}'.encode(), 1, False)]
    clients[-1].rc = 4  # MQTT_ERR_NO_CONN
    assert link.publish("up/status", {}) is None


def test_publish_refuses_foreign_topics_and_oversized_messages(tls):
    link, clients, _ = _link(tls)
    clients[-1].connack()
    with pytest.raises(ValueError):
        link.publish("down/config", {})
    with pytest.raises(ValueError):
        link.publish("up/inventory", {"x": "y" * 130_000})


@pytest.mark.parametrize("wert", [float("nan"), float("inf"), float("-inf")])
def test_publish_sendet_nie_eine_nicht_endliche_zahl(tls, wert):
    """allow_nan=False ist der letzte Riegel; der Aufrufer (Device._publish) faengt ValueError."""
    link, clients, _ = _link(tls)
    clients[-1].connack()
    with pytest.raises(ValueError):
        link.publish("up/status", {"x": wert})
    with pytest.raises(ValueError):
        link.publish("up/status", {"verschachtelt": {"x": [1.0, wert]}})
    assert clients[-1].published == []    # an der MQTT-Grenze kam nichts an
    assert link.publish("up/status", {"x": 1.5}) == 1    # Gegenprobe: ein endlicher Wert geht hinaus


def test_three_auth_failures_ask_for_rescue_once(tls):
    link, clients, events = _link(tls)
    client = clients[-1]
    client.connack(failure=True)
    client.fail(ssl.SSLError("bad certificate"))
    client.drop()  # Trennung ohne CONNACK (IoT Core bei gesperrtem Zertifikat, AN-1)
    assert events == [("anmeldung_scheitert",)] and link.connect_failures == 3
    client.connack(failure=True)
    assert events == [("anmeldung_scheitert",)]


def test_network_errors_do_not_count_and_success_resets(tls):
    link, clients, events = _link(tls)
    client = clients[-1]
    for _ in range(5):
        client.fail(ConnectionRefusedError())
    client.connack(failure=True)
    client.connack(failure=True)
    assert events == [] and link.connect_failures == 7
    client.connack()
    assert link.connect_failures == 0 and events == [("verbunden",)]
    client.drop()
    client.connack(failure=True)
    assert ("anmeldung_scheitert",) not in events


def test_port_falls_back_to_443_with_alpn_and_back(tls):
    link, clients, _ = _link(tls, alpn="x-amzn-mqtt-ca")
    for _ in range(link_module.PORT_FALLBACK_FAILURES):
        clients[-1].fail(OSError("Netz"))
    link.port_wechseln_falls_noetig()
    assert clients[0].stopped and len(clients) == 2
    assert clients[1].connect_args["port"] == 443 and link.port == 443
    for _ in range(link_module.PORT_FALLBACK_FAILURES):
        clients[-1].fail(OSError("Netz"))
    link.port_wechseln_falls_noetig()
    assert clients[-1].connect_args["port"] == 8883


def test_every_new_paho_client_counts_up_client_nr(tls):
    """Daran erkennt das Geraet, dass mids (je Client ab 1) und unbestaetigte Nachrichten des alten Clients weg sind."""
    link, clients, _ = _link(tls, alpn="x-amzn-mqtt-ca")
    nummer = link.client_nr
    assert nummer >= 1
    for _ in range(link_module.PORT_FALLBACK_FAILURES):
        clients[-1].fail(OSError("Netz"))
    link.port_wechseln_falls_noetig()
    assert link.client_nr == nummer + 1


def _link_mit_startfehlern(tls, fehler):
    """Link mit ALPN; die Fabrik wirft beim n-ten Aufbau (ab 0) TransportConfigError, wenn fehler[n] wahr ist."""
    ca, cert, key = tls
    events, clients, aufrufe = [], [], []

    def factory(client_id):
        aufrufe.append(client_id)
        if fehler[len(aufrufe) - 1]:
            raise TransportConfigError("Port passt nicht")
        clients.append(FakePaho(client_id))
        return clients[-1]

    link = link_module.Link(DEVICE, Zugang(cert, Endpoint("mosquitto", 8883, ca, "x-amzn-mqtt-ca")), key,
                            lambda *event: events.append(event), client_factory=factory)
    link.start()
    for _ in range(link_module.PORT_FALLBACK_FAILURES):
        clients[-1].fail(OSError("Netz"))
    return link, clients, events


def test_a_port_switch_that_cannot_start_stays_on_the_previous_port(tls):
    link, clients, events = _link_mit_startfehlern(tls, [False, True, False])
    link.port_wechseln_falls_noetig()
    assert link.port == 8883 and len(clients) == 2 and clients[0].stopped
    assert clients[1].connect_args["port"] == 8883 and clients[1].started and events == []


def test_a_link_that_cannot_start_on_either_port_asks_for_rescue(tls):
    link, clients, events = _link_mit_startfehlern(tls, [False, True, True])
    link.port_wechseln_falls_noetig()
    assert len(clients) == 1 and clients[0].stopped and not link.connected
    assert events == [("anmeldung_scheitert",)] and link.port == 8883


def test_no_fallback_without_alpn(tls):
    link, clients, _ = _link(tls)
    for _ in range(5):
        clients[-1].fail(OSError("Netz"))
    link.port_wechseln_falls_noetig()
    assert len(clients) == 1


def test_puback_is_reported(tls):
    link, clients, events = _link(tls)
    clients[-1].on_publish(clients[-1], None, 7, SimpleNamespace(is_failure=False), None)
    assert events == [("gesendet", 7, link.client_nr, True)]  # die Nummer des sendenden Clients, Erfolg


def test_puback_with_an_error_code_is_not_a_confirmation(tls):
    """MQTT 5: der Broker kann ein PUBACK mit Fehlercode schicken (z. B. 0x97 Quota exceeded)."""
    link, clients, events = _link(tls)
    clients[-1].on_publish(clients[-1], None, 7, SimpleNamespace(is_failure=True), None)
    assert events == [("gesendet", 7, link.client_nr, False)]


def test_puback_of_an_old_client_carries_the_number_of_that_client(tls):
    link, clients, events = _link(tls, alpn="x-amzn-mqtt-ca")
    alt, alte_nr = clients[0], link.client_nr
    for _ in range(link_module.PORT_FALLBACK_FAILURES):
        clients[-1].fail(OSError("Netz"))
    link.port_wechseln_falls_noetig()
    alt.on_publish(alt, None, 3, SimpleNamespace(is_failure=False), None)
    assert events[-1] == ("gesendet", 3, alte_nr, True) and link.client_nr != alte_nr


def test_stop_disconnects(tls):
    link, clients, _ = _link(tls)
    clients[-1].connack()
    link.stop()
    assert clients[-1].stopped and not link.connected


def test_a_key_that_does_not_match_the_certificate_is_a_config_error(tls):
    ca, cert, _ = tls
    other_key, _ = issue(make_ca(), DEVICE)
    link = link_module.Link(DEVICE, Zugang(cert, Endpoint("mosquitto", 8883, ca)), other_key, lambda *event: None,
                            client_factory=FakePaho)
    with pytest.raises(TransportConfigError):
        link.start()


def test_a_rejected_connack_followed_by_disconnect_counts_once(tls):
    """Echtes paho: auf ein abgelehntes CONNACK folgt on_disconnect."""
    link, clients, events = _link(tls)
    client = clients[-1]
    client.connack(failure=True)
    client.drop()
    assert link.connect_failures == 1 and events == []
    for _ in range(2):
        client.connack(failure=True)
        client.drop()
    assert events == [("anmeldung_scheitert",)] and link.connect_failures == 3


def test_a_pending_port_switch_is_void_after_a_successful_connect(tls):
    link, clients, _ = _link(tls, alpn="x-amzn-mqtt-ca")
    for _ in range(link_module.PORT_FALLBACK_FAILURES):
        clients[-1].fail(OSError("Netz"))
    clients[-1].connack()
    link.port_wechseln_falls_noetig()
    assert len(clients) == 1 and link.port == 8883 and not clients[0].stopped


def test_a_failing_event_sink_does_not_escape_the_paho_callbacks(tls):
    def sink(*event):
        raise RuntimeError("Geraet kaputt")

    ca, cert, key = tls
    clients = []

    def factory(client_id):
        clients.append(FakePaho(client_id))
        return clients[-1]

    link = link_module.Link(DEVICE, Zugang(cert, Endpoint("mosquitto", 8883, ca)), key, sink, client_factory=factory)
    link.start()
    client = clients[-1]
    client.connack()
    client.message(f"smartheat/{DEVICE}/down/config")
    client.on_publish(client, None, 1, SimpleNamespace(is_failure=False), None)
    client.drop()
