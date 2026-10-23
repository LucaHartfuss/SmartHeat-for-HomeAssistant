import json
import socket
import threading
from unittest.mock import MagicMock, Mock, patch

import pytest
import requests
import websocket
from fakes import HaWebSocket, ha_antwort

from heizungsbruecke.ha_api import HomeAssistantApi
from heizungsbruecke.ha_signals import HaSignalSource
from smartheat_runtime.ports import SignalUngueltig, SourceUnavailable

FLOW_URL = "http://supervisor/core/api/config/config_entries/flow"


def test_get_state_returns_parsed_float():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(spec=requests.Response)
    mock_response.json.return_value = {"state": "21.5"}
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.get", autospec=True, return_value=mock_response) as mock_get:
        result = api.get_state("climate.wohnzimmer_thermostat")

    assert result == 21.5
    mock_get.assert_called_once_with(
        "http://supervisor/core/api/states/climate.wohnzimmer_thermostat",
        headers={"Authorization": "Bearer test-token"},
        timeout=10,
    )


def test_get_raw_state_returns_string_state_without_float_cast():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(spec=requests.Response)
    mock_response.json.return_value = {"state": "heating"}
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.get", autospec=True, return_value=mock_response) as mock_get:
        result = api.get_raw_state("sensor.mode")

    assert result == "heating"
    mock_get.assert_called_once_with(
        "http://supervisor/core/api/states/sensor.mode",
        headers={"Authorization": "Bearer test-token"},
        timeout=10,
    )


@pytest.mark.parametrize("bad_state", ["unavailable", "unknown", ""])
def test_get_raw_state_raises_on_ha_failure_states(bad_state):
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(spec=requests.Response)
    mock_response.json.return_value = {"state": bad_state}
    mock_response.raise_for_status.return_value = None

    with (patch("heizungsbruecke.ha_api.requests.get", autospec=True, return_value=mock_response),
          pytest.raises(SignalUngueltig)):  # erwartet ungueltig, kein Programmfehler
        api.get_raw_state("sensor.mode")


def test_get_raw_state_rejects_attribute_reference_without_http_call():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")

    with patch("heizungsbruecke.ha_api.requests.get", autospec=True) as mock_get, pytest.raises(ValueError) as caught:
        api.get_raw_state("climate.x::hvac_action")

    mock_get.assert_not_called()
    assert not isinstance(caught.value, SignalUngueltig)  # Programmfehler, kein ungueltiger Wert


def test_get_state_with_attribute_reference_reads_named_attribute():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(spec=requests.Response)
    mock_response.json.return_value = {
        "state": "heat",
        "attributes": {"current_temperature": 19.5, "temperature": 21.0},
    }
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.get", autospec=True, return_value=mock_response) as mock_get:
        result = api.get_state("climate.wohnzimmer_thermostat::current_temperature")

    assert result == 19.5
    mock_get.assert_called_once_with(
        "http://supervisor/core/api/states/climate.wohnzimmer_thermostat",
        headers={"Authorization": "Bearer test-token"},
        timeout=10,
    )


@pytest.mark.parametrize("huge", [10**400, -10**400], ids=["gross-positiv", "gross-negativ"])
def test_get_state_with_attribute_beyond_float_range_is_a_value_error(huge):
    # JSON-Ganzzahl ausserhalb des float-Bereichs: wie jeder unbrauchbare Wert ein ValueError, kein OverflowError
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(spec=requests.Response)
    mock_response.json.return_value = {"state": "heat", "attributes": {"temperature": huge}}
    mock_response.raise_for_status.return_value = None

    with (patch("heizungsbruecke.ha_api.requests.get", autospec=True, return_value=mock_response),
          pytest.raises(SignalUngueltig)):
        api.get_state("climate.wohnzimmer_thermostat::temperature")


def test_get_state_with_target_attribute_reference_reads_temperature():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(spec=requests.Response)
    mock_response.json.return_value = {
        "state": "heat",
        "attributes": {"current_temperature": 19.5, "temperature": 21.0},
    }
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.get", autospec=True, return_value=mock_response):
        result = api.get_state("climate.wohnzimmer_thermostat::temperature")

    assert result == 21.0


def test_set_number_value_posts_correct_payload():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(spec=requests.Response)
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.post", autospec=True, return_value=mock_response) as mock_post:
        api.set_number_value("number.weishaupt_heizkurve_steigung", 0.72)

    mock_post.assert_called_once_with(
        "http://supervisor/core/api/services/number/set_value",
        headers={"Authorization": "Bearer test-token"},
        json={"entity_id": "number.weishaupt_heizkurve_steigung", "value": 0.72},
        timeout=(10, 60),
    )


