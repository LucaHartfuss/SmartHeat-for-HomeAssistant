"""PKCE-Anmeldung und Token-Erneuerung (Spec G4 1.4, Review Focus 1 und 2)."""
import base64
import hashlib
import json
import logging
import os
import stat
import threading

import pytest
import requests
from fake_vicare.server import GOOD_CODE, FakeVicare

from smartheat_gateway.drivers.vicare_cloud import oauth


@pytest.fixture
def fake():
    server = FakeVicare()
    base = server.start()
    yield server, base
    server.stop()


def store(tmp_path, base, wall=None, **extra):
    return oauth.TokenStore(tmp_path / "vicare.json", iam=base, **({"wall": wall} if wall else {}), **extra)


def test_the_challenge_is_the_s256_hash_of_a_valid_verifier():
    verifier, challenge = oauth.new_pair()
    assert 43 <= len(verifier) <= 128
    assert challenge == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def test_login_roundtrip_stores_tokens_privately_and_never_returns_them(fake, tmp_path):
    server, base = fake
    tokens = store(tmp_path, base)
    challenge = tokens.begin("client-x", "https://portal.test/oauth/callback")
    assert len(challenge) == 43
    assert tokens.finish(GOOD_CODE, "https://portal.test/oauth/callback") is None
    assert tokens.logged_in() and tokens.access_token()
    assert stat.S_IMODE(os.stat(tmp_path / "vicare.json").st_mode) == 0o600
    assert "pending" not in json.loads((tmp_path / "vicare.json").read_text())  # Verifier nach dem Tausch weg


def test_finish_without_begin_or_with_a_wrong_redirect_is_refused(fake, tmp_path):
    server, base = fake
    tokens = store(tmp_path, base)
    with pytest.raises(oauth.NotLoggedIn):
        tokens.finish(GOOD_CODE, "https://portal.test/oauth/callback")
    tokens.begin("client-x", "https://portal.test/oauth/callback")
    with pytest.raises(oauth.NotLoggedIn):
        tokens.finish(GOOD_CODE, "https://evil.test/oauth/callback")


def test_the_code_exchange_sends_the_matching_verifier_client_and_redirect(fake, tmp_path):
    """Der Fake prueft PKCE nicht: der Vertrag wird an der gesendeten Anfrage geprueft."""
    server, base = fake
    sent = []

    def spy(url, data, timeout):
        sent.append((url, dict(data)))
        return requests.post(url, data=data, timeout=timeout)

    tokens = store(tmp_path, base, post=spy)
    challenge = tokens.begin("client-x", "https://portal.test/cb")
    tokens.finish(GOOD_CODE, "https://portal.test/cb")
    url, form = sent[0]
    assert url == base + "/idp/v3/token"
    assert form["grant_type"] == "authorization_code" and form["code"] == GOOD_CODE
    assert form["client_id"] == "client-x" and form["redirect_uri"] == "https://portal.test/cb"
    digest = hashlib.sha256(form["code_verifier"].encode()).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == challenge


def test_a_bad_code_is_a_login_rejection_with_a_customer_text(fake, tmp_path):
    server, base = fake
    tokens = store(tmp_path, base)
    tokens.begin("client-x", "r")
    with pytest.raises(oauth.LoginRejected) as error:
        tokens.finish("falsch", "r")
    assert "Anmeld" in error.value.text and "falsch" not in error.value.text


def test_the_pending_login_expires_after_ten_minutes(fake, tmp_path):
    server, base = fake
    now = [1000.0]
    tokens = store(tmp_path, base, wall=lambda: now[0])
    tokens.begin("client-x", "r")
    now[0] += 601
    with pytest.raises(oauth.NotLoggedIn):
        tokens.finish(GOOD_CODE, "r")


def test_the_access_token_is_renewed_shortly_before_it_expires(fake, tmp_path):
    server, base = fake
    now = [1_000_000.0]
    server._clock = lambda: now[0]
    tokens = store(tmp_path, base, wall=lambda: now[0])
    tokens.begin("c", "r")
    tokens.finish(GOOD_CODE, "r")
    first = tokens.access_token()
    now[0] += 3600 - 30
    assert tokens.access_token() != first and server.tokens_issued == 2


