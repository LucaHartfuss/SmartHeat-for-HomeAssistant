"""Der ViCare-Client zaehlt jeden Aufruf, behandelt 401/429/503 und kennt die Kommandos der Spec."""
import logging
import time

import pytest
import requests
from fake_vicare.server import GOOD_CODE, FakeVicare

from smartheat_gateway.drivers.vicare_cloud import api as vicare
from smartheat_gateway.drivers.vicare_cloud import oauth
from smartheat_gateway.quota import QuotaExhausted, QuotaGuard, QuotaSpec

IDS = (2012345, "7637415000000001", "0")


@pytest.fixture
def world(tmp_path):
    server = FakeVicare()
    base = server.start()
    tokens = oauth.TokenStore(tmp_path / "vicare.json", iam=base)
    tokens.begin("c", "r")
    tokens.finish(GOOD_CODE, "r")
    guard = QuotaGuard(tmp_path / "quota" / "vicare_cloud.json", QuotaSpec(1450, 3, 86400))
    client = vicare.ViCareApi(tokens, guard, base=base)
    yield server, client, guard, tokens
    server.stop()


class StubSession:
    """Liefert vorbereitete Antworten (oder wirft) und merkt sich die Aufrufe."""

    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class StubResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("kein JSON")
        return self._body


def stubbed(world, *outcomes):
    server, client, guard, tokens = world
    session = StubSession(*outcomes)
    return vicare.ViCareApi(tokens, guard, base="http://stub", session=session), session, guard


def test_installations_are_flattened(world):
    server, client, guard, tokens = world
    assert client.installations() == [{"installation_id": 2012345, "gateway_serial": "7637415000000001",
                                       "device_id": "0", "device_type": "heating", "model_id": "Fake"}]


def test_features_return_the_feature_list(world):
    server, client, guard, tokens = world
    names = {item["feature"] for item in client.features(*IDS)}
    assert "heating.circuits.0.heating.curve" in names


def test_every_api_call_counts_against_the_quota_and_the_hard_limit_stops_the_client(world):
    server, client, guard, tokens = world
    client.installations()
    client.features(*IDS)
    client.features(*IDS)
    assert guard.used() == 3
    with pytest.raises(QuotaExhausted):
        client.features(*IDS)
    assert len(server.calls) == 3  # der vierte Aufruf hat die API nie erreicht


def test_http_429_blocks_the_quota_until_retry_after(world):
    server, client, guard, tokens = world
    server.control({"rate_limit_after": 0})
    with pytest.raises(vicare.RateLimited) as error:
        client.features(*IDS)
    assert error.value.retry_after == 60 and guard.exhausted()


def test_429_without_retry_after_blocks_for_one_hour(world):
    client, session, guard = stubbed(world, StubResponse(429, {}))
    with pytest.raises(vicare.RateLimited) as error:
        client.features(*IDS)
    assert error.value.retry_after == 3600.0
    assert guard.exhausted()
    assert abs(guard._load()["gesperrt_bis"] - (time.time() + 3600.0)) < 5


def test_429_with_unparsable_retry_after_falls_back_to_one_hour(world):
    client, session, guard = stubbed(world, StubResponse(429, {}, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}))
    with pytest.raises(vicare.RateLimited) as error:
        client.features(*IDS)
    assert error.value.retry_after == 3600.0


@pytest.mark.parametrize("header, expected", [("0", 60.0), ("-5", 60.0), ("nan", 3600.0), ("inf", 3600.0),
                                               ("-inf", 3600.0), ("999999999", 86400.0), ("120", 120.0),
                                               ("1.5", 60.0)])
def test_retry_after_is_sanitised_before_it_reaches_the_quota_file(world, header, expected):
    client, session, guard = stubbed(world, StubResponse(429, {}, {"Retry-After": header}))
    with pytest.raises(vicare.RateLimited) as error:
        client.features(*IDS)
    assert error.value.retry_after == expected
    assert abs(guard._load()["gesperrt_bis"] - (time.time() + expected)) < 5


@pytest.mark.parametrize("status", [400, 403, 404, 408])
def test_a_4xx_on_a_read_is_not_a_rejected_command(world, status):
    client, session, guard = stubbed(world, StubResponse(status, {"error": "x"}), StubResponse(status, {}))
    with pytest.raises(vicare.ReadFailed) as error:
        client.features(*IDS)
    assert not isinstance(error.value, vicare.CommandRejected) and "Einstellung" not in str(error.value)
    with pytest.raises(vicare.ReadFailed):
        client.installations()


def test_a_wrong_path_on_the_real_fake_is_a_read_failure(world):
    server, client, guard, tokens = world
    with pytest.raises(vicare.ReadFailed):
        client.features(2012345, "7637415000000001", "99")  # Fake: 404 DEVICE_NOT_FOUND


