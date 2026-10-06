# SmartHeat-for-HomeAssistant — Repo-Kontext

HA-Add-on-Repository mit zwei Add-ons: `heizungsbruecke` (Client-seitige Bridge-Logik: Snapshot-Publish, Boost, Notbetrieb bei ausbleibender Server-Antwort, lokale Sicherheits-Clamps) und `cloudflared_access_mqtt` (TCP-Tunnel-Forwarder zum Server). Beide laufen auf jedem Kunden-Pi, inkl. `client1`. Dazu `gateway/`: die Software des SmartHeat-Gateways (SHG, Clienttyp ohne Home Assistant), **kein** Add-on (siehe Besonderheiten). Volle Beschreibung: `../docs/architecture.md`, Abschnitte 4 und 5. Sicherheitsregeln aus `../CLAUDE.md` gelten unverändert — Änderungen hier wirken sich real auf laufende Kundenanlagen aus, sobald deployed.

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
  `roles.py`, `mqtt_link.py`, `plausibility.py`, `windows.py`. Seit SHG G2a zusätzlich: `texts.py`
  (`HostTexts`, Kunden- und Log-Texte, die den Host nennen; Standard = Texte des HA-Add-ons), `debounce.py`
  (`Debouncer`, 10-s-Entprellung des Raum-Solls), `room_mean.py` (Raummittel), `options.py` (hostneutrale Prüfung der
  Laufzeit-Optionen, gemeinsam für Add-on und Gateway).
- `heizungsbruecke/src/smartheat_transport/` — Transport-Deskriptor und Zugangsdaten, MQTT-Verbindung
  (`mqtt_client.py` als einzige Stelle mit `paho`, geprüft von `tests/test_transport_purity.py`).
- `heizungsbruecke/src/heizungsbruecke/` — **HA-Host** (`smartheat_runtime` über die Ports an Home Assistant
  angeschlossen): `__main__.py` (dünner Einstieg, `_start_bridge`), `host.py` (`HaHost`, `StartupError`, Warten auf
  HA, Prüfung der Hilfs-Entities), `config.py` (Optionen), `ha_api.py` (REST/WebSocket), `ha_signals.py`
  (`HaSignalSource`), `ha_sinks.py` (`HaStatusSink` mit Ereignis `smartheat_status`, `HaNotifySink`),
  `ha_trigger_client.py`/`triggers.py` (Auslöser aus HA), `ha_binding.py` (mypyllant, Weishaupt, Viessmann),
  `derived_sensors.py`/`helper_templates.py`, `manifest.py`, `version.py` (`ADDON_VERSION`).
- `gateway/` — **SmartHeat-Gateway ohne Home Assistant** (SHG G2a; Image, Compose, Agent und Laufzeit; Paket
  `src/smartheat_gateway/`, importiert `smartheat_core`/`smartheat_runtime`/`smartheat_transport`, nie `heizungsbruecke`;
  geprüft von `gateway/tests/test_boundaries.py`). Laufzeit-Seite (der `GatewayHost` an `smartheat_runtime`):
  `host.py`, `config.py`, `signals.py` (Referenzen `zigbee:`/`soll:`/`treiber:`/`raum:`), `triggers.py`,
  `target_store.py`, `zigbee.py`, `bus.py`, `sinks.py`, `raum.py`, `quota.py`, `drivers/` (Treiber-Protokoll,
  Registry, `simulation.py`), `runtime_main.py`; Tunnel: `tunnel.py`. Agent: `agent/` (Identität, signierter Client der
  Geräte-API, Befehle, Lebenszyklus, Schleife, Diagnoseseite), `agent/wire.py` ist der Vertrag mit dem Server
  (↔ `../tools/contracts/shg_device_v1.json`, Contract-Check 44). Compose: `gateway/compose/` (`docker-compose.yml`,
  Dev-Overlay, `mosquitto.conf`; Dienst `init` aus `init.py` schreibt Bus-Zugangsdaten, ACL und Zigbee2MQTT-Grundkonfiguration). Tests: `gateway/tests/` inkl. Fake-Zigbee2MQTT (`fake_z2m.py`) und Fake-Geräte-API
  (`fake_device_api.py`). Aufbau und Abläufe: `../docs/architecture.md`, Abschnitt „SmartHeat-Gateway“.
- `gateway/host/` — **Host-Dienste und Installer des Gateways** (SHG G2b-1; läuft auf dem **System-Python** des Pi
  (Debian 13 trixie, Python 3.13), nicht im Container): Paket `smartheat_host/` mit `updater.py` (Soll-Version, Manifest- und
  minisign-Prüfung, Umschalten, Gesundheit, Rückweg), `bundles.py`, `device_api.py`, `minisign.py`, `led.py`,
  `hoststatus.py`; dazu `install.sh`, `systemd/`, `udev/`, `nftables.conf`, `apt/`, `journald.conf.d/` und `release.pub`
  (bis zum echten Schlüssel ein Platzhalter). **Grenze** (geprüft von `gateway/host/tests/test_boundaries.py`): nur
  Standardbibliothek, `cryptography`, `smartheat_host` und genau `smartheat_gateway.{files,paths,version}` sowie
  `smartheat_gateway.agent.{wire,identity}` (`install.sh` legt diese Module mit ab); keine Tenant-IDs; nie
  `heizungsbruecke`. Tests: `cd gateway/host && pytest` (`install_checks.sh` läuft im Docker-Test des Installers).
