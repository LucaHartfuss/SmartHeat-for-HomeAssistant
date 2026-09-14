from unittest.mock import Mock, patch

import requests

from heizungsbruecke.web import create_app


def _app(ha_api=None):
    return create_app(
        heizungsserver_base_url="http://heizungsserver.example",
        ha_api=ha_api or Mock(),
        supervisor_base_url="http://supervisor",
        supervisor_token="test-supervisor-token",
    )


def test_login_forwards_credentials_and_stores_token_in_session():
    app = _app()
    app.testing = True
    mock_response = Mock(status_code=200)
    mock_response.json.return_value = {"token": "abc123"}

    with patch("heizungsbruecke.web.requests.post", return_value=mock_response) as mock_post:
        client = app.test_client()
        response = client.post("/api/login", json={"email": "luca@example.com", "password": "geheim123"})

    assert response.status_code == 200
    mock_post.assert_called_once_with(
        "http://heizungsserver.example/auth/login",
        json={"email": "luca@example.com", "password": "geheim123"},
        timeout=10,
    )
    with client.session_transaction() as flask_session:
        assert flask_session["heizungsserver_token"] == "abc123"


def test_login_returns_401_when_heizungsserver_rejects_credentials():
    app = _app()
    app.testing = True
    mock_response = Mock(status_code=401)

    with patch("heizungsbruecke.web.requests.post", return_value=mock_response):
        client = app.test_client()
        response = client.post("/api/login", json={"email": "luca@example.com", "password": "falsch"})

    assert response.status_code == 401


def test_login_returns_502_when_heizungsserver_unreachable():
    app = _app()
    app.testing = True

    with patch("heizungsbruecke.web.requests.post", side_effect=requests.RequestException("down")):
        client = app.test_client()
        response = client.post("/api/login", json={"email": "luca@example.com", "password": "geheim123"})

    assert response.status_code == 502


def test_login_requires_email_and_password():
    app = _app()
    app.testing = True
    client = app.test_client()

    response = client.post("/api/login", json={"email": "luca@example.com"})

    assert response.status_code == 400


def test_tenants_requires_login():
    app = _app()
    app.testing = True
    client = app.test_client()

    response = client.get("/api/tenants")

    assert response.status_code == 401


def test_tenants_returns_list_from_heizungsserver():
    app = _app()
    app.testing = True
    login_response = Mock(status_code=200)
    login_response.json.return_value = {"token": "abc123"}
    tenants_response = Mock(status_code=200)
    tenants_response.json.return_value = [{"tenant_id": "wohnung1", "profile_id": "vaillant_gastherme_heizkoerper"}]

    client = app.test_client()
    with patch("heizungsbruecke.web.requests.post", return_value=login_response):
        client.post("/api/login", json={"email": "luca@example.com", "password": "geheim123"})

    with patch("heizungsbruecke.web.requests.get", return_value=tenants_response) as mock_get:
        response = client.get("/api/tenants")

    assert response.status_code == 200
    assert response.get_json() == [{"tenant_id": "wohnung1", "profile_id": "vaillant_gastherme_heizkoerper"}]
    mock_get.assert_called_once_with(
        "http://heizungsserver.example/accounts/me/tenants",
        headers={"Authorization": "Bearer abc123"},
        timeout=10,
    )


def test_tenants_returns_502_json_when_heizungsserver_response_non_json():
    # I2(2): mirrors /api/login's existing guard -- a 200 response whose body isn't
    # valid JSON must become a clean JSON 502, not a raw HTML 500 traceback page.
    app = _app()
    app.testing = True
    client = app.test_client()
    _login(client)

    tenants_response = Mock(status_code=200)
    tenants_response.json.side_effect = ValueError("Invalid JSON")

    with patch("heizungsbruecke.web.requests.get", return_value=tenants_response):
        response = client.get("/api/tenants")

    assert response.status_code == 502
    body = response.get_json()
    assert isinstance(body, dict)
    assert "error" in body


def test_login_returns_502_when_heizungsserver_response_missing_token():
    app = _app()
    app.testing = True
    mock_response = Mock(status_code=200)
    mock_response.json.return_value = {"error": "no token here"}

    with patch("heizungsbruecke.web.requests.post", return_value=mock_response):
        client = app.test_client()
        response = client.post("/api/login", json={"email": "luca@example.com", "password": "geheim123"})

    assert response.status_code == 502
    assert response.get_json()["error"] == "Abo-Service nicht erreichbar"


