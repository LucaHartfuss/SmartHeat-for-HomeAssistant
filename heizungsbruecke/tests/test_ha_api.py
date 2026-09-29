import json
from unittest.mock import MagicMock, Mock, patch

import pytest
import requests

from heizungsbruecke.ha_api import HomeAssistantApi


def test_get_state_returns_parsed_float():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock()
    mock_response.json.return_value = {"state": "21.5"}
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.get", return_value=mock_response) as mock_get:
        result = api.get_state("climate.wohnzimmer_thermostat")

    assert result == 21.5
    mock_get.assert_called_once_with(
        "http://supervisor/core/api/states/climate.wohnzimmer_thermostat",
        headers={"Authorization": "Bearer test-token"},
        timeout=10,
    )


def test_get_raw_state_returns_string_state_without_float_cast():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock()
    mock_response.json.return_value = {"state": "heating"}
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.get", return_value=mock_response) as mock_get:
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
    mock_response = Mock()
    mock_response.json.return_value = {"state": bad_state}
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.get", return_value=mock_response), pytest.raises(ValueError):
        api.get_raw_state("sensor.mode")


def test_get_raw_state_rejects_attribute_reference_without_http_call():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")

    with patch("heizungsbruecke.ha_api.requests.get") as mock_get, pytest.raises(ValueError):
        api.get_raw_state("climate.x::hvac_action")

    mock_get.assert_not_called()


def test_get_state_with_attribute_reference_reads_named_attribute():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock()
    mock_response.json.return_value = {
        "state": "heat",
        "attributes": {"current_temperature": 19.5, "temperature": 21.0},
    }
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.get", return_value=mock_response) as mock_get:
        result = api.get_state("climate.wohnzimmer_thermostat::current_temperature")

    assert result == 19.5
    mock_get.assert_called_once_with(
        "http://supervisor/core/api/states/climate.wohnzimmer_thermostat",
        headers={"Authorization": "Bearer test-token"},
        timeout=10,
    )


def test_get_state_with_target_attribute_reference_reads_temperature():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock()
    mock_response.json.return_value = {
        "state": "heat",
        "attributes": {"current_temperature": 19.5, "temperature": 21.0},
    }
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.get", return_value=mock_response):
        result = api.get_state("climate.wohnzimmer_thermostat::temperature")

    assert result == 21.0


def test_set_number_value_posts_correct_payload():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock()
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.post", return_value=mock_response) as mock_post:
        api.set_number_value("number.weishaupt_heizkurve_steigung", 0.72)

    mock_post.assert_called_once_with(
        "http://supervisor/core/api/services/number/set_value",
        headers={"Authorization": "Bearer test-token"},
        json={"entity_id": "number.weishaupt_heizkurve_steigung", "value": 0.72},
        timeout=10,
    )


def test_send_notification_posts_to_the_split_notify_service():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock()
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.post", return_value=mock_response) as mock_post:
        api.send_notification("notify.mobile_app_lucas_iphone", "Sensor defekt")

    mock_post.assert_called_once_with(
        "http://supervisor/core/api/services/notify/mobile_app_lucas_iphone",
        headers={"Authorization": "Bearer test-token"},
        json={"message": "Sensor defekt"},
        timeout=10,
    )


def test_send_notification_raises_on_http_error():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock()
    mock_response.raise_for_status.side_effect = RuntimeError("500")

    with patch("heizungsbruecke.ha_api.requests.post", return_value=mock_response), pytest.raises(RuntimeError):
        api.send_notification("notify.mobile_app_lucas_iphone", "Sensor defekt")