- `gateway/release/` — Bundle-Bau (`build_bundle.py`: Compose mit Image-Digests und Manifest; nutzt dieselben
  Prüffunktionen wie der Updater). Tests: `cd gateway/release && pytest`. Der Release-Workflow
  `.github/workflows/release-gateway.yml` (Tag `gateway-vX.Y.Z`) ruft das Skript auf (`sign_bundle.sh`: Bundle prüfen, mit minisign signieren, mit dem Gerätecode verifizieren; Secrets nur im Job `sign`); Ablauf und Schlüssel: `../docs/ci-cd-runbook.md`,
  Abschnitt „Gateway-Release“.
- `heizungsbruecke/config.yaml` — hat einen echten `schema:`-Block, wird aber **ausschließlich** von der
  SmartHeat-Integration befüllt, nie manuell in der Add-on-UI.
- `cloudflared_access_mqtt/` — nur `run.sh`-Wrapper um `cloudflared access tcp`, keine eigene Logik.

## Prüfen und Branches

```
scripts/check.sh          # lint, test, contract (--only <schritt> für einzelne Schritte)
scripts/check.sh --full   # zusätzlich die Docker-Schritte (Build, Happy-Path, run.sh, Will-ACL, Gateway-Image und Compose-Lauf)
```
`scripts/check.sh` prüft und testet `heizungsbruecke/`, `gateway/`, `gateway/host/` **und** `gateway/release/`; `--full` baut zusätzlich das Gateway-Image
(lokal nur amd64) und fährt den Compose-Lauf mit dem Dev-Overlay (`tests/test_gateway_docker_build.sh`; dabei Bus-Anmeldung und ACL, Masken, Netz-Wache; die statische
Prüfung der Compose-Datei steckt in `gateway/tests/test_compose.py`). Zwei weitere Docker-Skripte gehören zum Gateway: `tests/test_gateway_install.sh` (Host-Installer zweimal in `debian:trixie`, Plattform per `SHG_INSTALL_PLATFORM`, die CI fährt zusätzlich arm64 im Job `install-arm64`) und `tests/test_gateway_updater.sh` (Updater gegen eine lokale Registry mit drei signierten Test-Bundles, Rückweg und Manipulation; Laufzeit rund 6 min, steuert Compose über den Docker-Socket des Hosts). Im Dev-Root fährt `scripts/check.sh --only e2e --full`
zusätzlich die Gateway-Modi des Ende-zu-Ende-Tests.
Direkter Aufruf bleibt möglich: `cd heizungsbruecke && pip install -e ".[dev]" && pytest` bzw.
`cd gateway && pip install -e ".[dev]" && pytest` (`pyproject.toml`: `testpaths = ["tests"]`, `pythonpath = ["src"]`). Testzahl: siehe CI (Job
`test`). Shell-Integrationstests liegen im Repo-Root unter `tests/` (`run_all.sh`).

Feature-Branches (`feat/…`/`fix/…`) zweigen von `develop` ab und werden `--no-ff` nach `develop`
gemergt — nie direkt nach `main`. `main` bewegt sich nur per Release-Tag
(`heizungsbruecke-vX.Y.Z`, `cloudflared_access_mqtt-vX.Y.Z`; `-dryrun`-Suffix = Probelauf) —
ein Push nach `main` ist ein Release an alle Kunden-Pis, deren Supervisor den Default-Branch
verfolgt. Release-Ablauf, CI-Jobs, Token: `../docs/ci-cd-runbook.md`.

## Besonderheiten

- **`gateway/` ist kein Add-on:** Der Supervisor sieht den Ordner nicht (keine `config.yaml`), er taucht nie im Add-on-Store auf. Das Image wird aus der Repo-Wurzel gebaut (`docker build -f gateway/Dockerfile .`, kopiert `smartheat_core`/`smartheat_transport`/`smartheat_runtime` aus `heizungsbruecke/src/`). Ein Release läuft über den eigenen Workflow `release-gateway.yml` (Tag-Muster `gateway-vX.Y.Z`, Version in `gateway/VERSION` und `gateway/CHANGELOG.md`, Image nach ghcr, signiertes Bundle); ohne echten Schlüssel in `gateway/host/release.pub` lehnt das Gate echte Tags ab, `-dryrun` geht. Die CI baut das Image zusätzlich für amd64 und arm64 (Job `build-gateway`). Änderungen in `smartheat_core`/`smartheat_runtime` wirken auf beide Clienttypen; der Golden-Master des Add-ons bleibt davon unberührt.
- **Gerätevertrag:** `gateway/src/smartheat_gateway/agent/wire.py` ist die Gerätehälfte des Vertrags mit dem Server (G3). Änderungen nur gleichzeitig in `wire.py`, `../tools/contracts/shg_device_v1.json` und (G3) `heizungsserver/devices_wire.py`; Contract-Check 44 vergleicht alle drei.
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
