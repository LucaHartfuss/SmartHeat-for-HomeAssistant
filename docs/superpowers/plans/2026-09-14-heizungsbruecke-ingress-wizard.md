# Heizungsbruecke Ingress-Wizard (Teil 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an Ingress-served setup wizard for the `heizungsbruecke` add-on (login against a dummy account/abo stub, tenant dropdown, Hersteller×Typ×Verteilsystem profile dropdown with combination check, sensor dropdowns with unit validation) that becomes the *only* way to configure the add-on, backed by a small new accounts/provisioning API on `heizungsserver`.

**Architecture:** Two repos, two new Flask apps. `heizungsserver` gets a `sqlite3`-backed `accounts` module (dummy accounts, tenant links, sessions) behind a Flask API (`accounts_api.py`), plus a `smartheat`-scheme ACL-snippet formatter reusing `onboarding.generate_installation()`. `heizungsbruecke` gets a Flask app (`web.py`) serving a static vanilla-JS wizard page and JSON routes that proxy to heizungsserver, list real HA entities via `ha_api`, and write add-on options via a new `supervisor_api` module. `heizungsbruecke/__main__.py` is restructured so the existing poll loop runs in a background thread while Flask owns the main thread — an unconfigured add-on is now a normal (not fatal) startup state.

**Tech Stack:** Python 3.12, Flask (new dependency in both repos), `werkzeug.security` (ships with Flask) for password hashing, `sqlite3` (heizungsserver, matches existing `state.py` pattern), `requests` (heizungsbruecke, already a dependency), plain HTML/vanilla JS for the wizard frontend (no build step), pytest with Flask's test client for all new tests.

**Spec:** `docs/superpowers/specs/2026-09-14-heizungsbruecke-ingress-wizard-design.md`

## Global Constraints

- `config.yaml`'s `options`/`schema` blocks for `tenant_id`/`profile`/`entity_*`/`poll_interval_seconds`/`notify_service`/`failsafe_stale_after_hours` are removed entirely — the wizard becomes the sole configuration path (0.6.0, breaking change).
- The add-on never auto-restarts itself after the wizard writes options; it tells the user to restart manually.
- The provisioning endpoint never mutates the live Mosquitto broker directly (no `mosquitto_passwd`/ACL file writes) — it returns the exact manual commands, same as the existing `onboarding.py` CLI.
- Account passwords are hashed with `werkzeug.security.generate_password_hash`/`check_password_hash`, never stored or logged in plaintext.
- No async framework anywhere — Flask (sync) on both repos, matching heizungsserver's existing `sqlite3`/stdlib-heavy style.
- Every wizard-facing error is a specific, human-readable German message; nothing falls back to a silent default or partial save.
- `onboarding.py`'s existing `format_acl_snippet()` (legacy `hz/<anlagen_id>/...` scheme) is left untouched; the new ACL formatter is a separate function for the `smartheat/<tenant_id>/...` scheme.

---

## Part 1: heizungsserver (new `accounts` module + API)

All Part 1 tasks happen in `C:\Users\Luca\Documents\HomeAssistant Dev\heizungsserver`, a separate git repository currently checked out directly on `main`. **Task 1's first step creates a feature branch before touching any files.**

### Task 1: `accounts.py` storage module

**Files:**
- Create: `src/heizungsserver/accounts.py`
- Test: `tests/test_accounts.py`

**Interfaces:**
- Produces: `init_db(path: str) -> sqlite3.Connection`, `Account(account_id, email, password_hash)` (frozen dataclass), `TenantLink(tenant_id, profile_id)` (frozen dataclass), `create_account(conn, email: str, password_hash: str) -> str`, `get_account_by_email(conn, email: str) -> Account | None`, `link_tenant(conn, account_id: str, tenant_id: str, profile_id: str) -> None`, `tenants_for_account(conn, account_id: str) -> list[TenantLink]`, `tenant_belongs_to_account(conn, account_id: str, tenant_id: str) -> bool`, `any_account_has_tenant(conn, tenant_id: str) -> bool`, `create_session(conn, account_id: str) -> str`, `account_id_for_token(conn, token: str) -> str | None`.

- [ ] **Step 1: Create the feature branch**

Run from the heizungsserver repo root:
```bash
git -C "C:\Users\Luca\Documents\HomeAssistant Dev\heizungsserver" checkout -b ingress-wizard-accounts-stub
```
Expected: `Switched to a new branch 'ingress-wizard-accounts-stub'`

- [ ] **Step 2: Write the failing tests**

Create `tests/test_accounts.py`:
```python
import pytest

from heizungsserver.accounts import (
    account_id_for_token,
    any_account_has_tenant,
    create_account,
    create_session,
    get_account_by_email,
    init_db,
    link_tenant,
    tenant_belongs_to_account,
    tenants_for_account,
)


@pytest.fixture
def conn(tmp_path):
    return init_db(str(tmp_path / "accounts.db"))


def test_create_and_get_account_round_trip(conn):
    account_id = create_account(conn, "luca@example.com", "hashed-pw")

    account = get_account_by_email(conn, "luca@example.com")

    assert account.account_id == account_id
    assert account.email == "luca@example.com"
    assert account.password_hash == "hashed-pw"


def test_get_account_by_email_returns_none_when_missing(conn):
    assert get_account_by_email(conn, "unknown@example.com") is None


def test_link_tenant_and_list_tenants_for_account(conn):
    account_id = create_account(conn, "luca@example.com", "hashed-pw")
    link_tenant(conn, account_id, "wohnung1", "vaillant_gastherme_heizkoerper")
    link_tenant(conn, account_id, "wohnung2", "weishaupt_waermepumpe_fussbodenheizung")

    tenants = tenants_for_account(conn, account_id)

    assert {(t.tenant_id, t.profile_id) for t in tenants} == {
        ("wohnung1", "vaillant_gastherme_heizkoerper"),
        ("wohnung2", "weishaupt_waermepumpe_fussbodenheizung"),
    }


def test_link_tenant_updates_profile_on_conflict(conn):
    account_id = create_account(conn, "luca@example.com", "hashed-pw")
    link_tenant(conn, account_id, "wohnung1", "vaillant_gastherme_heizkoerper")

    link_tenant(conn, account_id, "wohnung1", "weishaupt_waermepumpe_fussbodenheizung")

    tenants = tenants_for_account(conn, account_id)
    assert len(tenants) == 1
    assert tenants[0].profile_id == "weishaupt_waermepumpe_fussbodenheizung"


def test_tenant_belongs_to_account_true_and_false(conn):
    account_id = create_account(conn, "luca@example.com", "hashed-pw")
    link_tenant(conn, account_id, "wohnung1", "vaillant_gastherme_heizkoerper")

    assert tenant_belongs_to_account(conn, account_id, "wohnung1") is True
    assert tenant_belongs_to_account(conn, account_id, "wohnung-unbekannt") is False


def test_any_account_has_tenant_true_and_false(conn):
    account_id = create_account(conn, "luca@example.com", "hashed-pw")
    link_tenant(conn, account_id, "wohnung1", "vaillant_gastherme_heizkoerper")

    assert any_account_has_tenant(conn, "wohnung1") is True
    assert any_account_has_tenant(conn, "wohnung-nirgendwo") is False


def test_create_session_and_resolve_account_id_for_token(conn):
    account_id = create_account(conn, "luca@example.com", "hashed-pw")
    token = create_session(conn, account_id)

    assert account_id_for_token(conn, token) == account_id


def test_account_id_for_token_returns_none_for_unknown_token(conn):
    assert account_id_for_token(conn, "unknown-token") is None
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd "C:\Users\Luca\Documents\HomeAssistant Dev\heizungsserver" && python -m pytest tests/test_accounts.py -v`
Expected: FAIL (collection error) with `ModuleNotFoundError: No module named 'heizungsserver.accounts'`

- [ ] **Step 4: Implement `accounts.py`**