def test_get_state_respects_custom_api_prefix():
    api = HomeAssistantApi(base_url="http://localhost:18213", token="test-token", api_prefix="/api")
    mock_response = Mock()
    mock_response.json.return_value = {"state": "21.5"}
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.get", return_value=mock_response) as mock_get:
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
    ws = MagicMock()
    ws.recv.side_effect = [
        json.dumps({"type": "auth_required"}),
        json.dumps({"type": "auth_ok"}),
        json.dumps({"id": 1, "success": True, "result": {"id": "abc123"}}),
    ]

    with patch("heizungsbruecke.ha_api.websocket.create_connection", return_value=ws) as mock_connect:
        result = api._call_ws_command({"type": "input_number/create"})

    assert result == {"id": "abc123"}
    mock_connect.assert_called_once_with("ws://supervisor/core/api/websocket", timeout=10)
    ws.send.assert_any_call(json.dumps({"type": "auth", "access_token": "test-token"}))
    ws.send.assert_any_call(json.dumps({"id": 1, "type": "input_number/create"}))
    ws.close.assert_called_once()


def test_call_ws_command_raises_when_first_message_is_not_auth_required():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    ws = MagicMock()
    ws.recv.side_effect = [json.dumps({"type": "event"})]

    with (
        patch("heizungsbruecke.ha_api.websocket.create_connection", return_value=ws),
        pytest.raises(RuntimeError, match="Unerwartete erste WS-Nachricht"),
    ):
        api._call_ws_command({"type": "x"})
    ws.close.assert_called_once()


def test_call_ws_command_raises_when_auth_fails():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    ws = MagicMock()
    ws.recv.side_effect = [
        json.dumps({"type": "auth_required"}),
        json.dumps({"type": "auth_invalid"}),
    ]

    with (
        patch("heizungsbruecke.ha_api.websocket.create_connection", return_value=ws),
        pytest.raises(RuntimeError, match="WS-Authentifizierung fehlgeschlagen"),
    ):
        api._call_ws_command({"type": "x"})
    ws.close.assert_called_once()


def test_call_ws_command_raises_when_command_result_is_unsuccessful():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    ws = MagicMock()
    ws.recv.side_effect = [
        json.dumps({"type": "auth_required"}),
        json.dumps({"type": "auth_ok"}),
        json.dumps({"id": 1, "success": False, "error": {"message": "nope"}}),
    ]

    with (
        patch("heizungsbruecke.ha_api.websocket.create_connection", return_value=ws),
        pytest.raises(RuntimeError, match="WS-Kommando fehlgeschlagen"),
    ):
        api._call_ws_command({"type": "x"})
    ws.close.assert_called_once()






def test_entity_exists_returns_true_when_state_found():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(status_code=200)
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.get", return_value=mock_response):
        assert api.entity_exists("sensor.dat") is True


def test_entity_exists_returns_false_on_404():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(status_code=404)

    with patch("heizungsbruecke.ha_api.requests.get", return_value=mock_response):
        assert api.entity_exists("sensor.missing") is False


def test_entity_exists_raises_on_other_http_error():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(status_code=500)
    mock_response.raise_for_status.side_effect = RuntimeError("500")

    with patch("heizungsbruecke.ha_api.requests.get", return_value=mock_response), pytest.raises(RuntimeError):
        api.entity_exists("sensor.dat")


def test_start_config_flow_posts_handler_and_returns_json():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock()
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = {"type": "form", "flow_id": "f1", "data_schema": []}

    with patch("heizungsbruecke.ha_api.requests.post", return_value=mock_response) as mock_post:
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
    mock_response = Mock()
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = {"type": "create_entry", "result": {"entry_id": "e1"}}

    with patch("heizungsbruecke.ha_api.requests.post", return_value=mock_response) as mock_post:
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

    with patch.object(
        api, "_finish_config_flow", side_effect=[step2_response, step3_response, final_response]
    ) as mock_finish:
        result = api._advance_config_flow(step1_response, all_fields)

    assert result == final_response
    assert mock_finish.call_args_list[0].args == ("f1", {"name": "SmartHeat DART", "entity_id": "sensor.room"})
    assert mock_finish.call_args_list[1].args == ("f1", {"state_characteristic": "mean"})
    assert mock_finish.call_args_list[2].args == (
        "f1", {"max_age": {"hours": 24}, "sampling_size": 255, "precision": 2}
    )