def test_send_notification_posts_to_the_split_notify_service():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(spec=requests.Response)
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.post", autospec=True, return_value=mock_response) as mock_post:
        api.send_notification("notify.mobile_app_lucas_iphone", "Sensor defekt")

    mock_post.assert_called_once_with(
        "http://supervisor/core/api/services/notify/mobile_app_lucas_iphone",
        headers={"Authorization": "Bearer test-token"},
        json={"message": "Sensor defekt"},
        timeout=10,
    )


def test_send_notification_raises_on_http_error():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(spec=requests.Response)
    mock_response.raise_for_status.side_effect = RuntimeError("500")

    with patch("heizungsbruecke.ha_api.requests.post", autospec=True, return_value=mock_response), pytest.raises(RuntimeError):
        api.send_notification("notify.mobile_app_lucas_iphone", "Sensor defekt")


def test_get_state_respects_custom_api_prefix():
    api = HomeAssistantApi(base_url="http://localhost:18213", token="test-token", api_prefix="/api")
    mock_response = Mock(spec=requests.Response)
    mock_response.json.return_value = {"state": "21.5"}
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.get", autospec=True, return_value=mock_response) as mock_get:
        result = api.get_state("sensor.test")

    assert result == 21.5
    mock_get.assert_called_once_with(
        "http://localhost:18213/api/states/sensor.test",
        headers={"Authorization": "Bearer test-token"},
        timeout=10,
    )


def test_websocket_url_appends_websocket_path_to_http_base_url():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")

    assert api.websocket_url() == "ws://supervisor/core/api/websocket"


def test_websocket_url_uses_wss_scheme_for_https_base_url():
    api = HomeAssistantApi(base_url="https://ha.example.com", token="test-token")

    assert api.websocket_url() == "wss://ha.example.com/core/api/websocket"


def test_websocket_url_respects_custom_api_prefix():
    api = HomeAssistantApi(base_url="http://localhost:18213", token="test-token", api_prefix="/api")

    assert api.websocket_url() == "ws://localhost:18213/api/websocket"


def test_token_property_returns_configured_token():
    api = HomeAssistantApi(base_url="http://supervisor", token="secret-tok-123")

    assert api.token == "secret-tok-123"


def test_call_ws_command_completes_auth_handshake_and_returns_result():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    ws = MagicMock(spec=websocket.WebSocket)
    ws.recv.side_effect = [
        json.dumps({"type": "auth_required"}),
        json.dumps({"type": "auth_ok"}),
        json.dumps({"id": 1, "success": True, "result": {"id": "abc123"}}),
    ]

    with patch("heizungsbruecke.ha_api.websocket.create_connection", autospec=True, return_value=ws) as mock_connect:
        result = api.ws_command({"type": "input_number/create"})

    assert result == {"id": "abc123"}
    mock_connect.assert_called_once_with("ws://supervisor/core/api/websocket", timeout=10)
    ws.send.assert_any_call(json.dumps({"type": "auth", "access_token": "test-token"}))
    ws.send.assert_any_call(json.dumps({"id": 1, "type": "input_number/create"}))
    ws.close.assert_called_once()


def test_call_ws_command_raises_when_first_message_is_not_auth_required():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    ws = MagicMock(spec=websocket.WebSocket)
    ws.recv.side_effect = [json.dumps({"type": "event"})]

    with (
        patch("heizungsbruecke.ha_api.websocket.create_connection", autospec=True, return_value=ws),
        pytest.raises(RuntimeError, match="Unerwartete erste WS-Nachricht"),
    ):
        api.ws_command({"type": "x"})
    ws.close.assert_called_once()


def test_call_ws_command_raises_when_auth_fails():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    ws = MagicMock(spec=websocket.WebSocket)
    ws.recv.side_effect = [
        json.dumps({"type": "auth_required"}),
        json.dumps({"type": "auth_invalid"}),
    ]

    with (
        patch("heizungsbruecke.ha_api.websocket.create_connection", autospec=True, return_value=ws),
        pytest.raises(RuntimeError, match="WS-Authentifizierung fehlgeschlagen"),
    ):
        api.ws_command({"type": "x"})
    ws.close.assert_called_once()


def test_call_ws_command_raises_when_command_result_is_unsuccessful():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    ws = MagicMock(spec=websocket.WebSocket)
    ws.recv.side_effect = [
        json.dumps({"type": "auth_required"}),
        json.dumps({"type": "auth_ok"}),
        json.dumps({"id": 1, "success": False, "error": {"message": "nope"}}),
    ]

    with (
        patch("heizungsbruecke.ha_api.websocket.create_connection", autospec=True, return_value=ws),
        pytest.raises(RuntimeError, match="WS-Kommando fehlgeschlagen"),
    ):
        api.ws_command({"type": "x"})
    ws.close.assert_called_once()






