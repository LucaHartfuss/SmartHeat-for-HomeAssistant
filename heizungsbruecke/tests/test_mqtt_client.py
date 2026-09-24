import logging
from unittest.mock import patch, MagicMock

from heizungsbruecke.mqtt_client import BridgeMqttClient


def test_init_authenticates_with_given_credentials():
    with patch("heizungsbruecke.mqtt_client.mqtt.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        BridgeMqttClient(host="127.0.0.1", port=18830, tenant_id="kunde2", username="u", password="p")

    mock_client.username_pw_set.assert_called_once_with("u", "p")


def test_publish_value_publishes_correct_topic_and_payload():
    with patch("heizungsbruecke.mqtt_client.mqtt.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        client = BridgeMqttClient(host="127.0.0.1", port=18830, tenant_id="kunde2", username="u", password="p")
        client.publish_value(role="room_actual", value=19.5, seq="abc123")

    mock_client.publish.assert_called_once_with(
        "smartheat/kunde2/up/room_actual", '{"v": 19.5, "seq": "abc123"}', qos=1
    )


def test_subscribe_down_subscribes_correct_topic():
    with patch("heizungsbruecke.mqtt_client.mqtt.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        client = BridgeMqttClient(host="127.0.0.1", port=18830, tenant_id="kunde2", username="u", password="p")
        callback = MagicMock()
        client.subscribe_down(role="curve_current", on_message=callback)

    mock_client.subscribe.assert_called_once_with("smartheat/kunde2/down/curve_current", 1)
    mock_client.message_callback_add.assert_called_once_with(
        "smartheat/kunde2/down/curve_current", callback
    )


def test_on_connect_resubscribes_previously_registered_roles():
    with patch("heizungsbruecke.mqtt_client.mqtt.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        client = BridgeMqttClient(host="127.0.0.1", port=18830, tenant_id="kunde2", username="u", password="p")
        curve_callback = MagicMock()
        offset_callback = MagicMock()
        client.subscribe_down(role="curve_current", on_message=curve_callback)
        client.subscribe_down(role="offset_current", on_message=offset_callback)

        mock_client.subscribe.reset_mock()
        mock_client.message_callback_add.reset_mock()

        # Simulate paho invoking on_connect again after a reconnect.
        on_connect = mock_client.on_connect
        on_connect(mock_client, None, {}, 0, None)

    mock_client.subscribe.assert_any_call("smartheat/kunde2/down/curve_current", 1)
    mock_client.subscribe.assert_any_call("smartheat/kunde2/down/offset_current", 1)
    mock_client.message_callback_add.assert_any_call(
        "smartheat/kunde2/down/curve_current", curve_callback
    )
    mock_client.message_callback_add.assert_any_call(
        "smartheat/kunde2/down/offset_current", offset_callback
    )
    assert mock_client.subscribe.call_count == 2
    assert mock_client.message_callback_add.call_count == 2


def test_on_connect_before_any_subscription_does_not_error():
    with patch("heizungsbruecke.mqtt_client.mqtt.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        BridgeMqttClient(host="127.0.0.1", port=18830, tenant_id="kunde2", username="u", password="p")

        on_connect = mock_client.on_connect
        on_connect(mock_client, None, {}, 0, None)  # must not raise

    mock_client.subscribe.assert_not_called()


def test_publish_discovery_publishes_retained_config_to_correct_topic():
    with patch("heizungsbruecke.mqtt_client.mqtt.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        client = BridgeMqttClient(host="127.0.0.1", port=18830, tenant_id="kunde2", username="u", password="p")
        client.publish_discovery(
            component="binary_sensor", object_id="failsafe", config={"name": "Fail-Safe"}
        )

    mock_client.publish.assert_called_once_with(
        "homeassistant/binary_sensor/heizungsbruecke_kunde2/failsafe/config",
        '{"name": "Fail-Safe"}',
        retain=True,
    )


def test_publish_status_publishes_retained_payload_to_correct_topic():
    with patch("heizungsbruecke.mqtt_client.mqtt.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        client = BridgeMqttClient(host="127.0.0.1", port=18830, tenant_id="kunde2", username="u", password="p")
        client.publish_status(object_id="failsafe", payload="ON")

    mock_client.publish.assert_called_once_with(
        "smartheat/kunde2/status/failsafe", "ON", retain=True
    )


def test_on_connect_republishes_discovery_configs():
    with patch("heizungsbruecke.mqtt_client.mqtt.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        client = BridgeMqttClient(host="127.0.0.1", port=18830, tenant_id="kunde2", username="u", password="p")
        client.publish_discovery(
            component="binary_sensor", object_id="failsafe", config={"name": "Fail-Safe"}
        )

        mock_client.publish.reset_mock()

        # Simulate paho invoking on_connect again after a reconnect.
        on_connect = mock_client.on_connect
        on_connect(mock_client, None, {}, 0, None)

    mock_client.publish.assert_any_call(
        "homeassistant/binary_sensor/heizungsbruecke_kunde2/failsafe/config",
        '{"name": "Fail-Safe"}',
        retain=True,
    )


def test_on_connect_republishes_last_status_payloads():
    with patch("heizungsbruecke.mqtt_client.mqtt.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        client = BridgeMqttClient(host="127.0.0.1", port=18830, tenant_id="kunde2", username="u", password="p")
        client.publish_status(object_id="failsafe", payload="ON")

        mock_client.publish.reset_mock()

        # Simulate paho invoking on_connect again after a reconnect (e.g. broker lost
        # retained messages across a restart without persistence).
        on_connect = mock_client.on_connect
        on_connect(mock_client, None, {}, 0, None)

    mock_client.publish.assert_any_call("smartheat/kunde2/status/failsafe", "ON", retain=True)


def test_init_registers_last_will_for_availability_topic():
    with patch("heizungsbruecke.mqtt_client.mqtt.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        BridgeMqttClient(host="127.0.0.1", port=18830, tenant_id="kunde2", username="u", password="p")

    mock_client.will_set.assert_called_once_with(
        "smartheat/kunde2/status/availability", payload="offline", retain=True
    )


def test_on_connect_publishes_online_to_availability_topic():
    with patch("heizungsbruecke.mqtt_client.mqtt.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        BridgeMqttClient(host="127.0.0.1", port=18830, tenant_id="kunde2", username="u", password="p")

        mock_client.publish.reset_mock()

        on_connect = mock_client.on_connect
        on_connect(mock_client, None, {}, 0, None)

    mock_client.publish.assert_any_call(
        "smartheat/kunde2/status/availability", "online", retain=True
    )


def test_publish_value_uses_qos_1():
    with patch("heizungsbruecke.mqtt_client.mqtt.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        client = BridgeMqttClient(host="127.0.0.1", port=18830, tenant_id="kunde2", username="u", password="p")
        client.publish_value(role="curve_current", value=0.8, seq="tick-1")

        mock_client.publish.assert_called_once_with(
            "smartheat/kunde2/up/curve_current", '{"v": 0.8, "seq": "tick-1"}', qos=1
        )


def test_subscribe_down_uses_qos_1():
    with patch("heizungsbruecke.mqtt_client.mqtt.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        client = BridgeMqttClient(host="127.0.0.1", port=18830, tenant_id="kunde2", username="u", password="p")
        client.subscribe_down(role="curve_current", on_message=lambda *a: None)

        mock_client.subscribe.assert_called_once_with("smartheat/kunde2/down/curve_current", 1)


def test_on_disconnect_logs_warning_with_reason_code(monkeypatch, caplog):
    fake_paho_client = MagicMock()
    monkeypatch.setattr("heizungsbruecke.mqtt_client.mqtt.Client", lambda *a, **kw: fake_paho_client)

    client = BridgeMqttClient(host="127.0.0.1", port=18830, tenant_id="t1", username="u", password="p")

    with caplog.at_level(logging.WARNING):
        client._on_disconnect(fake_paho_client, None, None, 7, None)

    assert "getrennt" in caplog.text.lower() or "disconnect" in caplog.text.lower()
    assert "7" in caplog.text


def test_publish_telemetry_publishes_correct_topic_payload_and_not_retained():
    with patch("heizungsbruecke.mqtt_client.mqtt.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        client = BridgeMqttClient(host="127.0.0.1", port=18830, tenant_id="kunde2", username="u", password="p")
        client.publish_telemetry({
            "room_actual": 20.5, "boost_active": False, "failsafe_active": False,
            "ts": "2026-09-18T08:00:00Z",
        })

    mock_client.publish.assert_called_once_with(
        "smartheat/kunde2/telemetry",
        '{"room_actual": 20.5, "boost_active": false, "failsafe_active": false, "ts": "2026-09-18T08:00:00Z"}',
        qos=1,
    )


def test_publish_value_includes_trigger_when_given():
    with patch("heizungsbruecke.mqtt_client.mqtt.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        client = BridgeMqttClient(host="127.0.0.1", port=18830, tenant_id="kunde2", username="u", password="p")
        client.publish_value(role="heat_limit", value=16.0, seq="s1", trigger="daily")
    mock_client.publish.assert_called_once_with(
        "smartheat/kunde2/up/heat_limit", '{"v": 16.0, "seq": "s1", "trigger": "daily"}', qos=1
    )
