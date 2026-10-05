import logging
import ssl
from unittest.mock import MagicMock, patch

import pytest
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from smartheat_transport.connect import ConnectOptions
from smartheat_transport.descriptor import KIND_IOT_CORE, KIND_MOSQUITTO
from smartheat_transport.mqtt_client import BridgeMqttClient

PASSWORD_OPTIONS = ConnectOptions("127.0.0.1", 18830, "", username="u", password="p")


def _client(mock_paho_client, options=PASSWORD_OPTIONS, kind=KIND_MOSQUITTO, tenant="kunde2", **kwargs):
    with patch("smartheat_transport.mqtt_client.mqtt.Client", return_value=mock_paho_client) as client_cls:
        bridge = BridgeMqttClient(options, tenant, transport_kind=kind, **kwargs)
    return bridge, client_cls


def test_init_authenticates_with_given_credentials():
    with patch("smartheat_transport.mqtt_client.mqtt.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        BridgeMqttClient(PASSWORD_OPTIONS, "kunde2", transport_kind=KIND_MOSQUITTO)

    mock_client.username_pw_set.assert_called_once_with("u", "p")


def test_on_connect_before_any_subscription_does_not_error():
    with patch("smartheat_transport.mqtt_client.mqtt.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        BridgeMqttClient(PASSWORD_OPTIONS, "kunde2", transport_kind=KIND_MOSQUITTO)

        on_connect = mock_client.on_connect
        on_connect(mock_client, None, {}, 0, None)  # must not raise

    mock_client.subscribe.assert_not_called()


def test_on_disconnect_logs_warning_with_reason_code(monkeypatch, caplog):
    fake_paho_client = MagicMock()
    monkeypatch.setattr("smartheat_transport.mqtt_client.mqtt.Client", lambda *a, **kw: fake_paho_client)

    client = BridgeMqttClient(PASSWORD_OPTIONS, "t1", transport_kind=KIND_MOSQUITTO)

    with caplog.at_level(logging.WARNING):
        client._on_disconnect(fake_paho_client, None, None, 7, None)

    assert "getrennt" in caplog.text.lower() or "disconnect" in caplog.text.lower()
    assert "7" in caplog.text


def test_publish_telemetry_publishes_correct_topic_payload_and_not_retained():
    with patch("smartheat_transport.mqtt_client.mqtt.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        client = BridgeMqttClient(PASSWORD_OPTIONS, "kunde2", transport_kind=KIND_MOSQUITTO)
        client.publish_telemetry({
            "room_actual": 20.5, "boost_active": False, "failsafe_active": False,
            "ts": "2026-09-18T08:00:00Z",
        })

    mock_client.publish.assert_called_once_with(
        "smartheat/kunde2/telemetry",
        '{"room_actual": 20.5, "boost_active": false, "failsafe_active": false, "ts": "2026-09-18T08:00:00Z"}',
        qos=1,
    )


def _client_with_mock(**kwargs):
    patcher = patch("smartheat_transport.mqtt_client.mqtt.Client")
    mock_client_cls = patcher.start()
    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client
    client = BridgeMqttClient(PASSWORD_OPTIONS, "kunde2", transport_kind=KIND_MOSQUITTO, **kwargs)
    patcher.stop()
    return client, mock_client


def test_publish_snapshot_publishes_one_message_with_qos_1():
    client, mock_client = _client_with_mock()

    client.publish_snapshot({"schema": 4, "seq": "s1", "levers": {"curve": 1.2}})

    mock_client.publish.assert_called_once_with(
        "smartheat/kunde2/up/snapshot", '{"schema": 4, "seq": "s1", "levers": {"curve": 1.2}}', qos=1,
    )


def test_subscribe_setpoints_subscribes_single_topic_with_qos_1():
    client, mock_client = _client_with_mock()
    callback = MagicMock()

    client.subscribe_setpoints(on_message=callback)

    mock_client.message_callback_add.assert_called_once_with("smartheat/kunde2/down/setpoints", callback)
    mock_client.subscribe.assert_called_once_with("smartheat/kunde2/down/setpoints", 1)


def test_on_connect_resubscribes_setpoints():
    client, mock_client = _client_with_mock()
    callback = MagicMock()
    client.subscribe_setpoints(on_message=callback)
    mock_client.subscribe.reset_mock()
    mock_client.message_callback_add.reset_mock()

    client._on_connect(mock_client, None, {}, 0, None)

    mock_client.subscribe.assert_called_once_with("smartheat/kunde2/down/setpoints", 1)
    mock_client.message_callback_add.assert_called_once_with("smartheat/kunde2/down/setpoints", callback)


def test_on_connect_success_reason_code_object_subscribes_normally():
    client, mock_client = _client_with_mock()
    client.subscribe_setpoints(on_message=MagicMock())
    mock_client.subscribe.reset_mock()

    client._on_connect(mock_client, None, {}, ReasonCode(PacketTypes.CONNACK, identifier=0), None)

    mock_client.subscribe.assert_called_once()


@pytest.mark.parametrize("identifier", [134, 135])
def test_on_connect_auth_rejection_calls_hook_and_skips_subscribe(identifier):
    hook = MagicMock()
    client, mock_client = _client_with_mock(on_auth_rejected=hook)
    client.subscribe_setpoints(on_message=MagicMock())
    mock_client.subscribe.reset_mock()
    mock_client.publish.reset_mock()

    client._on_connect(mock_client, None, {}, ReasonCode(PacketTypes.CONNACK, identifier=identifier), None)

    hook.assert_called_once_with(client)
    mock_client.subscribe.assert_not_called()
    mock_client.publish.assert_not_called()


def test_on_connect_other_failure_does_not_call_auth_hook():
    hook = MagicMock()
    client, mock_client = _client_with_mock(on_auth_rejected=hook)

    client._on_connect(mock_client, None, {}, ReasonCode(PacketTypes.CONNACK, identifier=136), None)

    hook.assert_not_called()


def test_on_connect_auth_hook_exception_does_not_escape_network_thread(caplog):
    # Review Focus 4: paho 2.x unterdrueckt Callback-Exceptions nicht -- eine Exception
    # hier wuerde den Netzwerk-Thread beenden.
    hook = MagicMock(side_effect=RuntimeError("status-api kaputt"))
    client, mock_client = _client_with_mock(on_auth_rejected=hook)

    with caplog.at_level(logging.ERROR):
        client._on_connect(mock_client, None, {}, ReasonCode(PacketTypes.CONNACK, identifier=135), None)

    assert "status-api kaputt" in caplog.text


@pytest.mark.parametrize("identifier", [134, 135])
def test_auth_rejected_without_hook_only_logs(identifier, caplog):
    client, mock_client = _client_with_mock()

    with caplog.at_level(logging.ERROR):
        client._on_connect(mock_client, None, {}, ReasonCode(PacketTypes.CONNACK, identifier=identifier), None)

    assert "Anmeldung" in caplog.text and "abgelehnt" in caplog.text


def test_stop_disconnects_and_stops_loop():
    client, mock_client = _client_with_mock()

    client.stop()

    mock_client.disconnect.assert_called_once()
    mock_client.loop_stop.assert_called_once()


def test_init_connects_asynchronously_with_bounded_reconnect_backoff():
    _, mock_client = _client_with_mock()

    mock_client.connect_async.assert_called_once_with("127.0.0.1", 18830)
    mock_client.connect.assert_not_called()
    mock_client.reconnect_delay_set.assert_called_once_with(min_delay=1, max_delay=120)


def test_on_connect_fail_is_registered_and_logs_tunnel_hint(caplog):
    client, mock_client = _client_with_mock()

    assert mock_client.on_connect_fail == client._on_connect_fail
    with caplog.at_level(logging.ERROR):
        _fail(client, mock_client, ConnectionRefusedError())

    assert "cloudflared_access_mqtt" in caplog.text
    assert "18830" in caplog.text


def test_is_connected_asks_paho():
    client, mock_client = _client_with_mock()

    mock_client.is_connected.return_value = False
    assert client.is_connected() is False
    mock_client.is_connected.return_value = True
    assert client.is_connected() is True


def test_on_connect_calls_connected_hook_after_resubscribe():
    events = []
    client, mock_client = _client_with_mock(on_connected=lambda c: events.append(("hook", c)))
    client.subscribe_setpoints(on_message=MagicMock())
    mock_client.subscribe.side_effect = lambda *args: events.append(("subscribe", None))

    client._on_connect(mock_client, None, {}, 0, None)

    assert events == [("subscribe", None), ("hook", client)]


def test_on_connect_failure_does_not_call_connected_hook():
    hook = MagicMock()
    client, mock_client = _client_with_mock(on_connected=hook)

    client._on_connect(mock_client, None, {}, ReasonCode(PacketTypes.CONNACK, identifier=135), None)

    hook.assert_not_called()


def test_connected_hook_exception_does_not_escape_network_thread(caplog):
    client, mock_client = _client_with_mock(on_connected=MagicMock(side_effect=RuntimeError("worker kaputt")))

    with caplog.at_level(logging.ERROR):
        client._on_connect(mock_client, None, {}, 0, None)  # darf nicht werfen

    assert "worker kaputt" in caplog.text


def test_client_sets_no_last_will_and_publishes_nothing_on_connect():
    # B4/TP7: nichts mehr unter smartheat/<tenant>/status/ -- der Status laeuft seit 0.20.0
    # ueber die SmartHeat-Integration (Spec TP7 1.4), nicht mehr per Last Will/Discovery. Ein
    # von der ACL nicht erlaubtes Will-Topic wuerde den Connect nicht einmal ablehnen (siehe
    # tests/test_mosquitto_will_acl.sh) -- das war nie der Grund fuer diese Aenderung.
    _, mock_client = _client_with_mock()

    mock_client.on_connect(mock_client, None, {}, 0, None)

    mock_client.will_set.assert_not_called()
    mock_client.publish.assert_not_called()


def test_client_has_no_status_or_discovery_api():
    assert not hasattr(BridgeMqttClient, "publish_status")
    assert not hasattr(BridgeMqttClient, "publish_discovery")


def test_password_transport_logs_in_with_username_and_paho_client_id():
    paho = MagicMock()
    _, client_cls = _client(paho)
    assert client_cls.call_args.kwargs.get("client_id", "") == ""
    paho.username_pw_set.assert_called_once_with("u", "p")
    paho.connect_async.assert_called_once_with("127.0.0.1", 18830)


def test_certificate_transport_uses_tls_and_the_thing_name_as_client_id():
    paho = MagicMock()
    context = ssl.create_default_context()
    _, client_cls = _client(paho, ConnectOptions("e-ats.iot", 8883, "client1", ssl_context=context), KIND_IOT_CORE)
    assert client_cls.call_args.kwargs["client_id"] == "client1"
    paho.tls_set_context.assert_called_once_with(context)
    paho.username_pw_set.assert_not_called()


def _fail(bridge, paho, error):
    try:
        raise error
    except OSError:
        bridge._on_connect_fail(paho, None)  # paho ruft es im except-Block auf (loop_forever)


def test_connect_failures_are_counted_and_reset_by_a_connect():
    paho = MagicMock()
    bridge, _ = _client(paho)
    _fail(bridge, paho, ConnectionRefusedError())
    _fail(bridge, paho, ConnectionRefusedError())
    assert bridge.connect_failures == 2
    bridge._on_connect(paho, None, {}, 0, None)
    assert bridge.connect_failures == 0


def test_tls_failure_is_logged_as_tls_without_the_cloudflared_hint(caplog):
    paho = MagicMock()
    bridge, _ = _client(paho, ConnectOptions("e-ats.iot", 8883, "client1", ssl_context=ssl.create_default_context()),
                        KIND_IOT_CORE)
    with caplog.at_level(logging.ERROR):
        _fail(bridge, paho, ssl.SSLError("handshake failure"))
    assert "TLS" in caplog.text and "cloudflared" not in caplog.text


def test_network_failure_on_mosquitto_names_the_cloudflared_add_on(caplog):
    paho = MagicMock()
    bridge, _ = _client(paho)
    with caplog.at_level(logging.ERROR):
        _fail(bridge, paho, ConnectionRefusedError())
    assert "Netzwerk" in caplog.text and "cloudflared_access_mqtt" in caplog.text


def test_auth_rejection_is_logged_differently_from_connect_failures(caplog):
    paho = MagicMock()
    rejected = []
    bridge, _ = _client(paho, on_auth_rejected=rejected.append)
    with caplog.at_level(logging.ERROR):
        bridge._on_connect(paho, None, {}, ReasonCode(PacketTypes.CONNACK, identifier=135), None)
    assert rejected == [bridge] and "Anmeldung" in caplog.text and bridge.connect_failures == 0


def test_disconnect_without_a_successful_connack_counts_as_a_failed_attempt():
    """Ein Zertifikat, das IoT Core nach dem TLS-1.3-Handshake ablehnt (z. B. gesperrt), zeigt sich als
    Trennung statt als on_connect_fail -- ohne diese Zaehlung wuerde die Schwelle der Abo-Erkennung nie erreicht."""
    paho = MagicMock()
    bridge, _ = _client(paho)
    bridge._on_disconnect(paho, None, None, ReasonCode(PacketTypes.DISCONNECT, identifier=0), None)
    bridge._on_disconnect(paho, None, None, ReasonCode(PacketTypes.DISCONNECT, identifier=0), None)
    assert bridge.connect_failures == 2


def test_disconnect_after_a_successful_connack_does_not_count():
    paho = MagicMock()
    bridge, _ = _client(paho)
    bridge._on_connect(paho, None, {}, 0, None)
    bridge._on_disconnect(paho, None, None, ReasonCode(PacketTypes.DISCONNECT, identifier=0), None)
    assert bridge.connect_failures == 0


def test_a_new_attempt_after_a_normal_disconnect_counts_again_and_a_connack_resets():
    paho = MagicMock()
    bridge, _ = _client(paho)
    bridge._on_connect(paho, None, {}, 0, None)
    bridge._on_disconnect(paho, None, None, 0, None)      # Verbindung war da: zaehlt nicht
    bridge._on_disconnect(paho, None, None, 0, None)      # Versuch ohne CONNACK danach: zaehlt
    assert bridge.connect_failures == 1
    bridge._on_connect(paho, None, {}, 0, None)
    assert bridge.connect_failures == 0
