"""Anmeldung bei Viessmann per OAuth2 mit PKCE (Spec SHG G4 1.4, Variante A). Der Server sieht nur den kurzlebigen Code;
Verifier, Access- und Refresh-Token liegen nur in der privaten Datei des Geraets. Erneuert wird unter flock und mit
Nachlesen: das Refresh-Token rotiert, ein zweiter Prozess (Agent bei einer Probe, Laufzeit im Takt) darf nie mit dem
alten erneuern. Ein abgelehntes Refresh-Token (invalid_grant) loescht die Tokens und merkt `abgelaufen`; ein
unerreichbares oder voruebergehend ueberlastetes IAM ist kein Abmelden."""
import base64
import fcntl
import hashlib
import logging
import os
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import requests

from smartheat_gateway.files import read_json, write_json

logger = logging.getLogger(__name__)

IAM_BASE = "https://iam.viessmann-climatesolutions.com"
API_BASE = "https://api.viessmann-climatesolutions.com"
TOKEN_PATH = "/idp/v3/token"
PENDING_SECONDS = 600
RENEW_BEFORE_SECONDS = 60
HTTP_TIMEOUT = 20
TRANSIENT_CLIENT_STATUS = (408, 429)  # 4xx, die keine Ablehnung der Anmeldung sind
LOGIN_FAILED_TEXT = "Die Anmeldung bei Viessmann ist fehlgeschlagen. Bitte noch einmal anmelden."


def _base(env_name: str, default: str) -> str:
    if os.environ.get("SHG_TEST_ENDPOINTS") == "1" and os.environ.get(env_name):
        return os.environ[env_name].rstrip("/")
    return default


def iam_base() -> str:
    return _base("SHG_VICARE_IAM_BASE", IAM_BASE)


def api_base() -> str:
    return _base("SHG_VICARE_API_BASE", API_BASE)


class NotLoggedIn(Exception):
    """Keine (oder abgelaufene) Anmeldung: der Kunde muss sie im Portal erneuern."""


class TokenUnavailable(Exception):
    """Das IAM ist nicht erreichbar; die Anmeldung gilt weiter."""


class LoginRejected(Exception):
    def __init__(self, text: str = LOGIN_FAILED_TEXT) -> None:
        super().__init__(text)
        self.text = text


