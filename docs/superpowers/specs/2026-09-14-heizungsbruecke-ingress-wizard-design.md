# Heizungsbruecke Ingress-Wizard (Teil 2) - Design

Status: approved by user 2026-09-14, ready for writing-plans.

## Ueberblick

Teil 1 (Config-Vereinfachung, gemerged) hat DAT/DART/Tag-Nachtmittel-Berechnung
und Boost/Clamp-Defaults ins Add-on selbst verlagert. Was bleibt, ist ein
grundlegendes Problem: die Home-Assistant-Supervisor-Add-on-Konfiguration
(`config.yaml`/`schema`) hat **keinen Entity-/Device-Class-Selector** -- Nutzer
muessten Entity-IDs weiterhin als Freitext eintippen. Teil 2 loest das durch
einen eigenen Ingress-Wizard (eine vom Add-on selbst servierte Web-UI), der:

1. gegen einen Dummy-Account-/Abo-Service einloggt (Stub fuer den spaeteren
   echten Abo-Service),
2. den/die Tenant(s) dieses Accounts zur Auswahl anbietet,
3. Waermeerzeuger-Profil per drei gekoppelten Dropdowns (Hersteller x Typ x
   Verteilsystem) auswaehlen laesst, mit Kombinationspruefung gegen die
   verifizierten Profile,
4. fuer jede benoetigte Rolle echte HA-Entities zur Auswahl anbietet und beim
   Bestaetigen deren Einheiten validiert,
5. beim Abschluss die Add-on-Optionen schreibt UND das Tenant-Provisioning auf
   heizungsserver anstoesst.

## Ziele

- Der Wizard wird der **einzige** Weg, das Add-on zu konfigurieren; die
  bisherigen `options`/`schema`-Felder (`tenant_id`, `profile`, `entity_*`)
  werden aus `config.yaml` entfernt (Breaking Change, Version 0.6.0).
- Ein Account (Abo) kann mehrere Tenants verwalten (z.B. mehrere Wohnungen
  eines Nutzers).
- Die Profil-Kombinationspruefung verhindert, dass ein Nutzer eine
  Hersteller/Typ/Verteilsystem-Kombination waehlt, fuer die es noch keine
  verifizierten Clamp-/Boost-Defaults gibt.
- Sensor-Auswahl per echten HA-Entities (nicht Freitext), mit
  Einheiten-Validierung vor der Bestaetigung.
- Das Provisioning auf heizungsserver-Seite nutzt das **korrekte**
  `smartheat/<tenant_id>/...`-Topic-Schema, nicht das aeltere `hz/<anlagen_id>/...`-Schema von `onboarding.py`/`heizungsserver.daemon` (siehe
  "Bestehende Code-Realitaet" unten).

## Nicht-Ziele (YAGNI fuer diese Iteration)

- Kein echter Abo-/Payment-Service (bleibt Dummy/Stub, spaeter 1:1 ersetzbar).
- Keine automatische Mutation des laufenden Mosquitto-Brokers
  (`mosquitto_passwd`/ACL-Datei) durch die neue API -- sie liefert die
  auszufuehrenden Befehle, genau wie die bestehende `onboarding.py`-CLI heute.
  Automatisieren waere eine grundlegend andere Risikostufe (Mutation eines
  Produktivbrokers, an dem andere Kunden haengen) als dieses Feature-Set
  verlangt.
- Kein automatischer Restart des Add-ons nach dem Schreiben der Optionen
  (Nutzer macht das manuell, analog zu CLAUDE.md-Regel 3 fuer HA Core).
- Kein Browser-/E2E-Test in dieser Iteration; der manuelle Klick-Durchlauf
  passiert beim ohnehin geplanten Live-Test (Schritt C im
  Multi-Tenant-Rollout-Runbook).
- Keine Vereinheitlichung der beiden parallelen MQTT-Topic-Schemata
  (`hz/...` legacy vs. `smartheat/...` generic) in heizungsserver -- nur die
  neue ACL-Snippet-Funktion fuer's Provisioning nutzt korrekt `smartheat/...`.

## Bestehende Code-Realitaet (verifiziert, nicht angenommen)

- `heizungsserver` hat aktuell **keinen** HTTP-Server, nur einen synchronen
  MQTT-Daemon (`daemon.py`, `generic/daemon.py`) mit `sqlite3`-Zustand
  (`state.py`) -- kein asyncio, keine bestehende Web-Framework-Abhaengigkeit.