Create `src/heizungsserver/accounts.py`:
```python
"""SQLite-basierter Speicher fuer den Dummy-Account-/Abo-Stub (Ingress-Wizard-Login).

Gleiches Muster wie state.py: reine Funktionen + sqlite3, eigene init_db. Passwoerter
kommen bereits gehasht an (werkzeug.security in accounts_api.py) -- dieses Modul
speichert nie ein Klartextpasswort.
"""

import secrets
import sqlite3
from dataclasses import dataclass

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS account_tenants (
    account_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    PRIMARY KEY (account_id, tenant_id)
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    account_id TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Account:
    account_id: str
    email: str
    password_hash: str


@dataclass(frozen=True)
class TenantLink:
    tenant_id: str
    profile_id: str


def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def create_account(conn: sqlite3.Connection, email: str, password_hash: str) -> str:
    account_id = secrets.token_hex(8)
    conn.execute(
        "INSERT INTO accounts (account_id, email, password_hash) VALUES (?, ?, ?)",
        (account_id, email, password_hash),
    )
    conn.commit()
    return account_id


def get_account_by_email(conn: sqlite3.Connection, email: str) -> Account | None:
    row = conn.execute(
        "SELECT account_id, email, password_hash FROM accounts WHERE email = ?",
        (email,),
    ).fetchone()
    return Account(*row) if row is not None else None


def link_tenant(conn: sqlite3.Connection, account_id: str, tenant_id: str, profile_id: str) -> None:
    conn.execute(
        """
        INSERT INTO account_tenants (account_id, tenant_id, profile_id)
        VALUES (?, ?, ?)
        ON CONFLICT(account_id, tenant_id) DO UPDATE SET profile_id = excluded.profile_id
        """,
        (account_id, tenant_id, profile_id),
    )
    conn.commit()


def tenants_for_account(conn: sqlite3.Connection, account_id: str) -> list[TenantLink]:
    rows = conn.execute(
        "SELECT tenant_id, profile_id FROM account_tenants WHERE account_id = ?",
        (account_id,),
    ).fetchall()
    return [TenantLink(*row) for row in rows]


def tenant_belongs_to_account(conn: sqlite3.Connection, account_id: str, tenant_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM account_tenants WHERE account_id = ? AND tenant_id = ?",
        (account_id, tenant_id),
    ).fetchone()
    return row is not None


def any_account_has_tenant(conn: sqlite3.Connection, tenant_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM account_tenants WHERE tenant_id = ?",
        (tenant_id,),
    ).fetchone()
    return row is not None


def create_session(conn: sqlite3.Connection, account_id: str) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO sessions (token, account_id) VALUES (?, ?)", (token, account_id))
    conn.commit()
    return token


def account_id_for_token(conn: sqlite3.Connection, token: str) -> str | None:
    row = conn.execute("SELECT account_id FROM sessions WHERE token = ?", (token,)).fetchone()
    return row[0] if row is not None else None


def _main() -> None:
    """CLI-Helfer zum Anlegen eines Dummy-Test-Accounts, analog zu onboarding.py's
    _main(). Kein Self-Signup im Wizard -- Accounts werden von Hand hierueber seeded.
    """
    import getpass
    import sys

    from werkzeug.security import generate_password_hash

    if len(sys.argv) != 3:
        print("Usage: python -m heizungsserver.accounts <db_path> <email>", file=sys.stderr)
        sys.exit(1)
    db_path, email = sys.argv[1], sys.argv[2]
    password = getpass.getpass("Passwort: ")
    conn = init_db(db_path)
    account_id = create_account(conn, email, generate_password_hash(password))
    print(f"Account angelegt: {email} (account_id={account_id})")


if __name__ == "__main__":
    _main()
```

- [ ] **Step 5: Install Flask (needed for `werkzeug.security` used by the CLI helper and later tasks)**

Run: `pip install "flask>=3.0,<4"`
Then add it to `pyproject.toml`'s `dependencies` list (`dependencies = ["paho-mqtt>=2.1,<3", "flask>=3.0,<4"]`).

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_accounts.py -v`
Expected: PASS (9 tests)

- [ ] **Step 7: Commit**

```bash
git add src/heizungsserver/accounts.py tests/test_accounts.py pyproject.toml
git commit -m "feat(accounts): add sqlite-backed dummy account/tenant-link/session storage"
```

---

### Task 2: `smartheat_acl.py` ACL-snippet formatter

**Files:**
- Create: `src/heizungsserver/smartheat_acl.py`
- Test: `tests/test_smartheat_acl.py`

**Interfaces:**
- Produces: `format_smartheat_acl_snippet(username: str, tenant_id: str) -> str`

- [ ] **Step 1: Write the failing test**

Create `tests/test_smartheat_acl.py`:
```python
from heizungsserver.smartheat_acl import format_smartheat_acl_snippet


def test_format_smartheat_acl_snippet_contains_expected_topics():
    snippet = format_smartheat_acl_snippet("wohnung1_a1b2c3d4", "wohnung1")

    assert "user wohnung1_a1b2c3d4" in snippet
    assert "topic write smartheat/wohnung1/up/#" in snippet
    assert "topic read smartheat/wohnung1/down/#" in snippet
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_smartheat_acl.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `smartheat_acl.py`**

Create `src/heizungsserver/smartheat_acl.py`:
```python
"""ACL-Snippet-Formatierung fuer das `smartheat/<tenant_id>/...`-Topic-Schema
(heizungsserver.generic). Bewusst getrennt von onboarding.format_acl_snippet(), das
weiterhin das aeltere `hz/<anlagen_id>/...`-Schema (heizungsserver.daemon) bedient --
beide Schemata laufen aktuell parallel, siehe Design-Spec.
"""


def format_smartheat_acl_snippet(username: str, tenant_id: str) -> str:
    return (
        f"user {username}\n"
        f"topic write smartheat/{tenant_id}/up/#\n"
        f"topic read smartheat/{tenant_id}/down/#\n"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_smartheat_acl.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/heizungsserver/smartheat_acl.py tests/test_smartheat_acl.py
git commit -m "feat(accounts): add smartheat-scheme ACL snippet formatter"
```

---

### Task 3: `accounts_api.py` — login + tenants routes

**Files:**
- Create: `src/heizungsserver/accounts_api.py`
- Test: `tests/test_accounts_api.py`

**Interfaces:**
- Consumes: everything from Task 1 (`heizungsserver.accounts`).
- Produces: `create_app(db_path: str, tenants_json_path: str) -> Flask` with routes `POST /auth/login` and `GET /accounts/me/tenants`. `app.config["TENANTS_JSON_PATH"]` is set from the `tenants_json_path` argument (Task 4 reads it).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_accounts_api.py`:
```python
from werkzeug.security import generate_password_hash

from heizungsserver import accounts
from heizungsserver.accounts_api import create_app


def _app_with_account(tmp_path):
    db_path = str(tmp_path / "accounts.db")
    tenants_json_path = str(tmp_path / "tenants.json")
    with open(tenants_json_path, "w") as f:
        f.write("[]")

    conn = accounts.init_db(db_path)
    account_id = accounts.create_account(conn, "luca@example.com", generate_password_hash("geheim123"))
    conn.close()

    app = create_app(db_path=db_path, tenants_json_path=tenants_json_path)
    app.testing = True
    return app, account_id, db_path


def test_login_succeeds_with_correct_credentials(tmp_path):
    app, _, _ = _app_with_account(tmp_path)
    client = app.test_client()

    response = client.post("/auth/login", json={"email": "luca@example.com", "password": "geheim123"})

    assert response.status_code == 200
    assert "token" in response.get_json()


def test_login_fails_with_wrong_password(tmp_path):
    app, _, _ = _app_with_account(tmp_path)
    client = app.test_client()

    response = client.post("/auth/login", json={"email": "luca@example.com", "password": "falsch"})

    assert response.status_code == 401


def test_login_fails_for_unknown_email(tmp_path):
    app, _, _ = _app_with_account(tmp_path)
    client = app.test_client()

    response = client.post("/auth/login", json={"email": "unbekannt@example.com", "password": "geheim123"})

    assert response.status_code == 401


def test_login_requires_email_and_password(tmp_path):
    app, _, _ = _app_with_account(tmp_path)
    client = app.test_client()

    response = client.post("/auth/login", json={"email": "luca@example.com"})

    assert response.status_code == 400


def test_me_tenants_requires_authentication(tmp_path):
    app, _, _ = _app_with_account(tmp_path)
    client = app.test_client()

    response = client.get("/accounts/me/tenants")

    assert response.status_code == 401