def test_two_processes_renewing_at_once_share_one_refresh(fake, tmp_path):
    """Review Focus 2: das Refresh-Token rotiert, der zweite Erneuerer muss das neue lesen."""
    server, base = fake
    now = [2_000_000.0]
    server._clock = lambda: now[0]
    first = store(tmp_path, base, wall=lambda: now[0])
    first.begin("c", "r")
    first.finish(GOOD_CODE, "r")
    now[0] += 3600 + 1
    second = store(tmp_path, base, wall=lambda: now[0])  # zweite Instanz = zweiter Prozess, gleiche Datei
    results, errors = [], []

    def renew(instance):
        try:
            results.append(instance.access_token())
        except Exception as error:  # pragma: no cover - der Test schlaegt dann ueber errors fehl
            errors.append(error)

    threads = [threading.Thread(target=renew, args=(s,)) for s in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors and len(set(results)) == 1 and server.tokens_issued == 2


def test_invalid_grant_clears_the_tokens_and_marks_the_login_expired(fake, tmp_path):
    server, base = fake
    now = [3_000_000.0]
    server._clock = lambda: now[0]
    tokens = store(tmp_path, base, wall=lambda: now[0])
    tokens.begin("c", "r")
    tokens.finish(GOOD_CODE, "r")
    server.control({"invalid_grant": True})
    now[0] += 3601
    with pytest.raises(oauth.NotLoggedIn):
        tokens.access_token()
    assert not tokens.logged_in() and tokens.expired()
    data = json.loads((tmp_path / "vicare.json").read_text())  # bleibt gueltiges JSON
    assert "access_token" not in data and "refresh_token" not in data
    assert stat.S_IMODE(os.stat(tmp_path / "vicare.json").st_mode) == 0o600
    assert not (tmp_path / "vicare.json.tmp").exists()


def test_an_unreachable_iam_is_not_a_logout(fake, tmp_path):
    server, base = fake
    now = [4_000_000.0]
    server._clock = lambda: now[0]
    tokens = store(tmp_path, base, wall=lambda: now[0])
    tokens.begin("c", "r")
    tokens.finish(GOOD_CODE, "r")
    server.stop()
    now[0] += 3601
    with pytest.raises(oauth.TokenUnavailable):
        tokens.access_token()
    assert tokens.logged_in() and not tokens.expired()


def test_a_rate_limited_or_failing_iam_is_not_a_logout(fake, tmp_path):
    """429/408 und 5xx sind voruebergehend: nur ein echtes Ablehnen (invalid_grant) meldet 'abgelaufen'."""
    server, base = fake
    now = [5_000_000.0]
    server._clock = lambda: now[0]
    status = [429]

    class Response:
        def __init__(self, code):
            self.status_code = code

        def json(self):
            return {}

    tokens = store(tmp_path, base, wall=lambda: now[0])
    tokens.begin("c", "r")
    tokens.finish(GOOD_CODE, "r")
    tokens._post = lambda *a, **k: Response(status[0])
    now[0] += 3601
    for code in (429, 408, 500, 503):
        status[0] = code
        with pytest.raises(oauth.TokenUnavailable):
            tokens.access_token()
        assert tokens.logged_in() and not tokens.expired()


@pytest.mark.parametrize("status, body", [(403, {}), (404, {}), (401, {"error": "invalid_client"}),
                                          (400, {"error": "invalid_request"}), (400, {})])
def test_other_client_errors_on_refresh_are_not_a_logout(fake, tmp_path, status, body):
    """Nur 400/401 mit error=invalid_grant widerruft; WAF-403, falsche Basis-404, invalid_client nicht."""
    server, base = fake
    now = [5_500_000.0]
    server._clock = lambda: now[0]
    tokens = store(tmp_path, base, wall=lambda: now[0])
    tokens.begin("c", "r")
    tokens.finish(GOOD_CODE, "r")
    before = (tmp_path / "vicare.json").read_bytes()

    class Response:
        status_code = status

        def json(self):
            return body

    tokens._post = lambda *a, **k: Response()
    now[0] += 3601
    with pytest.raises(oauth.TokenUnavailable):
        tokens.access_token()
    assert (tmp_path / "vicare.json").read_bytes() == before  # Datei unveraendert, gueltiges JSON
    assert json.loads(before) and tokens.logged_in() and not tokens.expired()


@pytest.mark.parametrize("status", [400, 401])
def test_invalid_grant_with_400_or_401_is_a_logout(fake, tmp_path, status):
    server, base = fake
    now = [5_600_000.0]
    server._clock = lambda: now[0]
    tokens = store(tmp_path, base, wall=lambda: now[0])
    tokens.begin("c", "r")
    tokens.finish(GOOD_CODE, "r")

    class Response:
        status_code = status

        def json(self):
            return {"error": "invalid_grant"}

    tokens._post = lambda *a, **k: Response()
    now[0] += 3601
    with pytest.raises(oauth.NotLoggedIn):
        tokens.access_token()
    assert not tokens.logged_in() and tokens.expired()


def test_a_malformed_token_response_is_not_a_logout(fake, tmp_path):
    server, base = fake
    now = [6_000_000.0]
    server._clock = lambda: now[0]
    tokens = store(tmp_path, base, wall=lambda: now[0])
    tokens.begin("c", "r")
    tokens.finish(GOOD_CODE, "r")

    class Garbage:
        status_code = 200

        def json(self):
            raise ValueError("kein JSON")

    tokens._post = lambda *a, **k: Garbage()
    now[0] += 3601
    with pytest.raises(oauth.TokenUnavailable):
        tokens.access_token()
    assert tokens.logged_in() and not tokens.expired()


def test_force_refresh_renews_a_fresh_token(fake, tmp_path):
    server, base = fake
    tokens = store(tmp_path, base)
    tokens.begin("c", "r")
    tokens.finish(GOOD_CODE, "r")
    first = tokens.access_token()
    assert tokens.access_token() == first and server.tokens_issued == 1
    assert tokens.access_token(force_refresh=True) != first and server.tokens_issued == 2


def test_not_logged_in_without_a_file(tmp_path):
    tokens = oauth.TokenStore(tmp_path / "vicare.json", iam="http://127.0.0.1:1")
    assert not tokens.logged_in() and not tokens.expired()
    with pytest.raises(oauth.NotLoggedIn):
        tokens.access_token()


def test_no_secret_reaches_logs_or_exception_texts(fake, tmp_path, caplog):
    server, base = fake
    now = [7_000_000.0]
    server._clock = lambda: now[0]
    caplog.set_level(logging.DEBUG)
    tokens = store(tmp_path, base, wall=lambda: now[0])
    challenge = tokens.begin("c", "r")
    verifier = json.loads((tmp_path / "vicare.json").read_text())["pending"]["verifier"]
    tokens.finish(GOOD_CODE, "r")
    stored = json.loads((tmp_path / "vicare.json").read_text())
    secrets_seen = [verifier, stored["access_token"], stored["refresh_token"]]
    server.control({"invalid_grant": True})
    now[0] += 3601
    with pytest.raises(oauth.NotLoggedIn) as error:
        tokens.access_token()
    tokens.begin("c", "r")
    with pytest.raises(oauth.LoginRejected) as rejected:
        tokens.finish(GOOD_CODE, "r")
    texts = [caplog.text, str(error.value), rejected.value.text, challenge]
    for secret in secrets_seen:
        assert all(secret not in text for text in texts)
    assert GOOD_CODE not in caplog.text


def test_forget_removes_everything_and_the_test_endpoints_need_the_flag(fake, tmp_path, monkeypatch):
    server, base = fake
    tokens = store(tmp_path, base)
    tokens.begin("c", "r")
    tokens.finish(GOOD_CODE, "r")
    tokens.forget()
    assert not tokens.logged_in() and not (tmp_path / "vicare.json").exists()
    monkeypatch.setenv("SHG_VICARE_IAM_BASE", "http://evil.test")
    monkeypatch.delenv("SHG_TEST_ENDPOINTS", raising=False)
    assert oauth.iam_base() == "https://iam.viessmann-climatesolutions.com"
    monkeypatch.setenv("SHG_TEST_ENDPOINTS", "1")
    assert oauth.iam_base() == "http://evil.test"
