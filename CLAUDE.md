# SmartHeat-for-HomeAssistant — Repo-Kontext

HA-Add-on-Repository mit zwei Add-ons: `heizungsbruecke` (Client-seitige Bridge-Logik: Snapshot-Publish, Boost, Notbetrieb bei ausbleibender Server-Antwort, lokale Sicherheits-Clamps) und `cloudflared_access_mqtt` (TCP-Tunnel-Forwarder zum Server). Beide laufen auf jedem Kunden-Pi, inkl. `client1`. Volle Beschreibung: `../docs/architecture.md`, Abschnitte 4 und 5. Sicherheitsregeln aus `../CLAUDE.md` gelten unverändert — Änderungen hier wirken sich real auf laufende Kundenanlagen aus, sobald deployed.

## Struktur (Details: `../docs/architecture.md` §4–§5)

- `heizungsbruecke/src/smartheat_core/` — HA-freier **Client-Kern** (nur Standardbibliothek, geprüft von
  `tests/test_core_purity.py`; Anlage, Speicher, Meldungen und Uhr über Schnittstellen): `levers.py` (Hebel und
  Hebelsätze), `safety.py` (lokale Sicherheitswerte je Hebelsatz × Verteilsystem, Regel 4), `binding.py`
  (Beschreibung je Hersteller-Anbindung), `pipeline.py` (einzige Stelle, die Hebel schreibt), `enforce.py`
  (Durchsetzen statt Melden), `derived.py` (Mindestvorlauf), `boost.py`/`emergency_boost.py`, `write_budget.py`,
  `clamping.py`, `energy.py`, `wallclock.py` (prozessweite Wanduhr, `now()`/`today()`).
- `heizungsbruecke/src/smartheat_runtime/` — **hostneutraler Betrieb** (SHG G1; nur Standardbibliothek,
  `smartheat_core`, `smartheat_transport`, `requests` nur in `entitlement.py`; nie `heizungsbruecke`, `websocket`, `paho`
  nur über `smartheat_transport.mqtt_client`; keine Tenant-IDs; Grenzen geprüft von `tests/test_runtime_purity.py`): `app.py` (`start(host, clock)`,
  Handler, Ruhezustand, Abmelden, `StartFailure`/`_fail_start`), `ports.py` (Schnittstellen zum Host),
  `runtime_config.py` (`BootInfo`/`RuntimeConfig`), `runtime.py`/`worker.py` (Ereignisse nacheinander im
  Hauptthread), `delivery.py`/`ticks.py`/`snapshot.py` (Zustellung, Notbetrieb, Datenfehler), `telemetry.py`,
  `regulation.py`, `abo.py`/`entitlement.py`, `status.py` (Status-Modell `schema` 2) und `notifier.py`,
  `state.py`/`backup_store.py`, `waerme.py`/`waerme_hint.py`, `battery.py`/`room_sensors.py`/`datentraeger.py`,
  `roles.py`, `mqtt_link.py`, `plausibility.py`, `windows.py`.
- `heizungsbruecke/src/smartheat_transport/` — Transport-Deskriptor und Zugangsdaten, MQTT-Verbindung
  (`mqtt_client.py` als einzige Stelle mit `paho`, geprüft von `tests/test_transport_purity.py`).
- `heizungsbruecke/src/heizungsbruecke/` — **HA-Host** (`smartheat_runtime` über die Ports an Home Assistant
  angeschlossen): `__main__.py` (dünner Einstieg, `_start_bridge`), `host.py` (`HaHost`, `StartupError`, Warten auf
  HA, Prüfung der Hilfs-Entities), `config.py` (Optionen), `ha_api.py` (REST/WebSocket), `ha_signals.py`
  (`HaSignalSource`), `ha_sinks.py` (`HaStatusSink` mit Ereignis `smartheat_status`, `HaNotifySink`),
  `ha_trigger_client.py`/`triggers.py` (Auslöser aus HA), `ha_binding.py` (mypyllant, Weishaupt, Viessmann),
  `derived_sensors.py`/`helper_templates.py`, `manifest.py`, `version.py` (`ADDON_VERSION`).
- `heizungsbruecke/config.yaml` — hat einen echten `schema:`-Block, wird aber **ausschließlich** von der
  SmartHeat-Integration befüllt, nie manuell in der Add-on-UI.
- `cloudflared_access_mqtt/` — nur `run.sh`-Wrapper um `cloudflared access tcp`, keine eigene Logik.

## Prüfen und Branches

```
scripts/check.sh          # lint, test, contract (--only <schritt> für einzelne Schritte)
scripts/check.sh --full   # zusätzlich die Docker-Schritte (Build, Happy-Path, run.sh, Will-ACL)
```
Direkter Aufruf bleibt möglich: `cd heizungsbruecke && pip install -e ".[dev]" && pytest`
(`pyproject.toml`: `testpaths = ["tests"]`, `pythonpath = ["src"]`). Testzahl: siehe CI (Job
`test`). Shell-Integrationstests liegen im Repo-Root unter `tests/` (`run_all.sh`).

Feature-Branches (`feat/…`/`fix/…`) zweigen von `develop` ab und werden `--no-ff` nach `develop`
gemergt — nie direkt nach `main`. `main` bewegt sich nur per Release-Tag
(`heizungsbruecke-vX.Y.Z`, `cloudflared_access_mqtt-vX.Y.Z`; `-dryrun`-Suffix = Probelauf) —
ein Push nach `main` ist ein Release an alle Kunden-Pis, deren Supervisor den Default-Branch
verfolgt. Release-Ablauf, CI-Jobs, Token: `../docs/ci-cd-runbook.md`.