- `heizungsserver` hat **zwei parallele Multi-Tenant-Schemata**:
  - Legacy (`heizungsserver/daemon.py`, `__main__.py`, `onboarding.py`):
    Topic-Schema `hz/<anlagen_id>/...`, ACL-Beispiel in `deploy/acl.conf`.
  - Generic/aktuell (`heizungsserver/generic/*`): Topic-Schema
    `smartheat/<tenant_id>/...`, per `tenants.json` konfiguriert
    (`generic/tenant_config.py`), passend zu `heizungsbruecke`s
    `mqtt_client.py`.
  - `onboarding.py`s `generate_installation()` (Zufalls-ID + Passwort) ist
    schema-agnostisch und wiederverwendbar; `format_acl_snippet()` ist es
    **nicht** (hardcoded `hz/...`) -- fuer's neue Provisioning wird eine neue,
    zum `smartheat/...`-Schema passende Formatierungsfunktion gebraucht.
  - `deploy/acl.conf` gewaehrt aktuell nicht einmal dem Server-User
    `smartheat/#`-Rechte -- die produktive ACL-Konfiguration fuer das
    generic-Schema existiert moeglicherweise nur live auf dem Server, nicht
    in diesem Repo-Stand. Fuer diese Iteration irrelevant, da das
    Provisioning die Broker-ACL ohnehin nicht selbst schreibt (siehe
    Nicht-Ziele).
- `heizungsbruecke` hat aktuell **keinen** Web-Server, nur eine
  Poll-Loop (`__main__.main()`) plus MQTT-Client auf einem Hintergrundthread.
- `heizungsbruecke/profiles.py` hat aktuell nur ein flaches `profile_id`
  (z.B. `"vaillant_gastherme_heizkoerper"`); nur Eintraege mit sowohl
  `LOCAL_CLAMP_DEFAULTS` als auch `LOCAL_BOOST_DEFAULTS` sind "verifiziert".

## Architektur

Zwei neue Komponenten in zwei Repos:

```
Browser (Ingress-iframe)
   |
   v
heizungsbruecke: web.py (Flask, Ingress-Port)
   | (a) /api/login, /api/tenants  -->  heizungsserver: accounts_api.py (Flask)
   | (b) /api/profiles             -->  profiles.py (lokal, kein Netzwerk)
   | (c) /api/entities             -->  ha_api.py (HA Core API via Supervisor-Proxy)
   | (d) /api/complete             -->  supervisor_api.py (Optionen schreiben)
   |                                -->  heizungsserver: accounts_api.py /provision
```

### heizungsserver: neues `accounts`-Modul

- `accounts.py` (Storage, gleiches Muster wie `state.py`: reine Funktionen +
  `sqlite3`, eigene `init_db`):
  ```python
  def init_db(path: str) -> sqlite3.Connection: ...
  def create_account(conn, email: str, password_hash: str) -> str: ...  # account_id
  def get_account_by_email(conn, email: str) -> Account | None: ...
  def link_tenant(conn, account_id: str, tenant_id: str, profile_id: str) -> None: ...
  def tenants_for_account(conn, account_id: str) -> list[TenantLink]: ...
  def create_session(conn, account_id: str) -> str: ...  # token
  def account_id_for_token(conn, token: str) -> str | None: ...
  ```
  Passwort-Hashing ueber `werkzeug.security.generate_password_hash`/
  `check_password_hash` (kommt mit Flask, kein Klartext-Speichern -- auch ein
  Dummy-Service speichert keine Klartextpasswoerter).
- `smartheat_acl.py` (neu, ersetzt fuer diesen Zweck
  `onboarding.format_acl_snippet`):
  ```python
  def format_smartheat_acl_snippet(username: str, tenant_id: str) -> str: ...
  # "topic write smartheat/<tenant_id>/up/#\ntopic read smartheat/<tenant_id>/down/#\n"
  ```