def test_entity_exists_returns_true_when_state_found():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(spec=requests.Response, status_code=200)
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.get", autospec=True, return_value=mock_response):
        assert api.entity_exists("sensor.dat") is True


def test_entity_exists_returns_false_on_404():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(spec=requests.Response, status_code=404)

    with patch("heizungsbruecke.ha_api.requests.get", autospec=True, return_value=mock_response):
        assert api.entity_exists("sensor.missing") is False


def test_entity_exists_raises_on_other_http_error():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(spec=requests.Response, status_code=500)
    mock_response.raise_for_status.side_effect = RuntimeError("500")

    with patch("heizungsbruecke.ha_api.requests.get", autospec=True, return_value=mock_response), pytest.raises(RuntimeError):
        api.entity_exists("sensor.dat")


def test_start_config_flow_posts_handler_and_returns_json():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(spec=requests.Response)
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = {"type": "form", "flow_id": "f1", "data_schema": []}

    with patch("heizungsbruecke.ha_api.requests.post", autospec=True, return_value=mock_response) as mock_post:
        result = api._start_config_flow("statistics")

    assert result == {"type": "form", "flow_id": "f1", "data_schema": []}
    mock_post.assert_called_once_with(
        "http://supervisor/core/api/config/config_entries/flow",
        headers={"Authorization": "Bearer test-token"},
        json={"handler": "statistics", "show_advanced_options": False},
        timeout=10,
    )


def test_finish_config_flow_posts_data_to_flow_url():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(spec=requests.Response)
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = {"type": "create_entry", "result": {"entry_id": "e1"}}

    with patch("heizungsbruecke.ha_api.requests.post", autospec=True, return_value=mock_response) as mock_post:
        result = api._finish_config_flow("f1", {"name": "x"})

    assert result == {"type": "create_entry", "result": {"entry_id": "e1"}}
    mock_post.assert_called_once_with(
        "http://supervisor/core/api/config/config_entries/flow/f1",
        headers={"Authorization": "Bearer test-token"},
        json={"name": "x"},
        timeout=10,
    )


def test_advance_config_flow_filters_fields_per_step_until_flow_completes():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    all_fields = {
        "name": "SmartHeat DART", "entity_id": "sensor.room", "state_characteristic": "mean",
        "max_age": {"hours": 24}, "sampling_size": 255, "precision": 2,
    }
    step1_response = {
        "type": "form", "flow_id": "f1",
        "data_schema": [{"name": "name"}, {"name": "entity_id"}],
    }
    step2_response = {
        "type": "form", "flow_id": "f1",
        "data_schema": [{"name": "state_characteristic"}],
    }
    step3_response = {
        "type": "form", "flow_id": "f1",
        "data_schema": [{"name": "max_age"}, {"name": "sampling_size"}, {"name": "precision"}],
    }
    final_response = {"type": "create_entry", "result": {"entry_id": "e1"}}

    antworten = [ha_antwort(step2_response), ha_antwort(step3_response), ha_antwort(final_response)]
    with patch("heizungsbruecke.ha_api.requests.post", autospec=True, side_effect=antworten) as mock_post:
        result = api._advance_config_flow(step1_response, all_fields)

    assert result == final_response
    assert [(c.args[0], c.kwargs["json"]) for c in mock_post.call_args_list] == [
        (f"{FLOW_URL}/f1", {"name": "SmartHeat DART", "entity_id": "sensor.room"}),
        (f"{FLOW_URL}/f1", {"state_characteristic": "mean"}),
        (f"{FLOW_URL}/f1", {"max_age": {"hours": 24}, "sampling_size": 255, "precision": 2}),
    ]


def test_advance_config_flow_returns_immediately_when_first_response_is_not_a_form():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    final_response = {"type": "create_entry", "result": {"entry_id": "e1"}}

    with patch("heizungsbruecke.ha_api.requests.post", autospec=True) as mock_post:
        result = api._advance_config_flow(final_response, {"name": "x"})

    assert result == final_response
    mock_post.assert_not_called()


def test_find_entity_by_config_entry_returns_matching_entity_id():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    entries = [
        {"entity_id": "sensor.other", "config_entry_id": "e0"},
        {"entity_id": "sensor.dart", "config_entry_id": "e1"},
    ]

    ha_ws = HaWebSocket(entries)
    with ha_ws.angeschlossen():
        result = api._find_entity_by_config_entry("e1")

    assert result == "sensor.dart"
    assert ha_ws.kommandos == [{"id": 1, "type": "config/entity_registry/list"}]