def test_me_tenants_returns_linked_tenants(tmp_path):
    app, account_id, db_path = _app_with_account(tmp_path)
    conn = accounts.init_db(db_path)
    accounts.link_tenant(conn, account_id, "wohnung1", "vaillant_gastherme_heizkoerper")
    conn.close()
    client = app.test_client()
    token = client.post(
        "/auth/login", json={"email": "luca@example.com", "password": "geheim123"}
    ).get_json()["token"]

    response = client.get("/accounts/me/tenants", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.get_json() == [{"tenant_id": "wohnung1", "profile_id": "vaillant_gastherme_heizkoerper"}]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_accounts_api.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'heizungsserver.accounts_api'`

- [ ] **Step 3: Implement `accounts_api.py`**

Create `src/heizungsserver/accounts_api.py`:
```python
"""Flask-API fuer den Dummy-Account-/Abo-Stub (siehe accounts.py fuer die
Storage-Schicht). Ein-Konto-pro-Request-sqlite-Connection -- passend zur Dummy-Groesse
dieses Service, kein Connection-Pooling.
"""

import sqlite3

from flask import Flask, g, jsonify, request
from werkzeug.security import check_password_hash

from heizungsserver import accounts


def create_app(db_path: str, tenants_json_path: str) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path
    app.config["TENANTS_JSON_PATH"] = tenants_json_path

    def get_conn() -> sqlite3.Connection:
        if "conn" not in g:
            g.conn = accounts.init_db(app.config["DB_PATH"])
        return g.conn

    def require_account_id():
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return None
        token = header[len("Bearer "):]
        return accounts.account_id_for_token(get_conn(), token)

    @app.post("/auth/login")
    def login():
        body = request.get_json(silent=True) or {}
        email = body.get("email")
        password = body.get("password")
        if not email or not password:
            return jsonify(error="email und password sind erforderlich"), 400

        account = accounts.get_account_by_email(get_conn(), email)
        if account is None or not check_password_hash(account.password_hash, password):
            return jsonify(error="Ungueltige Zugangsdaten"), 401

        token = accounts.create_session(get_conn(), account.account_id)
        return jsonify(token=token), 200

    @app.get("/accounts/me/tenants")
    def me_tenants():
        account_id = require_account_id()
        if account_id is None:
            return jsonify(error="Nicht authentifiziert"), 401

        tenants = accounts.tenants_for_account(get_conn(), account_id)
        return jsonify([{"tenant_id": t.tenant_id, "profile_id": t.profile_id} for t in tenants]), 200

    app._require_account_id = require_account_id  # reused by Task 4's /provision route
    app._get_conn = get_conn
    return app
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_accounts_api.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/heizungsserver/accounts_api.py tests/test_accounts_api.py
git commit -m "feat(accounts): add login and tenants Flask routes"
```

---

### Task 4: `accounts_api.py` — provision route

**Files:**
- Modify: `src/heizungsserver/accounts_api.py`
- Test: `tests/test_accounts_api.py`

**Interfaces:**
- Consumes: `onboarding.generate_installation() -> Installation(anlagen_id, password)` (existing), `smartheat_acl.format_smartheat_acl_snippet` (Task 2), `accounts.any_account_has_tenant`/`tenant_belongs_to_account` (Task 1).
- Produces: `POST /tenants/<tenant_id>/provision` route. Response body on success: `{"username": str, "password": str, "mosquitto_passwd_command": str, "acl_snippet": str}`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_accounts_api.py` (append; add `import json` at the top of the file):
```python
def test_provision_returns_credentials_for_linked_tenant(tmp_path):
    app, account_id, db_path = _app_with_account(tmp_path)
    conn = accounts.init_db(db_path)
    accounts.link_tenant(conn, account_id, "wohnung1", "vaillant_gastherme_heizkoerper")
    conn.close()
    client = app.test_client()
    token = client.post(
        "/auth/login", json={"email": "luca@example.com", "password": "geheim123"}
    ).get_json()["token"]

    response = client.post(
        "/tenants/wohnung1/provision",
        json={"profile_id": "vaillant_gastherme_heizkoerper"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["username"].startswith("wohnung1_")
    assert "smartheat/wohnung1/up/#" in body["acl_snippet"]
    with open(app.config["TENANTS_JSON_PATH"]) as f:
        registry = json.load(f)
    assert {"tenant_id": "wohnung1", "profile_id": "vaillant_gastherme_heizkoerper"} in registry


def test_provision_returns_403_for_tenant_linked_to_another_account(tmp_path):
    app, _, db_path = _app_with_account(tmp_path)
    conn = accounts.init_db(db_path)
    other_account_id = accounts.create_account(conn, "other@example.com", generate_password_hash("x"))
    accounts.link_tenant(conn, other_account_id, "wohnung-fremd", "vaillant_gastherme_heizkoerper")
    conn.close()
    client = app.test_client()
    token = client.post(
        "/auth/login", json={"email": "luca@example.com", "password": "geheim123"}
    ).get_json()["token"]

    response = client.post(
        "/tenants/wohnung-fremd/provision",
        json={"profile_id": "vaillant_gastherme_heizkoerper"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403


def test_provision_returns_404_for_completely_unknown_tenant(tmp_path):
    app, _, _ = _app_with_account(tmp_path)
    client = app.test_client()
    token = client.post(
        "/auth/login", json={"email": "luca@example.com", "password": "geheim123"}
    ).get_json()["token"]

    response = client.post(
        "/tenants/nirgendwo/provision",
        json={"profile_id": "vaillant_gastherme_heizkoerper"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404


def test_provision_requires_authentication(tmp_path):
    app, _, _ = _app_with_account(tmp_path)
    client = app.test_client()

    response = client.post("/tenants/wohnung1/provision", json={"profile_id": "x"})

    assert response.status_code == 401
```
Note: `generate_password_hash` must be imported at the top of the test file (`from werkzeug.security import generate_password_hash`) — it already is, from Task 3's fixture helper.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_accounts_api.py -v -k provision`
Expected: FAIL with 404 (route not found) for all four new tests

- [ ] **Step 3: Implement the provision route**

Add to `src/heizungsserver/accounts_api.py` (imports at top: `import json`, `from pathlib import Path`, `from heizungsserver import onboarding`, `from heizungsserver.smartheat_acl import format_smartheat_acl_snippet`; route added inside `create_app`, before `return app`):
```python
    @app.post("/tenants/<tenant_id>/provision")
    def provision(tenant_id):
        account_id = require_account_id()
        if account_id is None:
            return jsonify(error="Nicht authentifiziert"), 401

        conn = get_conn()
        if not accounts.tenant_belongs_to_account(conn, account_id, tenant_id):
            if accounts.any_account_has_tenant(conn, tenant_id):
                return jsonify(error="Tenant gehoert nicht zu diesem Account"), 403
            return jsonify(error="Unbekannter Tenant"), 404

        body = request.get_json(silent=True) or {}
        profile_id = body.get("profile_id")
        if not profile_id:
            return jsonify(error="profile_id ist erforderlich"), 400

        installation = onboarding.generate_installation()
        username = f"{tenant_id}_{installation.anlagen_id}"
        acl_snippet = format_smartheat_acl_snippet(username, tenant_id)
        _append_tenant_to_registry(app.config["TENANTS_JSON_PATH"], tenant_id, profile_id)

        return jsonify(
            username=username,
            password=installation.password,
            mosquitto_passwd_command=f"mosquitto_passwd -b /etc/mosquitto/passwd {username} <PASSWORD>",
            acl_snippet=acl_snippet,
        ), 200
```
Add this module-level helper function (outside `create_app`, near the bottom of the file):
```python
def _append_tenant_to_registry(path: str, tenant_id: str, profile_id: str) -> None:
    registry_path = Path(path)
    entries = json.loads(registry_path.read_text()) if registry_path.exists() else []
    entries = [e for e in entries if e.get("tenant_id") != tenant_id]
    entries.append({"tenant_id": tenant_id, "profile_id": profile_id})
    registry_path.write_text(json.dumps(entries, indent=2))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_accounts_api.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add src/heizungsserver/accounts_api.py tests/test_accounts_api.py
git commit -m "feat(accounts): add tenant provisioning route, append to tenants.json registry"
```

*(End of Part 1. Push the branch when this plan's execution reaches the end — see "Wrap-up" below.)*

---

## Part 2: heizungsbruecke (Ingress-Wizard)

All Part 2 tasks continue on the current worktree branch (`worktree-heizungsbruecke-config-vereinfachung`), working directory `C:\Users\Luca\Documents\HomeAssistant Dev\SmartHeat-for-HomeAssistant\.claude\worktrees\heizungsbruecke-config-vereinfachung\heizungsbruecke`.

### Task 5: `ha_api.py` — `list_states()`

**Files:**
- Modify: `src/heizungsbruecke/ha_api.py`
- Test: `tests/test_ha_api.py`

**Interfaces:**
- Produces: `HomeAssistantApi.list_states(self) -> list[dict]` — full `GET /core/api/states` response (list of HA state objects, each with at least `entity_id`, `state`, `attributes`).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_ha_api.py`:
```python
def test_list_states_returns_full_states_list():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock()
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = [
        {"entity_id": "sensor.outdoor", "state": "5.0", "attributes": {"unit_of_measurement": "°C"}},
        {"entity_id": "number.curve", "state": "0.9", "attributes": {}},
    ]

    with patch("heizungsbruecke.ha_api.requests.get", return_value=mock_response) as mock_get:
        result = api.list_states()

    assert result == mock_response.json.return_value
    mock_get.assert_called_once_with(
        "http://supervisor/core/api/states",
        headers={"Authorization": "Bearer test-token"},
        timeout=10,
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ha_api.py -k list_states -v`
Expected: FAIL with `AttributeError: 'HomeAssistantApi' object has no attribute 'list_states'`

- [ ] **Step 3: Implement `list_states()`**

Add to `HomeAssistantApi` in `src/heizungsbruecke/ha_api.py` (after `get_state`):
```python
    def list_states(self) -> list[dict]:
        response = requests.get(
            f"{self._base_url}{self._api_prefix}/states",
            headers=self._headers,
            timeout=10,
        )
        response.raise_for_status()
        return response.json()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ha_api.py -k list_states -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/heizungsbruecke/ha_api.py tests/test_ha_api.py
git commit -m "feat(ha_api): add list_states() for the wizard's entity dropdowns"
```

---

### Task 6: `supervisor_api.py` — write add-on options

**Files:**
- Create: `src/heizungsbruecke/supervisor_api.py`
- Test: `tests/test_supervisor_api.py`

**Interfaces:**
- Produces: `set_own_options(base_url: str, token: str, options: dict) -> None` — `POST {base_url}/addons/self/options`, raises on HTTP error (same style as `ha_api.py`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_supervisor_api.py`:
```python
from unittest.mock import Mock, patch

import pytest

from heizungsbruecke.supervisor_api import set_own_options


def test_set_own_options_posts_correct_payload():
    mock_response = Mock()
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.supervisor_api.requests.post", return_value=mock_response) as mock_post:
        set_own_options("http://supervisor", "test-token", {"tenant_id": "wohnung1"})

    mock_post.assert_called_once_with(
        "http://supervisor/addons/self/options",
        headers={"Authorization": "Bearer test-token"},
        json={"options": {"tenant_id": "wohnung1"}},
        timeout=10,
    )


def test_set_own_options_raises_on_http_error():
    mock_response = Mock()
    mock_response.raise_for_status.side_effect = RuntimeError("500")

    with patch("heizungsbruecke.supervisor_api.requests.post", return_value=mock_response):
        with pytest.raises(RuntimeError):
            set_own_options("http://supervisor", "test-token", {})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_supervisor_api.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'heizungsbruecke.supervisor_api'`

- [ ] **Step 3: Implement `supervisor_api.py`**

Create `src/heizungsbruecke/supervisor_api.py`:
```python
"""Duenner Client fuer die Supervisor-eigene Management-API (nicht die HA-Core-API,
die ha_api.py bedient -- anderer Pfad, kein `/core`-Praefix). Aktuell nur die eine
Operation, die der Wizard braucht: die eigenen Add-on-Optionen schreiben.
"""

import requests


def set_own_options(base_url: str, token: str, options: dict) -> None:
    response = requests.post(
        f"{base_url}/addons/self/options",
        headers={"Authorization": f"Bearer {token}"},
        json={"options": options},
        timeout=10,
    )
    response.raise_for_status()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_supervisor_api.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/heizungsbruecke/supervisor_api.py tests/test_supervisor_api.py
git commit -m "feat: add supervisor_api.set_own_options for the wizard's completion step"
```

---

### Task 7: `profiles.py` — profile catalog

**Files:**
- Modify: `src/heizungsbruecke/profiles.py`
- Test: `tests/test_profiles.py`

**Interfaces:**
- Produces: `ProfileCatalogEntry(hersteller, erzeuger_typ, verteilsystem, profile_id)` (frozen dataclass), `PROFILE_CATALOG: tuple[ProfileCatalogEntry, ...]`, `is_verified(profile_id: str) -> bool`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_profiles.py` (add `PROFILE_CATALOG`, `is_verified` to the existing import line from `heizungsbruecke.profiles`):
```python
def test_profile_catalog_entries_have_known_profile_ids():
    for entry in PROFILE_CATALOG:
        assert entry.profile_id in REQUIRED_ROLES_BY_PROFILE


def test_is_verified_true_for_profile_with_full_defaults():
    assert is_verified("vaillant_gastherme_heizkoerper") is True


def test_is_verified_false_for_profile_without_defaults():
    assert is_verified("weishaupt_waermepumpe_fussbodenheizung") is False


def test_is_verified_false_for_unknown_profile():
    assert is_verified("nicht_vorhanden") is False
```
(`REQUIRED_ROLES_BY_PROFILE` must also be imported if not already present in the file.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_profiles.py -k "catalog or is_verified" -v`
Expected: FAIL with `ImportError: cannot import name 'PROFILE_CATALOG'`

- [ ] **Step 3: Implement the catalog**

Add to `src/heizungsbruecke/profiles.py` (after the `PROFILE_LABELS`/`REQUIRED_ROLES_BY_PROFILE` block):
```python
@dataclass(frozen=True)
class ProfileCatalogEntry:
    hersteller: str
    erzeuger_typ: str
    verteilsystem: str
    profile_id: str


# Wizard-Katalog fuer die drei gekoppelten Dropdowns (Hersteller x Typ x
# Verteilsystem). Jeder Eintrag muss einen Schluessel in REQUIRED_ROLES_BY_PROFILE
# haben; ob er "verified" ist (also waehlbar ohne Warnung), entscheidet is_verified().
PROFILE_CATALOG: tuple[ProfileCatalogEntry, ...] = (
    ProfileCatalogEntry("Vaillant", "Gastherme", "Heizkoerper", "vaillant_gastherme_heizkoerper"),
    ProfileCatalogEntry("Weishaupt", "Waermepumpe", "Fussbodenheizung", "weishaupt_waermepumpe_fussbodenheizung"),
)


def is_verified(profile_id: str) -> bool:
    return profile_id in LOCAL_CLAMP_DEFAULTS and profile_id in LOCAL_BOOST_DEFAULTS
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_profiles.py -v`
Expected: PASS (all tests, including pre-existing ones)

- [ ] **Step 5: Commit**

```bash
git add src/heizungsbruecke/profiles.py tests/test_profiles.py
git commit -m "feat(profiles): add Hersteller/Typ/Verteilsystem catalog for the wizard"
```

---

### Task 8: `web.py` — app skeleton, `/api/login`, `/api/tenants`

**Files:**
- Create: `src/heizungsbruecke/web.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: nothing new yet (uses `requests` directly; `ha_api`/`supervisor_api` wired in but unused until Tasks 9–10).
- Produces: `create_app(heizungsserver_base_url: str, ha_api, supervisor_base_url: str, supervisor_token: str) -> Flask`. This exact signature is fixed from here on — Tasks 9–11 only add routes inside it, never change these four parameters. Routes so far: `POST /api/login`, `GET /api/tenants`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_web.py`:
```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_web.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'heizungsbruecke.web'`

- [ ] **Step 3: Implement the `web.py` skeleton**

Create `src/heizungsbruecke/web.py`:
```python
"""Flask-App fuer das Ingress-Panel: statische Wizard-Seite plus die JSON-Routen, die
sie gegen heizungsserver, ha_api und die Supervisor-Options-API aufruft. Jeder Schritt
scheitert laut auf Deutsch -- siehe Design-Spec, Abschnitt Fehlerbehandlung.
"""

import secrets

import requests
from flask import Flask, jsonify, request, session


def create_app(
    heizungsserver_base_url: str,
    ha_api,
    supervisor_base_url: str,
    supervisor_token: str,
) -> Flask:
    app = Flask(__name__, static_folder="static", static_url_path="")
    app.secret_key = secrets.token_hex(32)
    app.config["HEIZUNGSSERVER_BASE_URL"] = heizungsserver_base_url
    app.config["HA_API"] = ha_api
    app.config["SUPERVISOR_BASE_URL"] = supervisor_base_url
    app.config["SUPERVISOR_TOKEN"] = supervisor_token

    @app.post("/api/login")
    def login():
        body = request.get_json(silent=True) or {}
        email = body.get("email")
        password = body.get("password")
        if not email or not password:
            return jsonify(error="E-Mail und Passwort sind erforderlich"), 400

        try:
            response = requests.post(
                f"{app.config['HEIZUNGSSERVER_BASE_URL']}/auth/login",
                json={"email": email, "password": password},
                timeout=10,
            )
        except requests.RequestException:
            return jsonify(error="Abo-Service nicht erreichbar"), 502

        if response.status_code != 200:
            return jsonify(error="Ungueltige Zugangsdaten"), 401

        session["heizungsserver_token"] = response.json()["token"]
        return jsonify(ok=True), 200

    @app.get("/api/tenants")
    def tenants():
        token = session.get("heizungsserver_token")
        if not token:
            return jsonify(error="Nicht eingeloggt"), 401

        try:
            response = requests.get(
                f"{app.config['HEIZUNGSSERVER_BASE_URL']}/accounts/me/tenants",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
        except requests.RequestException:
            return jsonify(error="Abo-Service nicht erreichbar"), 502

        if response.status_code != 200:
            return jsonify(error="Sitzung abgelaufen, bitte erneut einloggen"), 401

        return jsonify(response.json()), 200

    return app
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_web.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/heizungsbruecke/web.py tests/test_web.py
git commit -m "feat(web): add Flask app skeleton with login and tenants routes"
```

---

### Task 9: `web.py` — `/api/profiles`, `/api/entities`

**Files:**
- Modify: `src/heizungsbruecke/web.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `profiles.PROFILE_CATALOG`, `profiles.is_verified` (Task 7); `ha_api.list_states()` (Task 5, via `app.config["HA_API"]`).
- Produces: `GET /api/profiles` -> `[{"hersteller", "erzeuger_typ", "verteilsystem", "profile_id", "verified"}]`. `GET /api/entities?domain=<domain>` -> `[{"entity_id", "friendly_name", "unit_of_measurement"}]`, `400` if `domain` missing.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_web.py`:
```python
def test_profiles_endpoint_lists_catalog_with_verified_flag():
    app = _app()
    app.testing = True
    client = app.test_client()

    response = client.get("/api/profiles")

    assert response.status_code == 200
    body = response.get_json()
    vaillant_entry = next(e for e in body if e["profile_id"] == "vaillant_gastherme_heizkoerper")
    weishaupt_entry = next(e for e in body if e["profile_id"] == "weishaupt_waermepumpe_fussbodenheizung")
    assert vaillant_entry["verified"] is True
    assert weishaupt_entry["verified"] is False


def test_entities_endpoint_filters_by_domain():
    ha_api = Mock()
    ha_api.list_states.return_value = [
        {"entity_id": "sensor.outdoor", "attributes": {"friendly_name": "Aussen", "unit_of_measurement": "°C"}},
        {"entity_id": "number.curve", "attributes": {"friendly_name": "Kurve"}},
    ]
    app = _app(ha_api=ha_api)
    app.testing = True
    client = app.test_client()

    response = client.get("/api/entities?domain=sensor")

    assert response.status_code == 200
    assert response.get_json() == [
        {"entity_id": "sensor.outdoor", "friendly_name": "Aussen", "unit_of_measurement": "°C"},
    ]


def test_entities_endpoint_requires_domain_param():
    app = _app()
    app.testing = True
    client = app.test_client()

    response = client.get("/api/entities")

    assert response.status_code == 400
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_web.py -k "profiles_endpoint or entities_endpoint" -v`
Expected: FAIL with 404 (routes don't exist yet)

- [ ] **Step 3: Implement the routes**

Add `from heizungsbruecke import profiles` to the imports at the top of `src/heizungsbruecke/web.py`. Add inside `create_app`, before `return app`:
```python
    @app.get("/api/profiles")
    def profile_catalog():
        return jsonify([
            {
                "hersteller": entry.hersteller,
                "erzeuger_typ": entry.erzeuger_typ,
                "verteilsystem": entry.verteilsystem,
                "profile_id": entry.profile_id,
                "verified": profiles.is_verified(entry.profile_id),
            }
            for entry in profiles.PROFILE_CATALOG
        ]), 200

    @app.get("/api/entities")
    def entities():
        domain = request.args.get("domain")
        if not domain:
            return jsonify(error="domain-Parameter ist erforderlich"), 400

        states = app.config["HA_API"].list_states()
        matching = [
            {
                "entity_id": state["entity_id"],
                "friendly_name": state.get("attributes", {}).get("friendly_name", state["entity_id"]),
                "unit_of_measurement": state.get("attributes", {}).get("unit_of_measurement"),
            }
            for state in states
            if state["entity_id"].split(".", 1)[0] == domain
        ]
        return jsonify(matching), 200
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_web.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add src/heizungsbruecke/web.py tests/test_web.py
git commit -m "feat(web): add profile catalog and entity-listing routes"
```

---

### Task 10: `web.py` — `/api/complete`

**Files:**
- Modify: `src/heizungsbruecke/web.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `supervisor_api.set_own_options` (Task 6); `profiles.is_verified` (Task 7).
- Produces: `POST /api/complete` with body `{"tenant_id": str, "profile_id": str, "entities": {role: {"entity_id": str, "unit_of_measurement": str|None}}}`. On success: `200 {"ok": true, "message": "..."}`. Validation failures: `400`. Provisioning failures: relays heizungsserver's status code. Options-write failures: `502`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_web.py` (this helper is reused by all three new tests — add it once, above them):
```python
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


def test_complete_rejects_unverified_profile():
    app = _app()
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
    app = _app()
    app.testing = True
    client = app.test_client()
    _login(client)

    bad_entities = dict(_VALID_ENTITIES)
    bad_entities["entity_room_actual"] = {"entity_id": "sensor.wrong", "unit_of_measurement": "%"}

    response = client.post("/api/complete", json={
        "tenant_id": "wohnung1",
        "profile_id": "vaillant_gastherme_heizkoerper",
        "entities": bad_entities,
    })

    assert response.status_code == 400


def test_complete_provisions_and_writes_options_on_success():
    app = _app()
    app.testing = True
    client = app.test_client()
    _login(client)

    provision_response = Mock(status_code=200)
    provision_response.json.return_value = {"username": "wohnung1_a1b2", "password": "geheim"}

    with patch("heizungsbruecke.web.requests.post", return_value=provision_response), \
         patch("heizungsbruecke.web.supervisor_api.set_own_options") as mock_set_options:
        response = client.post("/api/complete", json={
            "tenant_id": "wohnung1",
            "profile_id": "vaillant_gastherme_heizkoerper",
            "entities": _VALID_ENTITIES,
        })

    assert response.status_code == 200
    mock_set_options.assert_called_once()
    written_options = mock_set_options.call_args.args[2]
    assert written_options["tenant_id"] == "wohnung1"
    assert written_options["profile"] == "vaillant_gastherme_heizkoerper"
    assert written_options["entity_room_actual"] == "sensor.rt"


def test_complete_relays_provisioning_failure():
    app = _app()
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_web.py -k complete -v`
Expected: FAIL with 404 (route doesn't exist yet)

- [ ] **Step 3: Implement `/api/complete`**

Add `from heizungsbruecke import supervisor_api` to the imports at the top of `src/heizungsbruecke/web.py`. Add module-level constant (above `create_app`):
```python
_ROLE_UNIT_EXPECTATIONS = {
    "entity_room_actual": "°C",
    "entity_room_target": "°C",
    "entity_outdoor_temp": "°C",
    "entity_offset_current": "°C",
    "entity_heat_limit": "°C",
}
_REQUIRED_ENTITY_ROLES = (*_ROLE_UNIT_EXPECTATIONS, "entity_curve_current")
```
Add inside `create_app`, before `return app`:
```python
    @app.post("/api/complete")
    def complete():
        token = session.get("heizungsserver_token")
        if not token:
            return jsonify(error="Nicht eingeloggt"), 401

        body = request.get_json(silent=True) or {}
        tenant_id = body.get("tenant_id")
        profile_id = body.get("profile_id")
        entities = body.get("entities", {})

        if not tenant_id or not profile_id:
            return jsonify(error="tenant_id und profile_id sind erforderlich"), 400

        if not profiles.is_verified(profile_id):
            return jsonify(error=f"Profil '{profile_id}' hat noch keine verifizierten Standardwerte"), 400

        for role in _REQUIRED_ENTITY_ROLES:
            entity = entities.get(role)
            if not entity or not entity.get("entity_id"):
                return jsonify(error=f"Feld '{role}' ist erforderlich"), 400
            expected_unit = _ROLE_UNIT_EXPECTATIONS.get(role)
            if expected_unit is not None and entity.get("unit_of_measurement") != expected_unit:
                return jsonify(
                    error=f"'{role}': erwartete Einheit '{expected_unit}', gefunden "
                          f"'{entity.get('unit_of_measurement')}'"
                ), 400

        try:
            provision_response = requests.post(
                f"{app.config['HEIZUNGSSERVER_BASE_URL']}/tenants/{tenant_id}/provision",
                json={"profile_id": profile_id},
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
        except requests.RequestException:
            return jsonify(error="Abo-Service nicht erreichbar"), 502

        if provision_response.status_code != 200:
            return jsonify(error="Provisioning fehlgeschlagen"), provision_response.status_code

        options = {
            "tenant_id": tenant_id,
            "profile": profile_id,
            **{role: entity["entity_id"] for role, entity in entities.items()},
        }
        try:
            supervisor_api.set_own_options(
                app.config["SUPERVISOR_BASE_URL"], app.config["SUPERVISOR_TOKEN"], options,
            )
        except requests.RequestException:
            return jsonify(error="Speichern der Add-on-Optionen fehlgeschlagen"), 502

        return jsonify(ok=True, message="Konfiguration gespeichert - Add-on bitte manuell neu starten"), 200
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_web.py -v`
Expected: PASS (13 tests)

- [ ] **Step 5: Commit**

```bash
git add src/heizungsbruecke/web.py tests/test_web.py
git commit -m "feat(web): add /api/complete -- validate, provision, write add-on options"
```

---

### Task 11: static wizard page + `GET /`

**Files:**
- Create: `src/heizungsbruecke/static/index.html`
- Create: `src/heizungsbruecke/static/wizard.js`
- Modify: `src/heizungsbruecke/web.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Produces: `GET /` serves `static/index.html`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_web.py`:
```python
def test_index_serves_wizard_page():
    app = _app()
    app.testing = True
    client = app.test_client()

    response = client.get("/")

    assert response.status_code == 200
    assert b"SmartHeat Einrichtung" in response.data
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_web.py -k index -v`
Expected: FAIL with 404

- [ ] **Step 3: Add the `GET /` route**

Add inside `create_app`, before `return app`:
```python
    @app.get("/")
    def index():
        return app.send_static_file("index.html")
```

- [ ] **Step 4: Create the wizard page**

Create `src/heizungsbruecke/static/index.html`:
```html
<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>SmartHeat Einrichtung</title>
<style>
  body { font-family: sans-serif; max-width: 480px; margin: 2rem auto; }
  .step { display: none; }
  .step.active { display: block; }
  .error { color: #b00020; }
  label { display: block; margin-top: 0.75rem; }
  select, input, button { width: 100%; padding: 0.4rem; margin-top: 0.25rem; }
</style>
</head>
<body>
<h1>SmartHeat Einrichtung</h1>
<p class="error" id="error" hidden></p>

<section class="step active" id="step-login">
  <h2>1. Anmelden</h2>
  <label>E-Mail <input type="email" id="login-email"></label>
  <label>Passwort <input type="password" id="login-password"></label>
  <button id="login-submit">Anmelden</button>
</section>

<section class="step" id="step-tenant">
  <h2>2. Anlage waehlen</h2>
  <label>Anlage <select id="tenant-select"></select></label>
  <button id="tenant-next">Weiter</button>
</section>

<section class="step" id="step-profile">
  <h2>3. Anlagentyp</h2>
  <label>Hersteller <select id="profile-hersteller"></select></label>
  <label>Erzeuger-Typ <select id="profile-typ"></select></label>
  <label>Verteilsystem <select id="profile-verteilsystem"></select></label>
  <p class="error" id="profile-warning" hidden>Diese Kombination hat noch keine verifizierten Standardwerte.</p>
  <button id="profile-next">Weiter</button>
</section>

<section class="step" id="step-sensors">
  <h2>4. Sensoren zuordnen</h2>
  <div id="sensor-fields"></div>
  <button id="sensors-next">Bestaetigen</button>
</section>

<section class="step" id="step-done">
  <h2>Fertig</h2>
  <p id="done-message"></p>
</section>

<script src="wizard.js"></script>
</body>
</html>
```

Create `src/heizungsbruecke/static/wizard.js`:
```javascript
const ROLE_LABELS = {
  entity_room_actual: "Ist-Temperatur Referenzraum",
  entity_room_target: "Soll-Temperatur Referenzraum",
  entity_outdoor_temp: "Aussentemperatur",
  entity_curve_current: "Heizkurve (aktuell)",
  entity_offset_current: "Niveau/Offset (aktuell)",
  entity_heat_limit: "Heizgrenze",
};
const ROLE_DOMAINS = {
  entity_room_actual: ["sensor", "climate"],
  entity_room_target: ["sensor", "climate"],
  entity_outdoor_temp: ["sensor"],
  entity_curve_current: ["number"],
  entity_offset_current: ["number"],
  entity_heat_limit: ["number", "sensor"],
};
// A climate.* entity has no single numeric state -- room_actual/room_target must
// reference one of its temperature attributes, matching ha_api.get_state()'s
// existing "entity_id::attribute" convention. Flattened directly into the option
// value here instead of a second dependent dropdown -- one fewer moving part for
// the same outcome. HA's climate entities report in the install's global unit
// (°C for this customer base), so the unit check for these two roles trusts that
// rather than reading a per-attribute unit_of_measurement HA doesn't expose here.
const CLIMATE_ATTRIBUTE_BY_ROLE = {
  entity_room_actual: "current_temperature",
  entity_room_target: "temperature",
};

let profileCatalog = [];

function showStep(id) {
  document.querySelectorAll(".step").forEach((el) => el.classList.remove("active"));
  document.getElementById(id).classList.add("active");
}

function showError(message) {
  const el = document.getElementById("error");
  el.textContent = message;
  el.hidden = !message;
}

async function apiFetch(url, options) {
  const response = await fetch(url, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.error || `Fehler (${response.status})`);
  }
  return body;
}

document.getElementById("login-submit").addEventListener("click", async () => {
  showError("");
  try {
    await apiFetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email: document.getElementById("login-email").value,
        password: document.getElementById("login-password").value,
      }),
    });
    const tenants = await apiFetch("/api/tenants");
    const select = document.getElementById("tenant-select");
    select.innerHTML = tenants.map((t) => `<option value="${t.tenant_id}">${t.tenant_id}</option>`).join("");
    showStep("step-tenant");
  } catch (error) {
    showError(error.message);
  }
});

document.getElementById("tenant-next").addEventListener("click", async () => {
  showError("");
  try {
    profileCatalog = await apiFetch("/api/profiles");
    populateHersteller();
    showStep("step-profile");
  } catch (error) {
    showError(error.message);
  }
});

function populateHersteller() {
  const herstellerSelect = document.getElementById("profile-hersteller");
  const hersteller = [...new Set(profileCatalog.map((e) => e.hersteller))];
  herstellerSelect.innerHTML = hersteller.map((h) => `<option value="${h}">${h}</option>`).join("");
  populateTyp();
}

function populateTyp() {
  const hersteller = document.getElementById("profile-hersteller").value;
  const typSelect = document.getElementById("profile-typ");
  const typen = [...new Set(profileCatalog.filter((e) => e.hersteller === hersteller).map((e) => e.erzeuger_typ))];
  typSelect.innerHTML = typen.map((t) => `<option value="${t}">${t}</option>`).join("");
  populateVerteilsystem();
}

function populateVerteilsystem() {
  const hersteller = document.getElementById("profile-hersteller").value;
  const typ = document.getElementById("profile-typ").value;
  const verteilSelect = document.getElementById("profile-verteilsystem");
  const systeme = profileCatalog.filter((e) => e.hersteller === hersteller && e.erzeuger_typ === typ);
  verteilSelect.innerHTML = systeme.map((e) => `<option value="${e.verteilsystem}">${e.verteilsystem}</option>`).join("");
  updateProfileWarning();
}

function currentProfileEntry() {
  const hersteller = document.getElementById("profile-hersteller").value;
  const typ = document.getElementById("profile-typ").value;
  const verteilsystem = document.getElementById("profile-verteilsystem").value;
  return profileCatalog.find(
    (e) => e.hersteller === hersteller && e.erzeuger_typ === typ && e.verteilsystem === verteilsystem
  );
}

function updateProfileWarning() {
  const entry = currentProfileEntry();
  const warning = document.getElementById("profile-warning");
  const nextButton = document.getElementById("profile-next");
  const blocked = !entry || !entry.verified;
  warning.hidden = !blocked;
  nextButton.disabled = blocked;
}

document.getElementById("profile-hersteller").addEventListener("change", populateTyp);
document.getElementById("profile-typ").addEventListener("change", populateVerteilsystem);
document.getElementById("profile-verteilsystem").addEventListener("change", updateProfileWarning);

document.getElementById("profile-next").addEventListener("click", async () => {
  showError("");
  const container = document.getElementById("sensor-fields");
  container.innerHTML = "";
  for (const [role, label] of Object.entries(ROLE_LABELS)) {
    let rawEntities = [];
    for (const domain of ROLE_DOMAINS[role]) {
      rawEntities = rawEntities.concat(await apiFetch(`/api/entities?domain=${domain}`));
    }
    const climateAttribute = CLIMATE_ATTRIBUTE_BY_ROLE[role];
    const options = rawEntities.map((e) => {
      if (e.entity_id.startsWith("climate.") && climateAttribute) {
        return { value: `${e.entity_id}::${climateAttribute}`, label: e.friendly_name, unit: "°C" };
      }
      return { value: e.entity_id, label: e.friendly_name, unit: e.unit_of_measurement || "" };
    });
    const wrapper = document.createElement("label");
    wrapper.textContent = label;
    const select = document.createElement("select");
    select.dataset.role = role;
    select.innerHTML = options
      .map((o) => `<option value="${o.value}" data-unit="${o.unit}">${o.label}</option>`)
      .join("");
    wrapper.appendChild(select);
    container.appendChild(wrapper);
  }
  showStep("step-sensors");
});

document.getElementById("sensors-next").addEventListener("click", async () => {
  showError("");
  const entities = {};
  document.querySelectorAll("#sensor-fields select").forEach((select) => {
    const chosenOption = select.options[select.selectedIndex];
    entities[select.dataset.role] = {
      entity_id: select.value,
      unit_of_measurement: chosenOption ? chosenOption.dataset.unit || null : null,
    };
  });

  const entry = currentProfileEntry();
  try {
    const result = await apiFetch("/api/complete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tenant_id: document.getElementById("tenant-select").value,
        profile_id: entry.profile_id,
        entities,
      }),
    });
    document.getElementById("done-message").textContent = result.message;
    showStep("step-done");
  } catch (error) {
    showError(error.message);
  }
});
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_web.py -v`
Expected: PASS (14 tests)

- [ ] **Step 6: Commit**

```bash
git add src/heizungsbruecke/web.py src/heizungsbruecke/static/index.html src/heizungsbruecke/static/wizard.js tests/test_web.py
git commit -m "feat(web): add the static wizard page (login through sensor confirmation)"
```

---

### Task 12: `__main__.py` restructure + `config.yaml` + `pyproject.toml` + `DOCS.md`

**Files:**
- Modify: `src/heizungsbruecke/__main__.py`
- Modify: `config.yaml`
- Modify: `pyproject.toml`
- Modify: `DOCS.md`
- Test: `tests/test_main.py`

**Interfaces:**
- Produces: `_is_configured(options: dict) -> bool`, `_run_bridge(options: dict, ha_api) -> None` (replaces the body that used to live directly in `main()`; returns instead of `sys.exit()` on any validation failure, and returns immediately if `_is_configured` is `False`). `main()` now starts `_run_bridge` in a background thread and runs `web.create_app(...)` in the foreground.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_main.py`. First add `_is_configured` and `_run_bridge` to the existing import block from `heizungsbruecke.__main__` (keep every existing imported name — this only adds two):
```python
def test_is_configured_true_when_all_required_fields_present():
    options = {
        "tenant_id": "wohnung1", "profile": "vaillant_gastherme_heizkoerper",
        "entity_room_actual": "sensor.rt", "entity_room_target": "sensor.target_rt",
        "entity_curve_current": "number.curve", "entity_offset_current": "number.offset",
        "entity_outdoor_temp": "sensor.outdoor", "entity_heat_limit": "number.heat_limit",
    }
    assert _is_configured(options) is True


def test_is_configured_false_when_a_required_field_is_missing():
    assert _is_configured({"tenant_id": "wohnung1"}) is False


def test_is_configured_false_for_empty_options():
    assert _is_configured({}) is False


def test_run_bridge_returns_early_without_raising_when_not_configured():
    _run_bridge({}, MagicMock())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_main.py -k "is_configured or run_bridge_returns_early" -v`
Expected: FAIL with `ImportError: cannot import name '_is_configured'`

- [ ] **Step 3: Restructure `__main__.py`**

Replace the entire contents of `src/heizungsbruecke/__main__.py` with:
```python
import json
import logging
import os
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from heizungsbruecke.backup_store import load_backup, save_backup
from heizungsbruecke.boost import decide_boost
from heizungsbruecke.bridge import apply_boost_decision, handle_down_message, publish_snapshot
from heizungsbruecke import daynight_snapshot, derived_sensors, web
from heizungsbruecke.failsafe import (
    FailsafeState,
    build_discovery_config,
    build_state_payload,
    enter_failsafe_if_stale,
    record_valid_message,
)
from heizungsbruecke.ha_api import HomeAssistantApi
from heizungsbruecke.manifest import ManifestError, build_manifest
from heizungsbruecke.mqtt_client import BridgeMqttClient
from heizungsbruecke.profiles import UnknownProfileError, resolve_boost_defaults, resolve_local_clamps

OPTIONS_PATH = Path("/data/options.json")
BACKUP_PATH = Path("/data/backup.json")
FAILSAFE_PATH = Path("/data/failsafe_state.json")
DERIVED_SENSORS_PATH = Path("/data/derived_sensors.json")
DAYNIGHT_SNAPSHOT_PATH = Path("/data/daynight_snapshot_state.json")

MQTT_HOST = "127.0.0.1"
MQTT_PORT = 18830
INGRESS_PORT = 8099

_REQUIRED_OPTIONS = (
    "tenant_id", "profile", "entity_room_actual", "entity_room_target",
    "entity_curve_current", "entity_offset_current", "entity_outdoor_temp", "entity_heat_limit",
)

# The add-on runs with `startup: services`, i.e. it can be started before HA Core has
# finished booting. A transient failure here (HA API not answering yet) must not be fatal
# on the first attempt -- retry with backoff before giving up. No config.yaml `watchdog`
# is set on purpose: once retries are exhausted the failure is treated as a genuine
# misconfiguration, and _run_bridge() returns (logs, doesn't crash the whole process)
# rather than crash-looping forever.
DERIVED_SENSORS_RETRY_DELAYS_SECONDS = (5, 10, 20, 40, 60, 60, 60)

logger = logging.getLogger(__name__)


def _is_configured(options: dict) -> bool:
    """Ab 0.6.0 hat config.yaml keine Pflichtfelder mehr -- der Einrichtungs-Assistent
    (Ingress-Panel) ist der einzige Konfigurationsweg. Ein frisch installiertes, noch
    nicht eingerichtetes Add-on hat also ein leeres oder unvollstaendiges options.json;
    das ist ab jetzt ein normaler Zustand, kein Fehler.
    """
    return all(options.get(field) for field in _REQUIRED_OPTIONS)


def _resolve_effective_options(options: dict) -> dict:
    clamps = resolve_local_clamps(options["profile"])
    boost = resolve_boost_defaults(options["profile"])
    return {
        **options,
        "curve_min": clamps.curve_min,
        "curve_max": clamps.curve_max,
        "offset_min": clamps.offset_min,
        "offset_max": clamps.offset_max,
        "boost_threshold_k": boost.threshold_k,
        "boost_curve_value": boost.curve_value,
        "boost_offset_value": boost.offset_value,
    }


def _validate_boost_config(options: dict) -> str | None:
    """Returns a German error message if the configured boost values fall outside
    the configured safety clamps, or None if the config is valid. A misconfigured
    boost value is a startup-time error, not something to silently clamp, since the
    boost path is the one write path that runs with no server oversight.
    """
    curve_min, curve_max = options["curve_min"], options["curve_max"]
    offset_min, offset_max = options["offset_min"], options["offset_max"]
    boost_curve_value = options["boost_curve_value"]
    boost_offset_value = options["boost_offset_value"]

    if not (curve_min <= boost_curve_value <= curve_max):
        return (
            f"boost_curve_value ({boost_curve_value}) liegt ausserhalb des konfigurierten "
            f"Bereichs [curve_min={curve_min}, curve_max={curve_max}]"
        )
    if not (offset_min <= boost_offset_value <= offset_max):
        return (
            f"boost_offset_value ({boost_offset_value}) liegt ausserhalb des konfigurierten "
            f"Bereichs [offset_min={offset_min}, offset_max={offset_max}]"
        )
    return None


def _validate_derived_sensor_prerequisites(options: dict) -> str | None:
    """Returns a German error message if a field the automatic DAT/DART/day-night-avg
    provisioning needs (derived_sensors.ensure_all) is missing, or None if both are
    present. Checked explicitly, before ensure_all() runs, so a customer who forgot
    entity_outdoor_temp gets a clean startup error instead of a raw KeyError.
    """
    missing = [
        field for field in ("entity_room_actual", "entity_outdoor_temp")
        if not options.get(field)
    ]
    if missing:
        return (
            "Folgende Pflichtfelder fehlen in der Add-on-Konfiguration (werden fuer "
            f"automatisch berechnete Sensoren gebraucht): {', '.join(missing)}"
        )
    return None


def _ensure_derived_sensors_with_retry(ha_api, options: dict) -> dict[str, str]:
    """Wraps `derived_sensors.ensure_all` with retry-with-backoff (see
    DERIVED_SENSORS_RETRY_DELAYS_SECONDS above for the rationale) so a transient failure
    while HA Core is still starting up doesn't crash the whole add-on on the first try.
    """
    delays = DERIVED_SENSORS_RETRY_DELAYS_SECONDS
    last_error: Exception | None = None
    for attempt in range(len(delays) + 1):
        try:
            return derived_sensors.ensure_all(
                ha_api=ha_api,
                tenant_id=options["tenant_id"],
                room_actual_entity_id=options["entity_room_actual"],
                outdoor_temp_entity_id=options["entity_outdoor_temp"],
                state_path=DERIVED_SENSORS_PATH,
            )
        except Exception as error:
            last_error = error
            if attempt == len(delays):
                break
            logger.warning(
                "Anlegen der abgeleiteten Sensoren fehlgeschlagen (Versuch %s/%s, evtl. ist "
                "HA Core beim Start des Add-ons noch nicht bereit): %s",
                attempt + 1, len(delays) + 1, error,
            )
            time.sleep(delays[attempt])
    raise last_error


def _make_down_callback(role, manifest, ha_api, options, write_lock, failsafe_ctx, mqtt_client):
    def _callback(client, userdata, message):
        try:
            payload = json.loads(message.payload)
            with write_lock:
                handle_down_message(
                    role=role,
                    value=payload["v"],
                    manifest=manifest,
                    ha_api=ha_api,
                    curve_min=options["curve_min"],
                    curve_max=options["curve_max"],
                    offset_min=options["offset_min"],
                    offset_max=options["offset_max"],
                    backup_path=BACKUP_PATH,
                )
                _record_valid_message(failsafe_ctx, mqtt_client, FAILSAFE_PATH)
        except Exception:
            logger.exception("Fehler bei der Verarbeitung einer Down-Nachricht fuer Rolle '%s'", role)
    return _callback


def _load_failsafe_ctx(path: Path) -> dict:
    raw = load_backup(path)
    return {
        "last_valid_update": raw.get("last_valid_update"),
        "state": FailsafeState(
            active=raw.get("failsafe_active", False),
            recovery_count=raw.get("recovery_count", 0),
        ),
    }


def _load_failsafe_ctx_safe(path: Path) -> dict:
    """Wraps `_load_failsafe_ctx` so a corrupt/truncated state file (e.g. after power
    loss on the Pi's SD card) cannot crash the whole add-on at startup -- every other
    `load_backup` call site in this codebase runs inside a caller-provided try/except
    (see bridge.py's handle_down_message/apply_boost_decision), this is that guard for
    the fail-safe state file. Falls back to the same default context a missing file
    would produce.
    """
    try:
        return _load_failsafe_ctx(path)
    except Exception as error:
        logger.warning(
            "Fail-Safe-Zustandsdatei konnte nicht gelesen werden (%s), starte mit Standardzustand: %s",
            path, error,
        )
        return {
            "last_valid_update": None,
            "state": FailsafeState(active=False, recovery_count=0),
        }


def _save_failsafe_ctx(ctx: dict, path: Path) -> None:
    save_backup(path, {
        "last_valid_update": ctx["last_valid_update"],
        "failsafe_active": ctx["state"].active,
        "recovery_count": ctx["state"].recovery_count,
    })


def _record_valid_message(failsafe_ctx: dict, mqtt_client, failsafe_path: Path) -> None:
    failsafe_ctx["last_valid_update"] = time.time()
    new_state = record_valid_message(failsafe_ctx["state"])
    if new_state != failsafe_ctx["state"]:
        mqtt_client.publish_status("failsafe", build_state_payload(new_state.active))
    failsafe_ctx["state"] = new_state
    _save_failsafe_ctx(failsafe_ctx, failsafe_path)


def _check_failsafe_staleness(failsafe_ctx: dict, stale_after_seconds: float, mqtt_client, failsafe_path: Path) -> None:
    last = failsafe_ctx["last_valid_update"]
    seconds_since = (time.time() - last) if last is not None else None
    new_state = enter_failsafe_if_stale(failsafe_ctx["state"], seconds_since, stale_after_seconds)
    if new_state != failsafe_ctx["state"]:
        mqtt_client.publish_status("failsafe", build_state_payload(new_state.active))
        if new_state.active:
            logger.warning(
                "Fail-Safe aktiviert - seit ueber %s Sekunden kein gueltiger Live-Wert empfangen.",
                stale_after_seconds,
            )
        failsafe_ctx["state"] = new_state
        _save_failsafe_ctx(failsafe_ctx, failsafe_path)


def _run_tick(manifest, ha_api, mqtt_client, options, write_lock, boost_was_active: bool) -> bool:
    """Runs one poll cycle: publish the snapshot, then (if room roles are configured)
    evaluate and apply the local boost decision. Returns the boost-active state to
    carry into the next tick. An individual unreadable sensor only costs that role its
    snapshot value (handled inside publish_snapshot); any remaining I/O failure
    propagates -- the caller (_run_bridge's loop) is responsible for catching and
    logging so a single bad tick doesn't kill the whole process.
    """
    seq = str(uuid.uuid4())
    publish_snapshot(
        manifest=manifest,
        ha_api=ha_api,
        mqtt_client=mqtt_client,
        seq=seq,
        notify_service=options.get("notify_service", ""),
    )
    logger.info("Snapshot veroeffentlicht, seq=%s", seq)

    if "room_actual" in manifest.entity_ids and "room_target" in manifest.entity_ids:
        room_actual = ha_api.get_state(manifest.entity_ids["room_actual"])
        room_target = ha_api.get_state(manifest.entity_ids["room_target"])
        decision = decide_boost(
            room_actual=room_actual,
            room_target=room_target,
            threshold_k=options.get("boost_threshold_k", 0.5),
            boost_curve_value=options["boost_curve_value"],
            boost_offset_value=options["boost_offset_value"],
        )
        with write_lock:
            boost_was_active = apply_boost_decision(
                decision=decision,
                boost_was_active=boost_was_active,
                manifest=manifest,
                ha_api=ha_api,
                curve_min=options["curve_min"],
                curve_max=options["curve_max"],
                offset_min=options["offset_min"],
                offset_max=options["offset_max"],
                backup_path=BACKUP_PATH,
            )

    return boost_was_active


def _run_bridge(options: dict, ha_api) -> None:
    """Laeuft im Hintergrund-Thread (siehe main()); validiert/loest Optionen selbst auf
    und startet die Poll-Loop nur, wenn der Einrichtungs-Assistent das Add-on schon
    konfiguriert hat. Ein noch nicht eingerichtetes Add-on ist ab 0.6.0 ein normaler
    Zustand (siehe _is_configured) -- deshalb hier `return` statt `sys.exit(1)` bei
    jedem Validierungsfehler: ein `sys.exit` wuerde den ganzen Prozess beenden und damit
    auch den Ingress-Wizard unerreichbar machen, der genau dieses Problem beheben soll.
    """
    if not _is_configured(options):
        logger.info(
            "Add-on ist noch nicht eingerichtet -- bitte den Einrichtungs-Assistenten "
            "(Add-on-Panel 'SmartHeat Einrichtung') oeffnen. Die Poll-Loop startet erst "
            "nach abgeschlossener Einrichtung und einem manuellen Neustart des Add-ons."
        )
        return

    try:
        options = _resolve_effective_options(options)
    except UnknownProfileError as error:
        logger.error("FEHLER: %s", error)
        return

    boost_config_error = _validate_boost_config(options)
    if boost_config_error:
        logger.error("FEHLER: %s", boost_config_error)
        return

    prerequisite_error = _validate_derived_sensor_prerequisites(options)
    if prerequisite_error:
        logger.error("FEHLER: %s", prerequisite_error)
        return

    try:
        derived_entity_ids = _ensure_derived_sensors_with_retry(ha_api, options)
    except Exception as error:
        logger.error(
            "FEHLER: Anlegen der abgeleiteten Sensoren fehlgeschlagen nach %d Versuchen: %s",
            len(DERIVED_SENSORS_RETRY_DELAYS_SECONDS) + 1, error,
        )
        return

    try:
        manifest = build_manifest(options, derived_entity_ids)
    except ManifestError as error:
        logger.error("FEHLER: %s", error)
        return

    mqtt_client = BridgeMqttClient(host=MQTT_HOST, port=MQTT_PORT, tenant_id=options["tenant_id"])
    write_lock = threading.Lock()

    failsafe_ctx = _load_failsafe_ctx_safe(FAILSAFE_PATH)
    if failsafe_ctx["last_valid_update"] is None:
        # Fresh install / no prior record: measure staleness from process start, so a
        # server that never sends a single valid value still trips fail-safe eventually
        # instead of reading "OK" forever.
        failsafe_ctx["last_valid_update"] = time.time()
    stale_after_seconds = options.get("failsafe_stale_after_hours", 26.0) * 3600
    mqtt_client.publish_discovery(
        component="binary_sensor", object_id="failsafe",
        config=build_discovery_config(options["tenant_id"]),
    )
    mqtt_client.publish_status("failsafe", build_state_payload(failsafe_ctx["state"].active))

    for role in ("curve_current", "offset_current"):
        if role in manifest.entity_ids:
            mqtt_client.subscribe_down(
                role=role,
                on_message=_make_down_callback(role, manifest, ha_api, options, write_lock, failsafe_ctx, mqtt_client),
            )
    mqtt_client.loop_start()

    boost_was_active = False

    while True:
        try:
            boost_was_active = _run_tick(manifest, ha_api, mqtt_client, options, write_lock, boost_was_active)
        except Exception:
            logger.exception("Fehler im Poll-Loop, wird beim naechsten Tick erneut versucht")

        try:
            with write_lock:
                _check_failsafe_staleness(failsafe_ctx, stale_after_seconds, mqtt_client, FAILSAFE_PATH)
        except Exception:
            logger.exception("Fehler bei der Fail-Safe-Staleness-Pruefung, wird beim naechsten Tick erneut versucht")

        try:
            daynight_snapshot.maybe_snapshot(
                ha_api=ha_api,
                room_12h_avg_entity_id=derived_entity_ids["_room_12h_avg"],
                day_avg_entity_id=derived_entity_ids["room_day_avg"],
                night_avg_entity_id=derived_entity_ids["room_night_avg"],
                state_path=DAYNIGHT_SNAPSHOT_PATH,
                now=datetime.now(),
            )
        except Exception:
            logger.exception("Fehler beim Tag-/Nachtmittel-Snapshot, wird beim naechsten Tick erneut versucht")

        time.sleep(options.get("poll_interval_seconds", 3600))


def main() -> None:
    logging.basicConfig(level=logging.INFO)

    options = json.loads(OPTIONS_PATH.read_text()) if OPTIONS_PATH.exists() else {}
    ha_api = HomeAssistantApi(base_url="http://supervisor", token=os.environ["SUPERVISOR_TOKEN"])

    bridge_thread = threading.Thread(target=_run_bridge, args=(options, ha_api), daemon=True)
    bridge_thread.start()

    app = web.create_app(
        heizungsserver_base_url=os.environ["HEIZUNGSSERVER_BASE_URL"],
        ha_api=ha_api,
        supervisor_base_url="http://supervisor",
        supervisor_token=os.environ["SUPERVISOR_TOKEN"],
    )
    app.run(host="0.0.0.0", port=INGRESS_PORT)


if __name__ == "__main__":
    main()
```
Note: `sys` is still imported for consistency with the rest of the codebase's style but is no longer used for `sys.exit()` in this file — that's fine, an unused import is a minor lint nit, not a test failure; remove the `import sys` line if you prefer stricter cleanliness.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/ -v`
Expected: PASS (all tests, including every pre-existing one — `_run_bridge` preserves all prior `main()` logic, just relocated)

- [ ] **Step 5: Update `config.yaml`**

Replace the entire contents of `config.yaml` with:
```yaml
name: "Heizungsbruecke"
version: "0.6.0"
slug: "heizungsbruecke"
description: "Generische Bruecken-Logik: liest konfigurierte HA-Entities, meldet sie an den SmartHeat-Server, schreibt Sollwerte zurueck, mit lokalem Boost-Failsafe und Sicherheits-Clamps."
url: "https://github.com/LucaHartfuss/SmartHeat-for-HomeAssistant"
arch:
  - aarch64
  - amd64
startup: services
boot: auto
host_network: true
homeassistant_api: true
hassio_api: true
ingress: true
ingress_port: 8099
panel_icon: "mdi:thermometer-lines"
panel_title: "SmartHeat Einrichtung"
environment:
  HEIZUNGSSERVER_BASE_URL: "https://accounts.hartfussha.org"
```

- [ ] **Step 6: Add Flask to `pyproject.toml`**

In `pyproject.toml`, change the `dependencies` line to:
```toml
dependencies = ["paho-mqtt>=2.1,<3", "requests>=2.31,<3", "websocket-client>=1.8,<2", "flask>=3.0,<4"]
```
Then run: `pip install "flask>=3.0,<4"` (if not already installed from Part 1's Task 1, Step 5 in a shared environment).

- [ ] **Step 7: Add the 0.6.0 changelog entry to `DOCS.md`**

Add this section to `DOCS.md`, after the existing "## Update von 0.4.0 auf 0.5.0" section and before "## Voraussetzungen":
```markdown
## Update von 0.5.0 auf 0.6.0 (Breaking Change)

`tenant_id`, `profile`, `entity_room_actual`, `entity_room_target`,
`entity_curve_current`, `entity_offset_current`, `entity_outdoor_temp`,
`entity_heat_limit`, `poll_interval_seconds`, `notify_service` und
`failsafe_stale_after_hours` entfallen als Supervisor-Configuration-Tab-Felder
(`options`/`schema` komplett entfernt). Die Konfiguration laeuft ab jetzt
ausschliesslich ueber den neuen Einrichtungs-Assistenten: Add-on-Panel
("SmartHeat Einrichtung") oeffnen, mit dem SmartHeat-Account einloggen,
Anlage/Profil/Sensoren im gefuehrten Dialog waehlen, bestaetigen.

**Achtung bei bestehenden Installationen:** nach dem Update auf 0.6.0 sind die
bisherigen Configuration-Tab-Werte wirkungslos -- die Einrichtung muss einmal
ueber den neuen Assistenten wiederholt werden, danach das Add-on manuell neu
starten. `poll_interval_seconds`, `notify_service` und
`failsafe_stale_after_hours` behalten ihre bisherigen Defaults (3600s / leer /
26.0h), wenn der Assistent sie nicht abfragt -- fuer eine Aenderung dieser drei
optionalen Werte vorerst `options.json` auf dem Pi direkt anpassen (kein
UI-Schritt dafuer in dieser Version).
```
Also update the top-of-file summary paragraph (first ~8 lines of `DOCS.md`) if it still describes configuration as happening via `config.yaml` fields — adjust the wording to mention the Ingress-Wizard instead, keeping the rest of the paragraph intact.

- [ ] **Step 8: Run the full suite one more time**

Run: `python -m pytest tests/ -v`
Expected: PASS (all tests)

- [ ] **Step 9: Commit**

```bash
git add src/heizungsbruecke/__main__.py config.yaml pyproject.toml DOCS.md tests/test_main.py
git commit -m "feat: make the Ingress-Wizard the sole configuration path (0.6.0)"
```

---

## Wrap-up (not a task — do this after Task 12)

- Part 1 (heizungsserver) is on its own branch (`ingress-wizard-accounts-stub`) in a separate repository from Part 2. It is **not** part of this worktree's branch and won't be touched by `finishing-a-development-branch` for `heizungsbruecke`. Push it and open a PR (or merge locally) separately, following the same integration-decision process as any other branch — ask the user which they want, same as was done for Teil 1.
- Part 2 (heizungsbruecke) stays on `worktree-heizungsbruecke-config-vereinfachung` and goes through the normal whole-branch review before merging, same as Teil 1.
- After both are merged: run `superpowers:requesting-code-review` (or the whole-branch review this session already used for Teil 1) on **both** repos before declaring Teil 2 done, then verify the built features against the original Teil-2 description (Ingress-Wizard mit Auth, Tenant-Dropdown, Profil-Dreifach-Dropdown mit Kombinationspruefung, Sensor-Auswahl-Dropdowns mit Einheiten-Validierung) before moving to the live end-to-end test.
- Two spec-flagged open items remain genuinely unresolved and need a live decision, not more code: the Cloudflare tunnel hostname for `HEIZUNGSSERVER_BASE_URL` (`https://accounts.hartfussha.org` is written into `config.yaml` but not yet provisioned in Cloudflare), and whether the live Mosquitto broker's ACL actually has `smartheat/#` rights for the server user yet.