- `accounts_api.py` (Flask):
  - `POST /auth/login` `{email, password}` -> `200 {token}` | `401`
  - `GET /accounts/me/tenants` (Header `Authorization: Bearer <token>`) ->
    `200 [{tenant_id, profile_id}]` | `401`
  - `POST /tenants/<tenant_id>/provision` (Header Bearer-Token, Body
    `{profile_id}`) -> `200 {username, password, mosquitto_passwd_command,
    acl_snippet}` | `403` (Tenant gehoert nicht zu diesem Account) | `404`
    (unbekannter Tenant). Nutzt `onboarding.generate_installation()` +
    `format_smartheat_acl_snippet()`. Schreibt den neuen Tenant-Eintrag in
    `tenants.json` (Best-Effort-Append; Daemon-Reload/Restart bleibt
    manueller Schritt, siehe Nicht-Ziele), mutiert aber **nicht** den
    laufenden Broker.

### heizungsbruecke: neues Ingress-Panel

- `config.yaml`: `ingress: true`, `panel_icon`, `panel_title`,
  `hassio_api: true` (fuer die Supervisor-Options-API) hinzu; `options`/
  `schema` fuer `tenant_id`/`profile`/`entity_*`/`notify_service`/
  `poll_interval_seconds`/`failsafe_stale_after_hours` entfernt (0.6.0,
  Breaking Change).
- `ha_api.py`: neue Methode `list_states() -> list[dict]` (schlichtes
  `GET /core/api/states`, liefert die volle Liste; Domain-/Einheiten-Filterung
  passiert in `web.py`, nicht hier -- `ha_api.py` bleibt ein duenner
  HA-Core-API-Client).
- `supervisor_api.py` (neu, duenner Client fuer die Supervisor-eigene
  Management-API, **nicht** die HA-Core-API -- anderer Pfad, kein
  `/core`-Praefix):
  ```python
  def set_own_options(base_url: str, token: str, options: dict) -> None: ...
  # POST {base_url}/addons/self/options  {"options": options}
  ```
- `web.py` (Flask, mit Flask-`session`-Cookie fuer den heizungsserver-Token --
  der Browser haelt nur das Ingress-Session-Cookie, nicht den Token direkt):
  - `GET /` -> statische Wizard-Seite (`static/wizard.html` + `wizard.js`,
    Vanilla JS, kein Build-Schritt)
  - `POST /api/login` `{email, password}` -> proxy zu heizungsserver
    `/auth/login`; Token landet im Flask-Session-Cookie
  - `GET /api/tenants` -> proxy zu heizungsserver
    `/accounts/me/tenants` (Token aus Session)
  - `GET /api/profiles` -> Hersteller/Typ/Verteilsystem-Katalog aus
    `profiles.py` (neue Registry-Erweiterung, siehe unten), inkl.
    `verified`-Flag pro Kombination
  - `GET /api/entities?domain=sensor|climate|number` -> gefilterte
    `ha_api.list_states()`-Ergebnisse (id, friendly_name,
    unit_of_measurement)
  - `POST /api/complete` `{tenant_id, profile_id, entities: {...},
    notify_service?, poll_interval_seconds?, failsafe_stale_after_hours?}` ->
    Server-seitige Revalidierung (Einheiten, Profil-Kombination, Tenant
    gehoert zum eingeloggten Account) -> ruft heizungsserver `/provision` ->
    schreibt Optionen via `supervisor_api.set_own_options()` -> `200 {ok:
    true}` mit Hinweistext "bitte Add-on manuell neu starten" oder
    `4xx {error: "..."}`
- `__main__.py`: bestehende Poll-Loop (`main()`-Koerper) wandert in einen
  Hintergrund-`threading.Thread`; Flask (`app.run(...)`) laeuft im
  Haupt-Thread, da Ingress sofort nach Add-on-Start reagieren muss. Flasks
  eingebauter Dev-Server reicht fuer diesen Zweck (Ingress-Traffic ist
  Single-User, geringes Volumen) -- kein WSGI-Produktionsserver noetig.

### Profil-Katalog (Erweiterung von `profiles.py`)

```python
@dataclass(frozen=True)
class ProfileCatalogEntry:
    hersteller: str
    erzeuger_typ: str
    verteilsystem: str
    profile_id: str

PROFILE_CATALOG: tuple[ProfileCatalogEntry, ...] = (
    ProfileCatalogEntry("Vaillant", "Gastherme", "Heizkoerper", "vaillant_gastherme_heizkoerper"),
    ProfileCatalogEntry("Weishaupt", "Waermepumpe", "Fussbodenheizung", "weishaupt_waermepumpe_fussbodenheizung"),
)

def is_verified(profile_id: str) -> bool:
    return profile_id in LOCAL_CLAMP_DEFAULTS and profile_id in LOCAL_BOOST_DEFAULTS
```
`/api/profiles` liefert `PROFILE_CATALOG` plus `is_verified()`-Flag pro
Eintrag; das Frontend baut die drei gekoppelten Dropdowns (Hersteller filtert
Typ, Typ filtert Verteilsystem) und blockt "Weiter", wenn die resultierende
Kombination nicht `verified` ist.