def test_advance_config_flow_returns_immediately_when_first_response_is_not_a_form():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    final_response = {"type": "create_entry", "result": {"entry_id": "e1"}}

    with patch.object(api, "_finish_config_flow") as mock_finish:
        result = api._advance_config_flow(final_response, {"name": "x"})

    assert result == final_response
    mock_finish.assert_not_called()


def test_find_entity_by_config_entry_returns_matching_entity_id():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    entries = [
        {"entity_id": "sensor.other", "config_entry_id": "e0"},
        {"entity_id": "sensor.dart", "config_entry_id": "e1"},
    ]

    with patch.object(api, "_call_ws_command", return_value=entries) as mock_call:
        result = api._find_entity_by_config_entry("e1")

    assert result == "sensor.dart"
    mock_call.assert_called_once_with({"type": "config/entity_registry/list"})


def test_find_entity_by_config_entry_raises_when_not_found():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")

    with patch.object(api, "_call_ws_command", return_value=[]), pytest.raises(RuntimeError, match="Keine Entity"):
        api._find_entity_by_config_entry("missing")










def test_create_persistent_notification_posts_to_service():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock()
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.post", return_value=mock_response) as mock_post:
        api.create_persistent_notification("SmartHeat", "Abo inaktiv", "smartheat_abo_inaktiv")

    mock_post.assert_called_once_with(
        "http://supervisor/core/api/services/persistent_notification/create",
        headers={"Authorization": "Bearer test-token"},
        json={"title": "SmartHeat", "message": "Abo inaktiv", "notification_id": "smartheat_abo_inaktiv"},
        timeout=10,
    )


def test_create_persistent_notification_raises_on_http_error():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock()
    mock_response.raise_for_status.side_effect = RuntimeError("500")

    with patch("heizungsbruecke.ha_api.requests.post", return_value=mock_response), pytest.raises(RuntimeError):
        api.create_persistent_notification("SmartHeat", "x", "smartheat_abo_inaktiv")


def test_dismiss_persistent_notification_posts_to_service():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock()
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.post", return_value=mock_response) as mock_post:
        api.dismiss_persistent_notification("smartheat_notbetrieb")

    mock_post.assert_called_once_with(
        "http://supervisor/core/api/services/persistent_notification/dismiss",
        headers={"Authorization": "Bearer test-token"},
        json={"notification_id": "smartheat_notbetrieb"},
        timeout=10,
    )


def test_dismiss_persistent_notification_raises_on_http_error():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock()
    mock_response.raise_for_status.side_effect = requests.HTTPError("500")

    with (
        patch("heizungsbruecke.ha_api.requests.post", return_value=mock_response),
        pytest.raises(requests.HTTPError),
    ):
        api.dismiss_persistent_notification("smartheat_notbetrieb")


def test_get_config_returns_parsed_json():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock()
    mock_response.json.return_value = {"time_zone": "Europe/Berlin"}
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.get", return_value=mock_response) as mock_get:
        result = api.get_config()

    assert result == {"time_zone": "Europe/Berlin"}
    mock_get.assert_called_once_with(
        "http://supervisor/core/api/config", headers={"Authorization": "Bearer test-token"}, timeout=10,
    )


def test_get_config_raises_on_http_error():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock()
    mock_response.raise_for_status.side_effect = requests.HTTPError("502")

    with (
        patch("heizungsbruecke.ha_api.requests.get", return_value=mock_response),
        pytest.raises(requests.HTTPError),
    ):
        api.get_config()


