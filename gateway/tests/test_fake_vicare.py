"""Der Fake-ViCare-Server verhaelt sich wie die echte API in den Teilen, die vicare_cloud braucht."""
import json
import urllib.error
import urllib.parse
import urllib.request

import pytest
from fake_vicare.server import DEFAULT, FakeVicare

from smartheat_core import hausmodell

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


CURVE = "heating.circuits.0.heating.curve"
NORMAL = "heating.circuits.0.operating.programs.normal"
ROOM = "heating.circuits.0.sensors.temperature.room"
SUPPLY = "heating.circuits.0.sensors.temperature.supply"
STATISTICS = "heating.burners.0.statistics"
FAR_FUTURE = 10 ** 9  # Token-Laufzeit: die Test-Uhr springt um Stunden


@pytest.fixture
def modell(clock):
    """Fake mit Hausmodell (Standard) auf der Test-Uhr; die Tokens ueberleben die Spruenge der Uhr."""
    server = FakeVicare(clock=clock)
    server.control({"token_ttl": FAR_FUTURE})
    base = server.start()
    token = login(base)["access_token"]
    yield server, base, token, clock
    server.stop()


def features(base, token):
    return {f["feature"]: f for f in call(base, "GET", FEATURES, token=token)[1]["data"]}


def prop(items, feature, name):
    return items[feature]["properties"][name]["value"]


def kern(schritte, *, slope=1.4, shift=0.0, soll=20.0, aussen=5.0):
    """Direktlauf des Kerns mit den Eingaengen, die der Fake aus seinen Features bildet."""
    zustand = hausmodell.Anlagenzustand(room=20.0, burner_hours=1000.0)
    erzeuger = hausmodell.Potenzgesetz(a=2.54, b=0.78)
    letzter = None
    for _ in range(schritte):
        letzter = hausmodell.anlage_schritt(
            zustand, hausmodell.HOUSES["H1"], erzeuger, (slope, soll + shift, 16.0), aussen, 0.0, 12.0, soll,
            hausmodell.Betriebsflags(), None)
    return zustand, letzter


def command(base, token, feature, name, body):
    return call(base, "POST", f"{FEATURES}/{feature}/commands/{name}", body, token=token)[0]


def test_the_room_sensor_follows_the_heating_curve_of_the_core(modell):
    server, base, token, clock = modell
    assert prop(features(base, token), ROOM, "value") == 20.0
    clock.advance(12 * 3600)
    items = features(base, token)
    zustand, _ = kern(12 * 12)
    assert prop(items, ROOM, "value") == round(zustand.room, 2) != 20.0
    assert prop(items, SUPPLY, "value") == round(max(hausmodell.Potenzgesetz(2.54, 0.78).flow(1.4, 20.0, 5.0), 20.0), 1)
    assert prop(items, "heating.sensors.temperature.outside", "value") == 5


def test_a_higher_curve_heats_the_room_more(modell):
    server, base, token, clock = modell
    assert command(base, token, CURVE, "setCurve", {"shift": 0, "slope": 3.5}) == 200
    clock.advance(12 * 3600)
    hoch = prop(features(base, token), ROOM, "value")
    zustand, _ = kern(12 * 12, slope=3.5)
    assert hoch == round(zustand.room, 2)
    other = FakeVicare(clock=clock)
    other.control({"token_ttl": FAR_FUTURE})
    base2 = other.start()
    try:
        token2 = login(base2)["access_token"]
        assert command(base2, token2, CURVE, "setCurve", {"shift": 0, "slope": 0.2}) == 200
        clock.advance(12 * 3600)
        assert prop(features(base2, token2), ROOM, "value") < hoch - 1.0
    finally:
        other.stop()


def test_a_command_acts_from_its_moment_on(modell):
    server, base, token, clock = modell
    clock.advance(6 * 3600)
    features(base, token)  # rechnet die ersten sechs Stunden mit der Kurve 1,4
    assert command(base, token, CURVE, "setCurve", {"shift": 0, "slope": 3.5}) == 200
    clock.advance(6 * 3600)
    erster, _ = kern(6 * 12)
    zustand = erster
    for _ in range(6 * 12):
        hausmodell.anlage_schritt(zustand, hausmodell.HOUSES["H1"], hausmodell.Potenzgesetz(2.54, 0.78),
                                  (3.5, 20.0, 16.0), 5.0, 0.0, 12.0, 20.0, hausmodell.Betriebsflags(), None)
    assert prop(features(base, token), ROOM, "value") == round(zustand.room, 2)


def test_set_temperature_and_shift_change_the_core_inputs(modell):
    server, base, token, clock = modell
    assert command(base, token, NORMAL, "setTemperature", {"targetTemperature": 24}) == 200
    assert command(base, token, CURVE, "setCurve", {"shift": 3, "slope": 1.4}) == 200
    clock.advance(6 * 3600)
    zustand, _ = kern(6 * 12, shift=3.0, soll=24.0)
    assert prop(features(base, token), ROOM, "value") == round(zustand.room, 2)


def test_the_burner_statistics_are_advanced_by_the_core(modell):
    server, base, token, clock = modell
    clock.advance(6 * 3600)
    items = features(base, token)
    zustand, _ = kern(6 * 12)
    assert prop(items, STATISTICS, "hours") == round(zustand.burner_hours, 3) > 1000.0
    assert prop(items, STATISTICS, "starts") == zustand.burner_starts
    assert prop(items, "heating.burners.0", "active") is True


def test_the_model_needs_time_not_polls(modell):
    server, base, token, clock = modell
    clock.advance(100)
    features(base, token)
    clock.advance(100)
    features(base, token)
    clock.advance(100)  # 300 s in drei Abfragen = ein Schritt, auch wenn die Reste einzeln darunter liegen
    zustand, _ = kern(1)
    assert prop(features(base, token), ROOM, "value") == round(zustand.room, 2)
    assert prop(features(base, token), ROOM, "value") == round(zustand.room, 2)  # ohne Zeit keine Aenderung


def test_a_reset_restarts_the_model(modell):
    server, base, token, clock = modell
    clock.advance(12 * 3600)
    assert prop(features(base, token), ROOM, "value") != 20.0
    server.control({"reset": True, "token_ttl": FAR_FUTURE})
    token = login(base)["access_token"]
    assert prop(features(base, token), ROOM, "value") == 20.0


def test_recordings_stay_static_without_the_model(clock):
    server = FakeVicare({"0": json.loads(json.dumps(DEFAULT))}, clock=clock)
    server.control({"token_ttl": FAR_FUTURE})
    base = server.start()
    try:
        token = login(base)["access_token"]
        before = features(base, token)
        assert command(base, token, CURVE, "setCurve", {"shift": 0, "slope": 3.5}) == 200
        clock.advance(12 * 3600)
        after = features(base, token)
        assert ROOM not in after and STATISTICS not in after
        assert prop(after, SUPPLY, "value") == prop(before, SUPPLY, "value") == 35
        assert prop(after, CURVE, "slope") == 3.5
    finally:
        server.stop()


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
