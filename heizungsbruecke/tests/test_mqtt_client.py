import logging
from unittest.mock import patch, MagicMock

import pytest
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from heizungsbruecke.mqtt_client import BridgeMqttClient


def test_init_authenticates_with_given_credentials():
    with patch("heizungsbruecke.mqtt_client.mqtt.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        BridgeMqttClient(host="127.0.0.1", port=18830, tenant_id="kunde2", username="u", password="p")

    mock_client.username_pw_set.assert_called_once_with("u", "p")


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


def _client_with_mock(**kwargs):
    patcher = patch("heizungsbruecke.mqtt_client.mqtt.Client")
    mock_client_cls = patcher.start()
    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client
    client = BridgeMqttClient(host="127.0.0.1", port=18830, tenant_id="kunde2", username="u", password="p", **kwargs)
    patcher.stop()
    return client, mock_client


def test_publish_snapshot_publishes_one_message_with_qos_1():
    client, mock_client = _client_with_mock()

    client.publish_snapshot({"schema": 2, "seq": "s1", "roles": {"dat": 8.2}})

    mock_client.publish.assert_called_once_with(
        "smartheat/kunde2/up/snapshot", '{"schema": 2, "seq": "s1", "roles": {"dat": 8.2}}', qos=1,
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


def test_stop_disconnects_and_stops_loop():
    client, mock_client = _client_with_mock()

    client.stop()

    mock_client.disconnect.assert_called_once()
    mock_client.loop_stop.assert_called_once()


def test_stop_publishes_offline_availability_before_disconnect():
    # Final-Review Minor 2: ein sauberes disconnect() loest den Last Will nicht aus --
    # stop() muss "offline" selbst veroeffentlichen (gleiches Topic/Payload/Retain wie LWT).
    client, mock_client = _client_with_mock()
    will_args = mock_client.will_set.call_args
    mock_client.reset_mock()

    client.stop()

    assert will_args.args[0] == "smartheat/kunde2/status/availability"
    assert will_args.kwargs == {"payload": "offline", "retain": True}
    names = [name for name, _, _ in mock_client.mock_calls]
    assert names == ["publish", "disconnect", "loop_stop"]
    mock_client.publish.assert_called_once_with("smartheat/kunde2/status/availability", "offline", retain=True)


def test_stop_still_disconnects_when_offline_publish_fails():
    client, mock_client = _client_with_mock()
    mock_client.publish.side_effect = RuntimeError("nicht verbunden")

    client.stop()  # darf nicht werfen

    mock_client.disconnect.assert_called_once()
    mock_client.loop_stop.assert_called_once()
