"""Der Fake-ViCare-Server verhaelt sich wie die echte API in den Teilen, die vicare_cloud braucht."""
import json
import urllib.error
import urllib.parse
import urllib.request

import pytest
from fake_vicare.server import FakeVicare

IDS = ("2012345", "7637415000000001", "0")
FEATURES = f"/iot/v2/features/installations/{IDS[0]}/gateways/{IDS[1]}/devices/{IDS[2]}/features"


@pytest.fixture
def fake():
    server = FakeVicare()
    base = server.start()
    yield server, base
    server.stop()


def call(base, method, path, body=None, form=None, token=None):
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
    else:
        data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read() or b"{}"), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}"), dict(error.headers)


def login(base):
    form = {"grant_type": "authorization_code", "client_id": "c", "code": "ok-code", "redirect_uri": "r",
            "code_verifier": "v"}
    status, body, _ = call(base, "POST", "/idp/v3/token", form=form)
    assert status == 200
    return body


def test_code_exchange_and_refresh_rotate_the_refresh_token(fake):
    server, base = fake
    first = login(base)
    form = {"grant_type": "refresh_token", "client_id": "c", "refresh_token": first["refresh_token"]}
    status, second, _ = call(base, "POST", "/idp/v3/token", form=form)
    assert status == 200 and second["refresh_token"] != first["refresh_token"]
    status, body, _ = call(base, "POST", "/idp/v3/token", form=form)
    assert status == 400 and body["error"] == "invalid_grant"  # rotiert: das alte ist tot
    assert server.tokens_issued == 2


def test_api_needs_a_valid_token_and_lists_installations_and_features(fake):
    server, base = fake
    assert call(base, "GET", "/iot/v2/equipment/installations?includeGateways=true")[0] == 401
    token = login(base)["access_token"]
    status, body, _ = call(base, "GET", "/iot/v2/equipment/installations?includeGateways=true", token=token)
    device = body["data"][0]["gateways"][0]["devices"][0]
    assert status == 200 and device["deviceType"] == "heating"
    status, body, _ = call(base, "GET", FEATURES, token=token)
    assert status == 200 and any(f["feature"] == "heating.circuits.0.heating.curve" for f in body["data"])
    assert server.calls == [("GET", "/iot/v2/equipment/installations"), ("GET", FEATURES)]


def test_a_command_is_checked_against_its_constraints_and_changes_the_feature(fake):
    server, base = fake
    token = login(base)["access_token"]
    path = FEATURES + "/heating.circuits.0.heating.curve/commands/setCurve"
    assert call(base, "POST", path, {"shift": 0, "slope": 9.9}, token=token)[0] == 400
    assert call(base, "POST", path, {"shift": 0, "slope": 1.25}, token=token)[0] == 400  # Schritt 0,1
    assert call(base, "POST", path, {"shift": 2, "slope": 1.1}, token=token)[0] == 200
    features = {f["feature"]: f for f in call(base, "GET", FEATURES, token=token)[1]["data"]}
    props = features["heating.circuits.0.heating.curve"]["properties"]
    assert (props["shift"]["value"], props["slope"]["value"]) == (2, 1.1)
    assert server.commands[-1] == {"feature": "heating.circuits.0.heating.curve", "command": "setCurve",
                                   "params": {"shift": 2, "slope": 1.1}}


def test_switches_offline_rate_limit_and_invalid_grant(fake):
    server, base = fake
    token = login(base)["access_token"]
    call(base, "POST", "/__control", {"rate_limit_after": 1})
    assert call(base, "GET", FEATURES, token=token)[0] == 200
    status, _, headers = call(base, "GET", FEATURES, token=token)
    assert status == 429 and headers["Retry-After"] == "60"
    call(base, "POST", "/__control", {"reset": True, "offline": True})
    token = login(base)["access_token"]  # reset verwirft die Tokens
    assert call(base, "GET", FEATURES, token=token)[0] == 503
    call(base, "POST", "/__control", {"reset": True, "invalid_grant": True})
    form = {"grant_type": "authorization_code", "code": "ok-code"}
    assert call(base, "POST", "/idp/v3/token", form=form)[1]["error"] == "invalid_grant"