def test_offline_is_unreachable_and_an_expired_token_is_renewed_once(world):
    server, client, guard, tokens = world
    server.control({"offline": True})
    with pytest.raises(vicare.Unreachable):
        client.features(*IDS)
    server.control({"reset": True})
    # Fake kennt nach reset keine Tokens mehr: 401 -> eine Erneuerung scheitert -> NotAuthenticated
    with pytest.raises(vicare.NotAuthenticated):
        client.features(*IDS)


def test_a_revoked_access_token_is_renewed_once_and_the_call_is_repeated(world):
    server, client, guard, tokens = world
    server.access.clear()  # serverseitig widerrufen, das Refresh-Token gilt noch
    assert client.features(*IDS)
    assert server.tokens_issued == 2
    assert guard.used() == 2  # beide HTTP-Aufrufe zaehlen (der abgelehnte und die Wiederholung)


def test_a_second_401_after_the_renewal_is_not_authenticated_without_a_third_call(world):
    client, session, guard = stubbed(world, StubResponse(401), StubResponse(401))
    with pytest.raises(vicare.NotAuthenticated):
        client.features(*IDS)
    assert len(session.calls) == 2


def test_a_transient_iam_failure_during_the_renewal_is_unreachable_not_logged_out(world):
    server, client, guard, tokens = world
    server.access.clear()
    server.refresh.clear()
    original = tokens._post

    def broken(*args, **kwargs):
        raise requests.ConnectionError("iam weg")
    tokens._post = broken
    try:
        with pytest.raises(vicare.Unreachable):
            client.features(*IDS)
    finally:
        tokens._post = original
    assert tokens.logged_in()


def test_a_missing_login_is_not_authenticated_and_costs_no_quota(world):
    server, client, guard, tokens = world
    tokens.forget()
    with pytest.raises(vicare.NotAuthenticated):
        client.features(*IDS)
    assert guard.used() == 0 and server.calls == []


def test_a_network_error_is_unreachable_and_hides_the_url(world):
    client, session, guard = stubbed(world, requests.ConnectionError("HTTPConnectionPool 7637415000000001"))
    with pytest.raises(vicare.Unreachable) as error:
        client.features(*IDS)
    assert "7637415000000001" not in str(error.value) and error.value.__cause__ is None
    assert error.value.__suppress_context__


def test_an_unparsable_success_body_is_unreachable(world):
    client, session, guard = stubbed(world, StubResponse(200, None))
    with pytest.raises(vicare.Unreachable):
        client.features(*IDS)


def test_a_non_object_body_yields_no_data(world):
    client, session, guard = stubbed(world, StubResponse(200, ["x"]), StubResponse(200, {"data": "kaputt"}))
    assert client.features(*IDS) == []
    assert client.features(*IDS) == []


def test_the_bearer_token_and_the_json_body_are_sent(world):
    server, client, guard, tokens = world
    stub = StubSession(StubResponse(200, {"data": {}}))
    client = vicare.ViCareApi(tokens, guard, base="http://stub", session=stub)
    client.execute(*IDS, "f", "setTemperature", {"targetTemperature": 21})
    method, url, kwargs = stub.calls[0]
    assert method == "POST" and url.startswith("http://stub/iot/v2/features/installations/2012345/gateways/")
    assert kwargs["json"] == {"targetTemperature": 21}
    assert kwargs["headers"]["Authorization"] == f"Bearer {tokens.access_token()}"


def test_commands_post_the_documented_paths_and_bodies(world):
    server, client, guard, tokens = world
    client.execute(*IDS, "heating.circuits.0.heating.curve", "setCurve", {"shift": 1, "slope": 1.2})
    client.execute(*IDS, "heating.circuits.0.operating.programs.normal", "setTemperature", {"targetTemperature": 21})
    assert [c["command"] for c in server.commands] == ["setCurve", "setTemperature"]
    assert server.calls[-1][1].endswith("/features/heating.circuits.0.operating.programs.normal/commands/setTemperature")


def test_a_rejected_command_carries_a_text_without_the_body(world):
    server, client, guard, tokens = world
    with pytest.raises(vicare.CommandRejected) as error:
        client.execute(*IDS, "heating.circuits.0.heating.curve", "setCurve", {"shift": 1, "slope": 9.9})
    assert "9.9" not in str(error.value)


def test_nothing_secret_reaches_the_log(world, caplog):
    server, client, guard, tokens = world
    caplog.set_level(logging.DEBUG)
    with pytest.raises(vicare.CommandRejected):
        client.execute(*IDS, "heating.circuits.0.heating.curve", "setCurve", {"shift": 1, "slope": 9.9})
    server.control({"offline": True})
    with pytest.raises(vicare.Unreachable):
        client.features(*IDS)
    # urllib3 loggt auf DEBUG die URL (Seriennummer) selbst; geprueft wird, was dieser Treiber ausgibt
    text = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("smartheat_gateway"))
    assert "7637415000000001" not in text and "9.9" not in text and tokens.access_token() not in text
