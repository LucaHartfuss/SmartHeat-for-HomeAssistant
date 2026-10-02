import logging
from unittest.mock import MagicMock, patch

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

    assert "abgelehnt" in caplog.text


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
        client._on_connect_fail(mock_client, None)

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