def test_find_entity_by_config_entry_raises_when_not_found():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")

    with HaWebSocket([]).angeschlossen(), pytest.raises(RuntimeError, match="Keine Entity"):
        api._find_entity_by_config_entry("missing")










def test_create_persistent_notification_posts_to_service():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(spec=requests.Response)
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.post", autospec=True, return_value=mock_response) as mock_post:
        api.create_persistent_notification("SmartHeat", "Abo inaktiv", "smartheat_abo_inaktiv")

    mock_post.assert_called_once_with(
        "http://supervisor/core/api/services/persistent_notification/create",
        headers={"Authorization": "Bearer test-token"},
        json={"title": "SmartHeat", "message": "Abo inaktiv", "notification_id": "smartheat_abo_inaktiv"},
        timeout=10,
    )


def test_create_persistent_notification_raises_on_http_error():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(spec=requests.Response)
    mock_response.raise_for_status.side_effect = RuntimeError("500")

    with patch("heizungsbruecke.ha_api.requests.post", autospec=True, return_value=mock_response), pytest.raises(RuntimeError):
        api.create_persistent_notification("SmartHeat", "x", "smartheat_abo_inaktiv")


def test_dismiss_persistent_notification_posts_to_service():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(spec=requests.Response)
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.post", autospec=True, return_value=mock_response) as mock_post:
        api.dismiss_persistent_notification("smartheat_notbetrieb")

    mock_post.assert_called_once_with(
        "http://supervisor/core/api/services/persistent_notification/dismiss",
        headers={"Authorization": "Bearer test-token"},
        json={"notification_id": "smartheat_notbetrieb"},
        timeout=10,
    )


def test_dismiss_persistent_notification_raises_on_http_error():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(spec=requests.Response)
    mock_response.raise_for_status.side_effect = requests.HTTPError("500")

    with (
        patch("heizungsbruecke.ha_api.requests.post", autospec=True, return_value=mock_response),
        pytest.raises(requests.HTTPError),
    ):
        api.dismiss_persistent_notification("smartheat_notbetrieb")


def test_get_config_returns_parsed_json():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(spec=requests.Response)
    mock_response.json.return_value = {"time_zone": "Europe/Berlin"}
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.get", autospec=True, return_value=mock_response) as mock_get:
        result = api.get_config()

    assert result == {"time_zone": "Europe/Berlin"}
    mock_get.assert_called_once_with(
        "http://supervisor/core/api/config", headers={"Authorization": "Bearer test-token"}, timeout=10,
    )


def test_get_config_raises_on_http_error():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(spec=requests.Response)
    mock_response.raise_for_status.side_effect = requests.HTTPError("502")

    with (
        patch("heizungsbruecke.ha_api.requests.get", autospec=True, return_value=mock_response),
        pytest.raises(requests.HTTPError),
    ):
        api.get_config()


def test_advance_config_flow_follows_a_menu_step_then_the_form():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    menu = {"type": "menu", "flow_id": "f1", "menu_options": ["binary_sensor", "sensor"]}
    form = {"type": "form", "flow_id": "f1", "data_schema": [{"name": "name"}, {"name": "state"}]}
    done = {"type": "create_entry", "result": {"entry_id": "e1"}}
    fields = {"next_step_id": "sensor", "name": "N", "state": "{{ 1 }}", "unit_of_measurement": "°C"}

    with patch("heizungsbruecke.ha_api.requests.post", autospec=True,
               side_effect=[ha_antwort(form), ha_antwort(done)]) as mock_post:
        result = api._advance_config_flow(menu, fields)

    assert result == done
    assert [(c.args[0], c.kwargs["json"]) for c in mock_post.call_args_list] == [
        (f"{FLOW_URL}/f1", {"next_step_id": "sensor"}),
        (f"{FLOW_URL}/f1", {"name": "N", "state": "{{ 1 }}"}),
    ]