def test_advance_config_flow_follows_a_menu_step_then_the_form():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    menu = {"type": "menu", "flow_id": "f1", "menu_options": ["binary_sensor", "sensor"]}
    form = {"type": "form", "flow_id": "f1", "data_schema": [{"name": "name"}, {"name": "state"}]}
    done = {"type": "create_entry", "result": {"entry_id": "e1"}}
    fields = {"next_step_id": "sensor", "name": "N", "state": "{{ 1 }}", "unit_of_measurement": "°C"}

    with patch.object(api, "_finish_config_flow", side_effect=[form, done]) as mock_finish:
        result = api._advance_config_flow(menu, fields)

    assert result == done
    assert mock_finish.call_args_list[0].args == ("f1", {"next_step_id": "sensor"})
    assert mock_finish.call_args_list[1].args == ("f1", {"name": "N", "state": "{{ 1 }}"})


def test_create_template_sensor_runs_template_flow_with_temperature_fields():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    with patch.object(api, "_start_config_flow", return_value={"type": "menu", "flow_id": "f1"}) as mock_start, \
         patch.object(api, "_advance_config_flow", return_value={"type": "create_entry", "result": {"entry_id": "e7"}}) as mock_advance, \
         patch.object(api, "_find_entity_by_config_entry", return_value="sensor.smartheat_t1_raumtemperatur") as mock_find:
        entity_id = api.create_template_sensor(name="SmartHeat t1 Raumtemperatur", template="{{ 20 }}")

    assert entity_id == "sensor.smartheat_t1_raumtemperatur"
    mock_start.assert_called_once_with("template")
    assert mock_advance.call_args.args[1] == {
        "next_step_id": "sensor", "name": "SmartHeat t1 Raumtemperatur", "state": "{{ 20 }}",
        "unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement",
    }
    mock_find.assert_called_once_with("e7")


@pytest.mark.parametrize("platform", ["statistics", "template"])
def test_delete_helper_deletes_the_config_entry_of_the_entity(platform):
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    registry = [
        {"entity_id": "sensor.other", "config_entry_id": "e1", "platform": "mypyllant"},
        {"entity_id": "sensor.smartheat_t1_dart", "config_entry_id": "e2", "platform": platform},
    ]
    mock_response = Mock()
    mock_response.raise_for_status.return_value = None
    with patch.object(api, "_call_ws_command", return_value=registry), \
         patch("heizungsbruecke.ha_api.requests.delete", return_value=mock_response) as mock_delete:
        api.delete_helper("sensor.smartheat_t1_dart")

    mock_delete.assert_called_once_with(
        "http://supervisor/core/api/config/config_entries/entry/e2",
        headers={"Authorization": "Bearer test-token"}, timeout=10,
    )


def test_delete_helper_raises_for_entity_without_config_entry():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    registry = [{"entity_id": "sensor.smartheat_t1_x", "config_entry_id": None, "platform": "template"}]
    with patch.object(api, "_call_ws_command", return_value=registry), \
         patch("heizungsbruecke.ha_api.requests.delete") as mock_delete, pytest.raises(RuntimeError):
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
        patch.object(api, "_call_ws_command", return_value=[entry]),
        patch("heizungsbruecke.ha_api.requests.delete") as mock_delete,
        pytest.raises(RuntimeError, match="kein SmartHeat-Hilfssensor"),
    ):
        api.delete_helper("sensor.smartheat_t1_dart")

    mock_delete.assert_not_called()


@pytest.mark.parametrize("entity_id", ["sensor.fremder_helfer", "sensor.", "smartheat_t1_dart"])
def test_delete_helper_refuses_entities_without_smartheat_prefix(entity_id):
    """Wie delete_input_number: nur Entities mit dem Objekt-Teil-Praefix `smartheat_`, auch wenn
    eine fremde Entity zufaellig auf der Plattform template/statistics liegt."""
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    entry = {"entity_id": entity_id, "config_entry_id": "e_fremd", "platform": "template"}
    with (
        patch.object(api, "_call_ws_command", return_value=[entry]) as mock_ws,
        patch("heizungsbruecke.ha_api.requests.delete") as mock_delete,
        pytest.raises(RuntimeError, match="kein SmartHeat-Hilfssensor"),
    ):
        api.delete_helper(entity_id)

    mock_ws.assert_not_called()
    mock_delete.assert_not_called()