## Besonderheiten

- MQTT-Adresse und -Port kommen aus dem Transport-Deskriptor (Option `transport`, vom Server geliefert); die Integration schreibt denselben Port als `local_port` in `cloudflared_access_mqtt` (Cross-Repo-Invariante, Contract-Check 6, siehe `../docs/architecture.md` §9).
- Lokale Sicherheitswerte (`smartheat_core/safety.py`, je Hebelsatz × Verteilsystem) gibt es nur im Add-on. Ihre Schlüssel (Verteilsysteme) spiegelt der Server; Tagestick-Uhrzeit (`daily_trigger_time`) und Basis-URL kommen per Optionen von Server bzw. Integration. `python3 ../tools/contract_check.py` prüft alle Cross-Repo-Duplikate — vor jedem Release grün.
- Versionsstand in `<addon>/config.yaml` (bei `heizungsbruecke` zusätzlich `ADDON_VERSION` in `src/heizungsbruecke/version.py`, Test prüft den Gleichlauf); seit der CI/CD-Umstellung (2026-09-28) hat jedes Add-on zusätzlich ein `CHANGELOG.md` (Pflichtabschnitt `## X.Y.Z` je Release, geprüft vom Release-Workflow, im Update-Dialog des Supervisors sichtbar) neben dem bisherigen `DOCS.md`.
- Lock-Datei erneuern (`heizungsbruecke/requirements.txt`, TP12e/AU-018): Das Image installiert nur aus dieser Datei (`pip install --require-hashes`, Hashes für alle Plattformen), `src/` liegt per `PYTHONPATH` auf dem Suchpfad, das Paket selbst wird nicht installiert. Basis-Images sind per Index-Digest gepinnt. Erneuert wird die Datei nur bei geänderten `dependencies` in `heizungsbruecke/pyproject.toml` oder bewusstem Bump, im Ordner `heizungsbruecke/` in einem Wegwerf-Container (die alte Datei wird als Ausgangsdatei mitgegeben, damit bestehende Pins bleiben; für einen Bump `--upgrade` vor `--generate-hashes` ergänzen und das Diff lesen):

  ```
  tar cf - pyproject.toml requirements.txt | docker run --rm -i \
    python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f \
    sh -c "mkdir /w && cd /w && tar xf - && pip install --quiet pip-tools >/dev/null 2>&1 \
           && pip-compile --quiet --generate-hashes --strip-extras --output-file requirements.txt pyproject.toml \
           && tar cf - requirements.txt" | tar xf -
  ```

  Danach `scripts/check.sh --only docker --full` (u. a. `tests/test_heizungsbruecke_reproducible.sh`); den Drift-Check gegen `pyproject.toml` übernimmt `scripts/ci/pin_check.py`.
- Startfehler (`smartheat_runtime/app.py::StartFailure`/`_fail_start`, HA-Teil `heizungsbruecke/host.py::StartupError`) melden über einen stabilen `notifier`-Schlüssel `fehler:<key>` (z. B. `hilfs_entities`, `entity_fehlt:<sortierte IDs>`) — der ausführliche Grund steht nur im Feld `grund` des Status-Events, in der Meldung und im Log, nicht in der Meldeidentität, sonst würde ein Neustart mit demselben Fehler jedes Mal erneut melden. Ein im Fehlertext zitiertes Geheimnis (`config.SECRET_OPTIONS`: `mqtt_password`, `tls_private_key`, `installation_token`) wird überall (Text, `grund`, Meldung, Retry-Log) durch `***` ersetzt (`host._without_credentials`).
- Golden-Master `heizungsbruecke/tests/test_golden_master.py` (Aufzeichnung `tests/golden/addon_scenario.json`) sichert zusammen mit den unveränderten Unit-Tests, dass das Add-on nach außen unverändert handelt (HA-Schreibaufrufe, Meldungen, Ereignisse, MQTT, Dateien im Datenverzeichnis). Die Aufzeichnung wird nie neu erzeugt, ohne dass ein Plan es verlangt: ändert sich der Golden-Master, ist der Code falsch, nicht die Datei.
- Startbereitschaft: HA gilt erst als erreichbar, wenn `GET /api/config` `state == "RUNNING"` meldet (`ha_api.is_reachable`); vorher wartet der Start unbegrenzt, das ~4-min-Budget für fehlende Entities/Hilfs-Entities zählt nur bei laufendem HA (Cloud-Integrationen wie `mypyllant` laden erst nach dem HTTP-Server).
- Der Status `regelt` gilt ab dem ersten MQTT-Connect (`EV_MQTT_CONNECTED`); im Abo-inaktiv-Modus direkt nach dem Hochfahren. Endzustände sind ein Ruhezustand, weil der Supervisor-Watchdog auch einen Exit 0 neu startet.
- Kein Last Will und nichts unter `smartheat/<tenant>/status/` (B4): der Server erwartet dort nichts mehr. Ein Connect mit einem von der ACL verbotenen Will-Topic nimmt Mosquitto 2 (verifiziert 2.0.11/2.1.2) trotzdem an, nur ein direktes Publish dorthin lehnt sie ab (`tests/test_mosquitto_will_acl.sh`) — kein Verbindungsschutz, also kein Grund, `refresh-acl` auf ältere Add-ons zu warten.