def test_create_template_sensor_runs_template_flow_with_temperature_fields():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    menu = {"type": "menu", "flow_id": "f1", "menu_options": ["binary_sensor", "sensor"]}
    form = {"type": "form", "flow_id": "f1", "data_schema": [
        {"name": "name"}, {"name": "state"}, {"name": "unit_of_measurement"}, {"name": "device_class"},
        {"name": "state_class"}, {"name": "device_id"},
    ]}
    done = {"type": "create_entry", "result": {"entry_id": "e7"}}
    ha_ws = HaWebSocket([{"entity_id": "sensor.other", "config_entry_id": "e1"},
                          {"entity_id": "sensor.smartheat_t1_raumtemperatur", "config_entry_id": "e7"}])
    with patch("heizungsbruecke.ha_api.requests.post", autospec=True,
               side_effect=[ha_antwort(menu), ha_antwort(form), ha_antwort(done)]) as mock_post, ha_ws.angeschlossen():
        entity_id = api.create_template_sensor(name="SmartHeat t1 Raumtemperatur", template="{{ 20 }}")

    assert entity_id == "sensor.smartheat_t1_raumtemperatur"
    assert [(c.args[0], c.kwargs["json"]) for c in mock_post.call_args_list] == [
        (FLOW_URL, {"handler": "template", "show_advanced_options": False}),
        (f"{FLOW_URL}/f1", {"next_step_id": "sensor"}),
        (f"{FLOW_URL}/f1", {"name": "SmartHeat t1 Raumtemperatur", "state": "{{ 20 }}", "unit_of_measurement": "°C",
                            "device_class": "temperature", "state_class": "measurement"}),
    ]
    assert ha_ws.kommandos == [{"id": 1, "type": "config/entity_registry/list"}]


def test_delete_helper_deletes_the_config_entry_of_the_entity():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    registry = [
        {"entity_id": "sensor.other", "config_entry_id": "e1", "platform": "mypyllant"},
        {"entity_id": "sensor.smartheat_t1_dart", "config_entry_id": "e2", "platform": "template"},
    ]
    mock_response = Mock(spec=requests.Response)
    mock_response.raise_for_status.return_value = None
    with HaWebSocket(registry).angeschlossen(), \
         patch("heizungsbruecke.ha_api.requests.delete", autospec=True, return_value=mock_response) as mock_delete:
        api.delete_helper("sensor.smartheat_t1_dart")

    mock_delete.assert_called_once_with(
        "http://supervisor/core/api/config/config_entries/entry/e2",
        headers={"Authorization": "Bearer test-token"}, timeout=10,
    )


def test_delete_helper_raises_for_entity_without_config_entry():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    registry = [{"entity_id": "sensor.smartheat_t1_x", "config_entry_id": None, "platform": "template"}]
    with HaWebSocket(registry).angeschlossen(), \
         patch("heizungsbruecke.ha_api.requests.delete", autospec=True) as mock_delete, pytest.raises(RuntimeError):
        api.delete_helper("sensor.smartheat_t1_x")

    mock_delete.assert_not_called()


@pytest.mark.parametrize("platform", ["mypyllant", "input_number", None])
def test_delete_helper_refuses_config_entries_of_other_integrations(platform):
    """Eine verwechselte/fremde Entity-ID (derived_sensors.json kaputt oder von einer anderen
    Installation) darf nie den Config-Entry einer anderen Integration loeschen, z.B. mypyllant."""
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    entry = {"entity_id": "sensor.smartheat_t1_dart", "config_entry_id": "e_vaillant"}
    if platform is not None:
        entry["platform"] = platform
    with (
        HaWebSocket([entry]).angeschlossen(),
        patch("heizungsbruecke.ha_api.requests.delete", autospec=True) as mock_delete,
        pytest.raises(RuntimeError, match="kein SmartHeat-Hilfssensor"),
    ):
        api.delete_helper("sensor.smartheat_t1_dart")

    mock_delete.assert_not_called()


@pytest.mark.parametrize("entity_id", ["sensor.fremder_helfer", "sensor.", "smartheat_t1_dart"])
def test_delete_helper_refuses_entities_without_smartheat_prefix(entity_id):
    """Nur Entities mit dem Objekt-Teil-Praefix `smartheat_`, auch wenn
    eine fremde Entity zufaellig auf der Plattform template liegt."""
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    ha_ws = HaWebSocket([{"entity_id": entity_id, "config_entry_id": "e_fremd", "platform": "template"}])
    with (
        ha_ws.angeschlossen(),
        patch("heizungsbruecke.ha_api.requests.delete", autospec=True) as mock_delete,
        pytest.raises(RuntimeError, match="kein SmartHeat-Hilfssensor"),
    ):
        api.delete_helper(entity_id)

    assert ha_ws.urls == []  # keine WebSocket-Verbindung
    mock_delete.assert_not_called()


def test_fire_event_posts_the_event_data():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(spec=requests.Response)
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.post", autospec=True, return_value=mock_response) as mock_post:
        api.fire_event("smartheat_status", {"schema": 1, "status": "regelt"})

    mock_post.assert_called_once_with(
        "http://supervisor/core/api/events/smartheat_status",
        headers={"Authorization": "Bearer test-token"},
        json={"schema": 1, "status": "regelt"},
        timeout=10,
    )


