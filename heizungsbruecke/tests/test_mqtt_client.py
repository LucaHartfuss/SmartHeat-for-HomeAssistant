from unittest.mock import patch, MagicMock

from heizungsbruecke.mqtt_client import BridgeMqttClient


def test_publish_value_publishes_correct_topic_and_payload():
    with patch("heizungsbruecke.mqtt_client.mqtt.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        client = BridgeMqttClient(host="127.0.0.1", port=18830, tenant_id="kunde2")
        client.publish_value(role="room_actual", value=19.5, seq="abc123")

    mock_client.publish.assert_called_once_with(
        "smartheat/kunde2/up/room_actual", '{"v": 19.5, "seq": "abc123"}'
    )


def test_subscribe_down_subscribes_correct_topic():
    with patch("heizungsbruecke.mqtt_client.mqtt.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        client = BridgeMqttClient(host="127.0.0.1", port=18830, tenant_id="kunde2")
        callback = MagicMock()
        client.subscribe_down(role="curve_current", on_message=callback)

    mock_client.subscribe.assert_called_once_with("smartheat/kunde2/down/curve_current")
    mock_client.message_callback_add.assert_called_once_with(
        "smartheat/kunde2/down/curve_current", callback
    )