def test_fire_event_posts_the_event_data():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock()
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.post", return_value=mock_response) as mock_post:
        api.fire_event("smartheat_status", {"schema": 1, "status": "regelt"})

    mock_post.assert_called_once_with(
        "http://supervisor/core/api/events/smartheat_status",
        headers={"Authorization": "Bearer test-token"},
        json={"schema": 1, "status": "regelt"},
        timeout=10,
    )


def test_fire_event_raises_on_http_error():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock()
    mock_response.raise_for_status.side_effect = RuntimeError("401")

    with patch("heizungsbruecke.ha_api.requests.post", return_value=mock_response), pytest.raises(RuntimeError):
        api.fire_event("smartheat_status", {})


def test_is_reachable_true_when_ha_is_running():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    with patch.object(api, "get_config", return_value={"time_zone": "UTC", "state": "RUNNING"}):
        assert api.is_reachable() is True


@pytest.mark.parametrize("config", [
    {"state": "NOT_RUNNING"}, {"state": "STARTING"}, {"state": "STOPPING"}, {"time_zone": "UTC"}, [], None,
])
def test_is_reachable_false_while_ha_is_not_running(config):
    """HAs HTTP-Server antwortet schon frueh im Bootstrap; Integrationen wie mypyllant laden erst
    danach. Das Start-Budget darf erst bei RUNNING zaehlen (sonst Startfehler bei langsamer Cloud)."""
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    with patch.object(api, "get_config", return_value=config):
        assert api.is_reachable() is False


@pytest.mark.parametrize("error", [requests.ConnectionError("weg"), requests.HTTPError("502"), ValueError("kein JSON")])
def test_is_reachable_false_on_any_error(error):
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    with patch.object(api, "get_config", side_effect=error):
        assert api.is_reachable() is False


def test_set_climate_temperature_posts_correct_payload():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock()
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.post", return_value=mock_response) as mock_post:
        api.set_climate_temperature("climate.zone_1", 20.5)

    mock_post.assert_called_once_with(
        "http://supervisor/core/api/services/climate/set_temperature",
        headers={"Authorization": "Bearer test-token"},
        json={"entity_id": "climate.zone_1", "temperature": 20.5},
        timeout=10,
    )


def test_set_hvac_mode_posts_correct_payload():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock()
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.post", return_value=mock_response) as mock_post:
        api.set_hvac_mode("climate.zone_1", "heat_cool")

    mock_post.assert_called_once_with(
        "http://supervisor/core/api/services/climate/set_hvac_mode",
        headers={"Authorization": "Bearer test-token"},
        json={"entity_id": "climate.zone_1", "hvac_mode": "heat_cool"},
        timeout=10,
    )


def test_delete_input_number_sends_ws_command():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    with patch.object(api, "_call_ws_command", return_value={}) as mock_ws:
        api.delete_input_number("input_number.smartheat_x_tagesmittel")

    mock_ws.assert_called_once_with(
        {"type": "input_number/delete", "input_number_id": "smartheat_x_tagesmittel"}
    )


def test_delete_input_number_refuses_non_input_number_entities():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    with patch.object(api, "_call_ws_command") as mock_ws, pytest.raises(RuntimeError):
        api.delete_input_number("sensor.smartheat_x_tagesmittel")

    mock_ws.assert_not_called()


def test_delete_input_number_refuses_entities_without_smartheat_prefix():
    """Wie delete_helper(): nur SmartHeat-eigene Helfer werden geloescht, kein fremder
    input_number-Helfer wird versehentlich getroffen."""
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    with (
        patch.object(api, "_call_ws_command") as mock_ws,
        pytest.raises(RuntimeError, match="kein SmartHeat-Hilfssensor"),
    ):
        api.delete_input_number("input_number.fremder_helfer")

    mock_ws.assert_not_called()