def test_fire_event_raises_on_http_error():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(spec=requests.Response)
    mock_response.raise_for_status.side_effect = RuntimeError("401")

    with patch("heizungsbruecke.ha_api.requests.post", autospec=True, return_value=mock_response), pytest.raises(RuntimeError):
        api.fire_event("smartheat_status", {})


def test_is_reachable_true_when_ha_is_running():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    with patch("heizungsbruecke.ha_api.requests.get", autospec=True,
               return_value=ha_antwort({"time_zone": "UTC", "state": "RUNNING"})) as mock_get:
        assert api.is_reachable() is True
    assert mock_get.call_args.args == ("http://supervisor/core/api/config",)


@pytest.mark.parametrize("config", [
    {"state": "NOT_RUNNING"}, {"state": "STARTING"}, {"state": "STOPPING"}, {"time_zone": "UTC"}, [], None,
])
def test_is_reachable_false_while_ha_is_not_running(config):
    """HAs HTTP-Server antwortet schon frueh im Bootstrap; Integrationen wie mypyllant laden erst
    danach. Das Start-Budget darf erst bei RUNNING zaehlen (sonst Startfehler bei langsamer Cloud)."""
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    with patch("heizungsbruecke.ha_api.requests.get", autospec=True, return_value=ha_antwort(config)):
        assert api.is_reachable() is False


@pytest.mark.parametrize("fehler", ["verbindung", "502", "401 falsches Token", "kein JSON"])
def test_is_reachable_false_on_any_error(fehler):
    # Der Koerper meldet RUNNING: nur der Fehler macht die Antwort unbrauchbar.
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    antwort = ha_antwort({"time_zone": "UTC", "state": "RUNNING"})
    if fehler in ("502", "401 falsches Token"):
        antwort.raise_for_status.side_effect = requests.HTTPError(fehler)
    elif fehler == "kein JSON":
        antwort.json.side_effect = ValueError("kein JSON")
    get = {"side_effect": requests.ConnectionError("weg")} if fehler == "verbindung" else {"return_value": antwort}
    with patch("heizungsbruecke.ha_api.requests.get", autospec=True, **get):
        assert api.is_reachable() is False


def test_set_climate_temperature_posts_correct_payload():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(spec=requests.Response)
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.post", autospec=True, return_value=mock_response) as mock_post:
        api.set_climate_temperature("climate.zone_1", 20.5)

    mock_post.assert_called_once_with(
        "http://supervisor/core/api/services/climate/set_temperature",
        headers={"Authorization": "Bearer test-token"},
        json={"entity_id": "climate.zone_1", "temperature": 20.5},
        timeout=(10, 60),
    )


def test_set_hvac_mode_posts_correct_payload():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(spec=requests.Response)
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.post", autospec=True, return_value=mock_response) as mock_post:
        api.set_hvac_mode("climate.zone_1", "heat_cool")

    mock_post.assert_called_once_with(
        "http://supervisor/core/api/services/climate/set_hvac_mode",
        headers={"Authorization": "Bearer test-token"},
        json={"entity_id": "climate.zone_1", "hvac_mode": "heat_cool"},
        timeout=(10, 60),
    )


@pytest.mark.parametrize("platform", ["statistics", "mypyllant"])
def test_delete_helper_refuses_every_platform_but_template(platform):
    """Das Add-on legt nur Template-Helfer an: auch ein Config-Entry der Plattform statistics mit SmartHeat-Namen wird
    nicht geloescht."""
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    registry = [{"entity_id": "sensor.smartheat_t1_dart", "config_entry_id": "e2", "platform": platform}]
    with (
        HaWebSocket(registry).angeschlossen(),
        patch("heizungsbruecke.ha_api.requests.delete", autospec=True) as mock_delete,
        pytest.raises(RuntimeError, match="kein SmartHeat-Hilfssensor"),
    ):
        api.delete_helper("sensor.smartheat_t1_dart")

    mock_delete.assert_not_called()


def test_delete_helper_deletes_a_renamed_helper_by_its_config_entry_title():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    registry = [{"entity_id": "sensor.heizraum_zuhause_smartheat_t1_dat", "config_entry_id": "e2",
                 "platform": "template"}]
    entries = [{"entry_id": "e1", "title": "Zuhause"}, {"entry_id": "e2", "title": "SmartHeat t1 DAT"}]
    mock_response = Mock(spec=requests.Response)
    mock_response.raise_for_status.return_value = None
    ha_ws = HaWebSocket(registry, entries)
    with ha_ws.angeschlossen(), \
         patch("heizungsbruecke.ha_api.requests.delete", autospec=True, return_value=mock_response) as mock_delete:
        api.delete_helper("sensor.heizraum_zuhause_smartheat_t1_dat", tenant_id="t1")

    assert [k["type"] for k in ha_ws.kommandos] == ["config/entity_registry/list", "config_entries/get"]
    mock_delete.assert_called_once_with(
        "http://supervisor/core/api/config/config_entries/entry/e2",
        headers={"Authorization": "Bearer test-token"}, timeout=10,
    )