### Sensor-Rollen und Einheiten-Erwartung

| Rolle | erlaubte Domains | erwartete Einheit | Attribut-Auswahl bei `climate.*` |
|---|---|---|---|
| `entity_room_actual` | `sensor`, `climate` | `°C` | `current_temperature` |
| `entity_room_target` | `sensor`, `climate` | `°C` | `temperature` |
| `entity_outdoor_temp` | `sensor` | `°C` | - |
| `entity_curve_current` | `number` | keine (nur Domain-Pruefung `number`, kein `unit_of_measurement`-Check) | - |
| `entity_offset_current` | `number` | `°C` | - |
| `entity_heat_limit` | `number`, `sensor` | `°C` | - |

Bei Auswahl eines `climate.*`-Entities fuer `entity_room_actual`/
`entity_room_target` erscheint ein zweites Dropdown fuer das Attribut
(`current_temperature`/`temperature`), passend zur bestehenden
`entity_id::attribute`-Konvention in `ha_api.get_state()`.

## Fehlerbehandlung

Jeder Schritt scheitert laut und auf Deutsch, nie stillschweigend:
Login-Fehlschlag, heizungsserver nicht erreichbar (Tunnel/Netzwerk),
leere Entity-Liste fuer eine Domain, Einheiten-Mismatch bei Bestaetigung,
Supervisor-Options-Schreibfehler und Provisioning-Fehlschlag zeigen jeweils
eine spezifische Inline-Meldung und blockieren den Fortschritt, statt auf
einen Default zurueckzufallen oder teilweise zu speichern.

## Teststrategie

- heizungsserver: Unit-Tests fuer `accounts.py` (sqlite-Fixture, Rundreise
  create/get/link/session), `smartheat_acl.py` (Format-Check), und
  `accounts_api.py` (Flask-Test-Client: Login-Erfolg/-Fehlschlag,
  Tenant-Liste, Provisioning inkl. 403/404-Faelle).
- heizungsbruecke: Unit-Tests fuer `web.py`-Routen (Flask-Test-Client,
  gemockte `ha_api`/heizungsserver-Aufrufe), fuer die
  Profil-Katalog-Filterung, und fuer `supervisor_api.set_own_options()`
  (gemockte `requests.post`, analog zum bestehenden `ha_api.py`-Teststil).
- Kein Browser-/E2E-Test in dieser Iteration (siehe Nicht-Ziele).

## Config & Versionierung

- `heizungsbruecke`: 0.5.0 -> **0.6.0** (Breaking Change). DOCS.md-Eintrag im
  bestehenden "Update von X auf Y"-Format: entfallene Felder, neue
  Ingress-Voraussetzung, Hinweis "Konfiguration jetzt ausschliesslich ueber
  den Wizard (Add-on-Panel oeffnen)".
  Neue Python-Abhaengigkeit: `flask`.
- `heizungsserver`: additive Aenderung (keine Versionsnummer im gleichen Sinn
  wie das Add-on, aber neue Abhaengigkeit `flask` + `werkzeug` (kommt mit
  Flask)).

## Offene Punkte / bewusst verschoben

- **Netzwerkweg heizungsserver <-> heizungsbruecke fuer die neue HTTP-API**:
  aktuell existiert nur ein Cloudflare-Access-Tunnel fuer MQTT
  (`cloudflared_access_mqtt`). Ob die neue HTTP-API einen eigenen
  Tunnel-Hostname braucht oder ueber einen bestehenden Weg laeuft, ist beim
  Live-Test (Schritt C im Multi-Tenant-Rollout-Runbook) zu klaeren, nicht
  Teil dieses Specs.
- **Mosquitto-Broker-ACL fuer das `smartheat/`-Schema**: der aktuelle
  `deploy/acl.conf` im Repo gewaehrt das nicht; ob die live laufende
  Broker-Config das bereits hat, ist unklar (Regel 7: im Zweifel fragen,
  nicht raten) -- betrifft aber nur die manuellen Befehle, die das
  Provisioning zurueckgibt, nicht diesen Code selbst.