def new_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class TokenStore:
    def __init__(self, path: Path, *, wall=time.time, iam: str | None = None, post=requests.post) -> None:
        self._path = path
        self._lock_path = path.with_suffix(".lock")
        self._wall = wall
        self._iam = iam
        self._post = post

    # --- Datei ---

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _load(self) -> dict:
        data = read_json(self._path)
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        write_json(self._path, data, private=True)

    def logged_in(self) -> bool:
        return bool(self._load().get("refresh_token"))

    def expired(self) -> bool:
        return bool(self._load().get("abgelaufen"))

    def forget(self) -> None:
        with self._locked():
            for path in (self._path, self._path.with_name(self._path.name + ".tmp")):
                path.unlink(missing_ok=True)

    # --- Anmeldung ---

    def begin(self, client_id: str, redirect_uri: str) -> str:
        verifier, challenge = new_pair()
        with self._locked():
            data = self._load()
            data["pending"] = {"verifier": verifier, "client_id": client_id, "redirect_uri": redirect_uri,
                               "created": self._wall()}
            self._save(data)
        return challenge

    def finish(self, code: str, redirect_uri: str) -> None:
        with self._locked():
            data = self._load()
            pending = data.get("pending")
            if (not isinstance(pending, dict) or pending.get("redirect_uri") != redirect_uri
                    or self._wall() - float(pending.get("created", 0)) > PENDING_SECONDS):
                raise NotLoggedIn("Es wurde keine Anmeldung vorbereitet (oder sie ist abgelaufen).")
            body = self._token_request({
                "grant_type": "authorization_code", "client_id": pending["client_id"], "code": code,
                "redirect_uri": redirect_uri, "code_verifier": pending["verifier"]})
            if body is None:
                raise LoginRejected()
            self._save(self._with_tokens({"client_id": pending["client_id"]}, body))

    # --- Zugriff ---

    def access_token(self, force_refresh: bool = False) -> str:
        data = self._load()
        if not data.get("refresh_token"):
            raise NotLoggedIn("Nicht bei Viessmann angemeldet.")
        if not force_refresh and self._fresh(data):
            return data["access_token"]
        with self._locked():
            data = self._load()  # Nachlesen: ein anderer Prozess kann schon erneuert haben
            if not data.get("refresh_token"):
                raise NotLoggedIn("Nicht bei Viessmann angemeldet.")
            if not force_refresh and self._fresh(data):
                return data["access_token"]
            body = self._token_request({"grant_type": "refresh_token", "client_id": data["client_id"],
                                        "refresh_token": data["refresh_token"]},
                                       revoked_only=True)
            if body is None:
                self._save({"client_id": data.get("client_id"), "abgelaufen": True})
                raise NotLoggedIn("Die Anmeldung bei Viessmann ist abgelaufen.")
            data = self._with_tokens(data, body)
            self._save(data)
            return data["access_token"]

    def _fresh(self, data: dict) -> bool:
        expires_at = float(data.get("expires_at", 0))
        return bool(data.get("access_token")) and expires_at - self._wall() > RENEW_BEFORE_SECONDS

    def _with_tokens(self, data: dict, body: dict) -> dict:
        # "pending" bleibt: eine Erneuerung waehrend einer neuen Anmeldung darf deren PKCE-Zustand nicht loeschen
        # (finish baut die Daten ohne "pending" neu auf).
        data = {k: v for k, v in data.items() if k != "abgelaufen"}
        data.update({"access_token": body["access_token"],
                     "refresh_token": body.get("refresh_token") or data.get("refresh_token"),
                     "expires_at": self._wall() + float(body.get("expires_in", 3600))})
        return data

    def _token_request(self, form: dict, *, revoked_only: bool = False) -> dict | None:
        """Antwort bei 200, None bei einer Ablehnung, TokenUnavailable sonst. Nie den Inhalt loggen.

        Beim Erneuern (revoked_only) gilt nur 400/401 mit error=invalid_grant als Ablehnung, denn sie loescht die
        Anmeldung; 403 (WAF), 404 (falsche Basis), invalid_client u. a. sind kein Widerruf. Beim Code-Tausch
        (finish) ist jede nicht voruebergehende 4xx eine Ablehnung, das zerstoert nichts."""
        try:
            response = self._post((self._iam or iam_base()) + TOKEN_PATH, data=form, timeout=HTTP_TIMEOUT)
        except requests.RequestException as error:
            logger.warning("Viessmann-IAM nicht erreichbar (%s)", type(error).__name__)
            raise TokenUnavailable() from None
        status = response.status_code
        try:
            body = response.json()
        except ValueError:
            body = None
        if status == 200:
            if isinstance(body, dict) and isinstance(body.get("access_token"), str):
                return body
            logger.warning("Viessmann-IAM: unbrauchbare Antwort")
            raise TokenUnavailable()
        if revoked_only:
            if status in (400, 401) and isinstance(body, dict) and body.get("error") == "invalid_grant":
                logger.warning("Viessmann-IAM widerruft die Anmeldung (%s, invalid_grant)", status)
                return None
            logger.warning("Viessmann-IAM antwortet beim Erneuern mit %s, Anmeldung bleibt", status)
            raise TokenUnavailable()
        if 400 <= status < 500 and status not in TRANSIENT_CLIENT_STATUS:
            logger.warning("Viessmann-IAM lehnt die Anfrage ab (%s)", status)
            return None
        logger.warning("Viessmann-IAM antwortet voruebergehend mit %s", status)
        raise TokenUnavailable()