@pytest.mark.parametrize("title", ["Wohnzimmer Statistik", "SmartHeat t2 DAT", "SmartHeat t1", None])
def test_delete_helper_refuses_a_renamed_helper_with_a_foreign_title(title):
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    registry = [{"entity_id": "sensor.umbenannt", "config_entry_id": "e2", "platform": "template"}]
    entries = [] if title is None else [{"entry_id": "e2", "title": title}]
    with (
        HaWebSocket(registry, entries).angeschlossen(),
        patch("heizungsbruecke.ha_api.requests.delete", autospec=True) as mock_delete,
        pytest.raises(RuntimeError, match="kein SmartHeat-Hilfssensor"),
    ):
        api.delete_helper("sensor.umbenannt", tenant_id="t1")

    mock_delete.assert_not_called()


# --- Weishaupt-Betriebsart und Viessmann-Heizprogramm ---

def test_select_option_posts_correct_payload():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(spec=requests.Response)
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.post", autospec=True, return_value=mock_response) as mock_post:
        api.select_option("select.betriebsart", "Normal")

    mock_post.assert_called_once_with(
        "http://supervisor/core/api/services/select/select_option",
        headers={"Authorization": "Bearer test-token"},
        json={"entity_id": "select.betriebsart", "option": "Normal"},
        timeout=(10, 60),
    )


def test_select_option_raises_on_http_error():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(spec=requests.Response)
    mock_response.raise_for_status.side_effect = requests.HTTPError("500")

    with patch("heizungsbruecke.ha_api.requests.post", autospec=True, return_value=mock_response), pytest.raises(requests.HTTPError):
        api.select_option("select.x", "Normal")


def test_set_preset_mode_posts_correct_payload():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(spec=requests.Response)
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.post", autospec=True, return_value=mock_response) as mock_post:
        api.set_preset_mode("climate.heizkreis", "home")

    mock_post.assert_called_once_with(
        "http://supervisor/core/api/services/climate/set_preset_mode",
        headers={"Authorization": "Bearer test-token"},
        json={"entity_id": "climate.heizkreis", "preset_mode": "home"},
        timeout=(10, 60),
    )


def test_set_preset_mode_raises_on_http_error():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(spec=requests.Response)
    mock_response.raise_for_status.side_effect = requests.HTTPError("500")

    with patch("heizungsbruecke.ha_api.requests.post", autospec=True, return_value=mock_response), pytest.raises(requests.HTTPError):
        api.set_preset_mode("climate.x", "home")


def test_get_attribute_returns_the_attribute_as_text():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(spec=requests.Response)
    mock_response.json.return_value = {"state": "heat", "attributes": {"preset_mode": "home"}}
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.get", autospec=True, return_value=mock_response) as mock_get:
        assert api.get_attribute("climate.heizkreis", "preset_mode") == "home"

    mock_get.assert_called_once_with(
        "http://supervisor/core/api/states/climate.heizkreis",
        headers={"Authorization": "Bearer test-token"},
        timeout=10,
    )


@pytest.mark.parametrize("attributes", [{}, {"preset_mode": None}, {"preset_mode": ""}])
def test_get_attribute_raises_when_missing_or_empty(attributes):
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(spec=requests.Response)
    mock_response.json.return_value = {"state": "heat", "attributes": attributes}
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.get", autospec=True, return_value=mock_response), pytest.raises(ValueError):
        api.get_attribute("climate.heizkreis", "preset_mode")


def test_registries_use_the_ws_command_and_ignore_other_answers():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    with HaWebSocket([{"entity_id": "sensor.a"}], [{"id": "d1"}]).angeschlossen():
        assert api.entity_registry() == [{"entity_id": "sensor.a"}]
        assert api.device_registry() == [{"id": "d1"}]
    with HaWebSocket({"unerwartet": 1}, {"unerwartet": 1}).angeschlossen():
        assert api.entity_registry() == []
        assert api.device_registry() == []


def test_registry_queries_go_out_as_websocket_commands():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    ha_ws = HaWebSocket([], [])
    with ha_ws.angeschlossen():
        api.entity_registry()
        api.device_registry()

    assert ha_ws.urls == ["ws://supervisor/core/api/websocket"] * 2  # je Abfrage eine kurzlebige Verbindung
    assert ha_ws.kommandos == [{"id": 1, "type": "config/entity_registry/list"},
                               {"id": 1, "type": "config/device_registry/list"}]