def test_login_returns_502_when_heizungsserver_response_non_json():
    app = _app()
    app.testing = True
    mock_response = Mock(status_code=200)
    mock_response.json.side_effect = ValueError("Invalid JSON")

    with patch("heizungsbruecke.web.requests.post", return_value=mock_response):
        client = app.test_client()
        response = client.post("/api/login", json={"email": "luca@example.com", "password": "geheim123"})

    assert response.status_code == 502
    assert response.get_json()["error"] == "Abo-Service nicht erreichbar"


def test_login_returns_400_when_request_body_is_non_object_json():
    app = _app()
    app.testing = True
    client = app.test_client()

    # Send a JSON list instead of a JSON object
    response = client.post(
        "/api/login",
        data='["not", "a", "dict"]',
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "E-Mail und Passwort sind erforderlich"


def test_profiles_endpoint_requires_login():
    app = _app()
    app.testing = True
    client = app.test_client()

    response = client.get("/api/profiles")

    assert response.status_code == 401


def test_profiles_endpoint_lists_catalog_with_verified_flag():
    app = _app()
    app.testing = True
    client = app.test_client()
    _login(client)

    response = client.get("/api/profiles")

    assert response.status_code == 200
    body = response.get_json()
    vaillant_entry = next(e for e in body if e["profile_id"] == "vaillant_gastherme_heizkoerper")
    weishaupt_entry = next(e for e in body if e["profile_id"] == "weishaupt_waermepumpe_fussbodenheizung")
    assert vaillant_entry["verified"] is True
    assert weishaupt_entry["verified"] is False


def test_entities_endpoint_requires_login():
    app = _app()
    app.testing = True
    client = app.test_client()

    response = client.get("/api/entities?domain=sensor")

    assert response.status_code == 401


def test_entities_endpoint_filters_by_domain():
    ha_api = Mock()
    ha_api.list_states.return_value = [
        {"entity_id": "sensor.outdoor", "attributes": {"friendly_name": "Aussen", "unit_of_measurement": "°C"}},
        {"entity_id": "number.curve", "attributes": {"friendly_name": "Kurve"}},
    ]
    app = _app(ha_api=ha_api)
    app.testing = True
    client = app.test_client()
    _login(client)

    response = client.get("/api/entities?domain=sensor")

    assert response.status_code == 200
    assert response.get_json() == [
        {"entity_id": "sensor.outdoor", "friendly_name": "Aussen", "unit_of_measurement": "°C"},
    ]


def test_entities_endpoint_requires_domain_param():
    app = _app()
    app.testing = True
    client = app.test_client()
    _login(client)

    response = client.get("/api/entities")

    assert response.status_code == 400


def test_entities_endpoint_returns_502_json_when_ha_api_raises():
    # I2(1): a raised exception from list_states() must become a clean JSON 502, not
    # a raw HTML 500 traceback page.
    ha_api = Mock()
    ha_api.list_states.side_effect = RuntimeError("HA nicht erreichbar")
    app = _app(ha_api=ha_api)
    app.testing = True
    client = app.test_client()
    _login(client)

    response = client.get("/api/entities?domain=sensor")

    assert response.status_code == 502
    body = response.get_json()
    assert isinstance(body, dict)
    assert "error" in body


def _login(client):
    login_response = Mock(status_code=200)
    login_response.json.return_value = {"token": "abc123"}
    with patch("heizungsbruecke.web.requests.post", return_value=login_response):
        client.post("/api/login", json={"email": "luca@example.com", "password": "geheim123"})


_VALID_ENTITIES = {
    "entity_room_actual": {"entity_id": "sensor.rt", "unit_of_measurement": "°C"},
    "entity_room_target": {"entity_id": "sensor.target_rt", "unit_of_measurement": "°C"},
    "entity_outdoor_temp": {"entity_id": "sensor.outdoor", "unit_of_measurement": "°C"},
    "entity_offset_current": {"entity_id": "number.offset", "unit_of_measurement": "°C"},
    "entity_heat_limit": {"entity_id": "number.heat_limit", "unit_of_measurement": "°C"},
    "entity_curve_current": {"entity_id": "number.curve", "unit_of_measurement": None},
}

# /api/complete now revalidates every role's unit against real HA states instead of
# trusting the client-supplied unit_of_measurement (I1) -- these are the "real" states
# behind _VALID_ENTITIES's entity_ids, matching what a real ha_api.list_states() would
# report. Note the client-claimed unit_of_measurement values in _VALID_ENTITIES above
# are now irrelevant to validation; kept only because the wizard still sends them.
_VALID_STATES = [
    {"entity_id": "sensor.rt", "attributes": {"unit_of_measurement": "°C"}},
    {"entity_id": "sensor.target_rt", "attributes": {"unit_of_measurement": "°C"}},
    {"entity_id": "sensor.outdoor", "attributes": {"unit_of_measurement": "°C"}},
    {"entity_id": "number.offset", "attributes": {"unit_of_measurement": "°C"}},
    {"entity_id": "number.heat_limit", "attributes": {"unit_of_measurement": "°C"}},
    {"entity_id": "number.curve", "attributes": {}},
]


def _complete_app(states=None):
    ha_api = Mock()
    ha_api.list_states.return_value = _VALID_STATES if states is None else states
    return _app(ha_api=ha_api)


def test_complete_rejects_unverified_profile():
    app = _complete_app()
    app.testing = True
    client = app.test_client()
    _login(client)

    response = client.post("/api/complete", json={
        "tenant_id": "wohnung1",
        "profile_id": "weishaupt_waermepumpe_fussbodenheizung",
        "entities": _VALID_ENTITIES,
    })

    assert response.status_code == 400


def test_complete_rejects_wrong_unit():
    # I1: the server must revalidate against the REAL entity unit, not the client's
    # claim -- entities still claims "°C" for entity_room_actual (matching what a
    # cooperative wizard would send), but the real HA state (list_states(), mocked
    # here) reports "%". A client-trusted implementation would wrongly accept this.
    states = [dict(s) for s in _VALID_STATES]
    sensor_rt = next(s for s in states if s["entity_id"] == "sensor.rt")
    sensor_rt["attributes"] = {"unit_of_measurement": "%"}
    app = _complete_app(states=states)
    app.testing = True
    client = app.test_client()
    _login(client)

    response = client.post("/api/complete", json={
        "tenant_id": "wohnung1",
        "profile_id": "vaillant_gastherme_heizkoerper",
        "entities": _VALID_ENTITIES,
    })

    assert response.status_code == 400
    assert "erwartete Einheit" in response.get_json()["error"]


def test_complete_rejects_entity_id_that_does_not_exist_in_real_states():
    # I1(b): an entity_id the client sends must actually exist in HA's real states --
    # not merely be well-formed and unit-tagged by the client.
    bad_entities = dict(_VALID_ENTITIES)
    bad_entities["entity_room_actual"] = {"entity_id": "sensor.does_not_exist", "unit_of_measurement": "°C"}
    app = _complete_app()
    app.testing = True
    client = app.test_client()
    _login(client)

    response = client.post("/api/complete", json={
        "tenant_id": "wohnung1",
        "profile_id": "vaillant_gastherme_heizkoerper",
        "entities": bad_entities,
    })

    assert response.status_code == 400
    assert "existiert nicht" in response.get_json()["error"]


def test_complete_rejects_non_dict_entities():
    # I2(3): a non-dict `entities` value must not raise (AttributeError on .get())
    # before being validated -- it must be rejected with a clean 400.
    app = _complete_app()
    app.testing = True
    client = app.test_client()
    _login(client)

    response = client.post("/api/complete", json={
        "tenant_id": "wohnung1",
        "profile_id": "vaillant_gastherme_heizkoerper",
        "entities": ["not", "a", "dict"],
    })

    assert response.status_code == 400
    body = response.get_json()
    assert isinstance(body, dict)
    assert "error" in body


def test_complete_returns_400_not_500_for_non_object_json_body():
    # I2(3): a JSON list as the whole request body (not just `entities`) must also not
    # crash -- `body.get(...)` would otherwise raise AttributeError on a list.
    app = _complete_app()
    app.testing = True
    client = app.test_client()
    _login(client)

    response = client.post(
        "/api/complete",
        data='["not", "a", "dict"]',
        content_type="application/json",
    )

    assert response.status_code == 400
    body = response.get_json()
    assert isinstance(body, dict)
    assert "error" in body


def test_complete_writes_only_validated_roles_ignoring_extra_entities_keys():
    # I3: with schema: false, Supervisor accepts unvalidated extra option keys -- an
    # extra `entities` key must never reach set_own_options verbatim.
    app = _complete_app()
    app.testing = True
    client = app.test_client()
    _login(client)

    provision_response = Mock(status_code=200)
    provision_response.json.return_value = {
        "username": "wohnung1_a1b2",
        "password": "geheim",
        "mosquitto_passwd_command": "mosquitto_passwd -b /etc/mosquitto/passwd wohnung1_a1b2 geheim",
        "acl_snippet": "user wohnung1_a1b2\ntopic write smartheat/wohnung1/up/#\n",
    }
    entities_with_extra_key = dict(_VALID_ENTITIES)
    entities_with_extra_key["poll_interval_seconds"] = "not-a-number"

    with patch("heizungsbruecke.web.requests.post", return_value=provision_response), \
         patch("heizungsbruecke.web.supervisor_api.set_own_options") as mock_set_options:
        response = client.post("/api/complete", json={
            "tenant_id": "wohnung1",
            "profile_id": "vaillant_gastherme_heizkoerper",
            "entities": entities_with_extra_key,
        })

    assert response.status_code == 200
    written_options = mock_set_options.call_args.args[2]
    assert "poll_interval_seconds" not in written_options


def test_complete_provisions_and_writes_options_on_success():
    app = _complete_app()
    app.testing = True
    client = app.test_client()
    _login(client)

    provision_response = Mock(status_code=200)
    provision_response.json.return_value = {
        "username": "wohnung1_a1b2",
        "password": "geheim",
        "mosquitto_passwd_command": "mosquitto_passwd -b /etc/mosquitto/passwd wohnung1_a1b2 geheim",
        "acl_snippet": "user wohnung1_a1b2\ntopic write smartheat/wohnung1/up/#\n",
    }

    with patch("heizungsbruecke.web.requests.post", return_value=provision_response), \
         patch("heizungsbruecke.web.supervisor_api.set_own_options") as mock_set_options:
        response = client.post("/api/complete", json={
            "tenant_id": "wohnung1",
            "profile_id": "vaillant_gastherme_heizkoerper",
            "entities": _VALID_ENTITIES,
        })

    assert response.status_code == 200
    body = response.get_json()
    assert body["mqtt_username"] == "wohnung1_a1b2"
    assert body["mqtt_password"] == "geheim"
    assert body["mosquitto_passwd_command"] == "mosquitto_passwd -b /etc/mosquitto/passwd wohnung1_a1b2 geheim"
    assert body["acl_snippet"] == "user wohnung1_a1b2\ntopic write smartheat/wohnung1/up/#\n"
    mock_set_options.assert_called_once()
    written_options = mock_set_options.call_args.args[2]
    assert written_options["tenant_id"] == "wohnung1"
    assert written_options["profile"] == "vaillant_gastherme_heizkoerper"
    assert written_options["entity_room_actual"] == "sensor.rt"


def test_complete_returns_502_when_provisioning_response_is_missing_fields():
    app = _complete_app()
    app.testing = True
    client = app.test_client()
    _login(client)

    provision_response = Mock(status_code=200)
    provision_response.json.return_value = {"username": "wohnung1_a1b2"}  # missing password/commands/acl

    with patch("heizungsbruecke.web.requests.post", return_value=provision_response):
        response = client.post("/api/complete", json={
            "tenant_id": "wohnung1",
            "profile_id": "vaillant_gastherme_heizkoerper",
            "entities": _VALID_ENTITIES,
        })

    assert response.status_code == 502


def test_complete_relays_provisioning_failure():
    app = _complete_app()
    app.testing = True
    client = app.test_client()
    _login(client)

    provision_response = Mock(status_code=403)

    with patch("heizungsbruecke.web.requests.post", return_value=provision_response):
        response = client.post("/api/complete", json={
            "tenant_id": "wohnung-fremd",
            "profile_id": "vaillant_gastherme_heizkoerper",
            "entities": _VALID_ENTITIES,
        })

    assert response.status_code == 403


def test_complete_preserves_credentials_on_options_write_failure():
    app = _complete_app()
    app.testing = True
    client = app.test_client()
    _login(client)

    provision_response = Mock(status_code=200)
    provision_response.json.return_value = {
        "username": "wohnung1_a1b2",
        "password": "testsecret",
        "mosquitto_passwd_command": "mosquitto_passwd -b /etc/mosquitto/passwd wohnung1_a1b2 testsecret",
        "acl_snippet": "user wohnung1_a1b2\ntopic write smartheat/wohnung1/up/#\n",
    }

    with patch("heizungsbruecke.web.requests.post", return_value=provision_response), \
         patch("heizungsbruecke.web.supervisor_api.set_own_options", side_effect=requests.RequestException("down")):
        response = client.post("/api/complete", json={
            "tenant_id": "wohnung1",
            "profile_id": "vaillant_gastherme_heizkoerper",
            "entities": _VALID_ENTITIES,
        })

    assert response.status_code == 502
    body = response.get_json()
    assert body["mqtt_username"] == "wohnung1_a1b2"
    assert body["mqtt_password"] == "testsecret"
    assert body["mosquitto_passwd_command"] == "mosquitto_passwd -b /etc/mosquitto/passwd wohnung1_a1b2 testsecret"
    assert body["acl_snippet"] == "user wohnung1_a1b2\ntopic write smartheat/wohnung1/up/#\n"


def test_complete_returns_502_when_provisioning_response_is_non_dict_json():
    app = _complete_app()
    app.testing = True
    client = app.test_client()
    _login(client)

    provision_response = Mock(status_code=200)
    provision_response.json.return_value = None  # non-dict JSON response

    with patch("heizungsbruecke.web.requests.post", return_value=provision_response):
        response = client.post("/api/complete", json={
            "tenant_id": "wohnung1",
            "profile_id": "vaillant_gastherme_heizkoerper",
            "entities": _VALID_ENTITIES,
        })

    assert response.status_code == 502


def test_index_serves_wizard_page():
    app = _app()
    app.testing = True
    client = app.test_client()

    response = client.get("/")

    assert response.status_code == 200
    assert b"SmartHeat Einrichtung" in response.data


def test_unmapped_route_returns_normal_404_not_generic_500():
    # Regression guard: the generic @app.errorhandler(Exception) backstop must NOT
    # swallow HTTPException (Flask's _find_error_handler walks the MRO, so a bare
    # Exception handler catches 404s too unless it explicitly lets them through).
    # This matters in practice: static_url_path="" makes the static route a
    # catch-all, so every browser page load fires GET <base>/favicon.ico, which
    # would otherwise log a full ERROR-level traceback on every single wizard open.
    app = _app()
    app.testing = True
    client = app.test_client()

    response = client.get("/does-not-exist")

    assert response.status_code == 404
    body = response.get_json(silent=True)
    assert body is None or body.get("error") != "Unerwarteter Fehler"


def test_unexpected_exception_still_gets_generic_json_500_backstop():
    # Mirror image of the above: a genuine, unnamed exception must still be caught
    # and turned into the generic JSON 500 -- the HTTPException passthrough must not
    # accidentally disable the backstop entirely.
    app = _complete_app()
    app.testing = True
    client = app.test_client()
    _login(client)

    with patch("heizungsbruecke.web.profiles.is_verified", side_effect=RuntimeError("boom")):
        response = client.post("/api/complete", json={
            "tenant_id": "wohnung1",
            "profile_id": "vaillant_gastherme_heizkoerper",
            "entities": _VALID_ENTITIES,
        })

    assert response.status_code == 500
    body = response.get_json()
    assert isinstance(body, dict)
    assert body["error"] == "Unerwarteter Fehler"


def test_wizard_js_uses_ingress_relative_api_paths():
    # C1: Home Assistant serves add-on Ingress panels at a path prefix
    # (/api/hassio_ingress/<token>/...) and does not rewrite the add-on's own HTML/JS
    # for it -- a root-absolute "/api/..." fetch would resolve against the HA origin,
    # not this add-on. Every apiFetch(...) call must be prefixed with the page-relative
    # BASE constant instead. This is invisible to a Flask-test-client/Docker-script
    # check hitting the app at root, which is exactly why it slipped through before.
    app = _app()
    app.testing = True
    client = app.test_client()

    response = client.get("/wizard.js")

    assert response.status_code == 200
    body = response.data.decode()
    assert "const BASE = window.location.pathname" in body
    assert 'apiFetch("/api/' not in body
    assert "apiFetch(`/api/" not in body
    assert 'fetch("/api/' not in body


def test_wizard_js_never_offers_climate_domain_for_entity_room_actual():
    # C2: entity_room_actual feeds derived_sensors.ensure_all() -> HA's statistics
    # config-flow, which rejects both climate-domain sources and the "::attribute"
    # suffix -- the heating bridge then dies silently after ~4 minutes of retries.
    # entity_room_target is unaffected (it only ever goes through ha_api.get_state())
    # and must keep offering climate.
    app = _app()
    app.testing = True
    client = app.test_client()

    response = client.get("/wizard.js")
    body = response.data.decode()

    role_domains_block = body[body.index("ROLE_DOMAINS"):body.index("CLIMATE_ATTRIBUTE_BY_ROLE")]
    room_actual_line = next(
        line for line in role_domains_block.splitlines() if "entity_room_actual" in line
    )
    room_target_line = next(
        line for line in role_domains_block.splitlines() if "entity_room_target" in line
    )
    assert "climate" not in room_actual_line
    assert "climate" in room_target_line
    assert "entity_room_actual" not in body[body.index("CLIMATE_ATTRIBUTE_BY_ROLE"):]