@pytest.mark.parametrize(("method", "path"), [("get_states", "states"), ("get_services", "services")])
def test_states_and_services_are_read_over_rest(method, path):
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(spec=requests.Response)
    mock_response.json.return_value = [{"x": 1}]
    mock_response.raise_for_status.return_value = None
    with patch("heizungsbruecke.ha_api.requests.get", autospec=True, return_value=mock_response) as mock_get:
        assert getattr(api, method)() == [{"x": 1}]
    mock_get.assert_called_once_with(f"http://supervisor/core/api/{path}", headers={"Authorization": "Bearer test-token"},
                                     timeout=10)
    mock_response.json.return_value = {"kein": "array"}
    with patch("heizungsbruecke.ha_api.requests.get", autospec=True, return_value=mock_response):
        assert getattr(api, method)() == []


@pytest.mark.parametrize("zustand", ["unavailable", "unknown", "heizt"])
def test_get_state_without_a_number_is_a_value_error(zustand):
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    with (
        patch("heizungsbruecke.ha_api.requests.get", autospec=True, return_value=ha_antwort({"state": zustand})),
        pytest.raises(SignalUngueltig, match=zustand),
    ):
        api.get_state("sensor.vorlauf")


# --- Zeitlimit und Abbruch auf den Leseaufrufen: eine definierte Ausnahme, am Port kein Lesewert ---

# Leseaufruf der HA-API -> derselbe Lesepfad am Port (HaSignalSource): get_unit liest das Attribut unit_of_measurement.
LESEAUFRUFE = {
    "get_state": (lambda api: api.get_state("sensor.vorlauf"), lambda port: port.get_state("sensor.vorlauf")),
    "get_raw_state": (lambda api: api.get_raw_state("sensor.vorlauf"), lambda port: port.get_raw_state("sensor.vorlauf")),
    "get_attribute": (lambda api: api.get_attribute("sensor.vorlauf", "unit_of_measurement"),
                      lambda port: port.get_unit("sensor.vorlauf")),
}


@pytest.mark.parametrize(("lesen", "am_port"), LESEAUFRUFE.values(), ids=LESEAUFRUFE.keys())
def test_a_time_limit_on_a_read_raises_timeout_and_gives_no_value(lesen, am_port):
    # So meldet requests das abgelaufene Lese-Zeitlimit (timeout=10); ein echtes Warten von 10 s ersetzt die Attrappe.
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    with patch("heizungsbruecke.ha_api.requests.get", autospec=True,
               side_effect=requests.ReadTimeout("Read timed out. (read timeout=10)")):
        with pytest.raises(requests.Timeout):
            lesen(api)
        with pytest.raises(SourceUnavailable):
            am_port(HaSignalSource(api))


@pytest.fixture
def abbrechender_server(request):
    """Echter HTTP-Server auf 127.0.0.1, der die Verbindung mitten in der Antwort schliesst: `ohne_antwort` gleich
    nach der Anfrage, `halber_koerper` nach dem Kopf und dem Anfang des Koerpers (Content-Length 64)."""
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(0.1)
    ende = threading.Event()

    def bedienen():
        while not ende.is_set():
            try:
                verbindung, _ = listener.accept()
            except TimeoutError:
                continue
            with verbindung:
                anfrage = b""
                while b"\r\n\r\n" not in anfrage:  # die ganze Anfrage lesen: Schliessen mit FIN, nicht RST
                    teil = verbindung.recv(65536)
                    if not teil:
                        break
                    anfrage += teil
                if request.param == "halber_koerper":
                    verbindung.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 64\r\n"
                                       b"\r\n{\"state\": \"2")

    thread = threading.Thread(target=bedienen, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    ende.set()
    thread.join(timeout=2)
    listener.close()


@pytest.mark.parametrize(("abbrechender_server", "fehler"), [
    ("ohne_antwort", requests.ConnectionError),
    ("halber_koerper", requests.exceptions.ChunkedEncodingError),
], indirect=["abbrechender_server"])
@pytest.mark.parametrize(("lesen", "am_port"), LESEAUFRUFE.values(), ids=LESEAUFRUFE.keys())
def test_an_abort_in_the_middle_of_the_answer_raises_and_gives_no_value(abbrechender_server, fehler, lesen, am_port):
    api = HomeAssistantApi(base_url=abbrechender_server, token="test-token", api_prefix="/api")
    with pytest.raises(fehler):
        lesen(api)
    with pytest.raises(SourceUnavailable):
        am_port(HaSignalSource(api))
