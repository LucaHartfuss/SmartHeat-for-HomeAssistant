# SmartHeat-for-HomeAssistant — Repo-Kontext

HA-Add-on-Repository mit zwei Add-ons: `heizungsbruecke` (Client-seitige Bridge-Logik: Snapshot-Publish, Boost, Fail-Safe, lokale Sicherheits-Clamps) und `cloudflared_access_mqtt` (TCP-Tunnel-Forwarder zum Server). Beide laufen auf jedem Kunden-Pi, inkl. `client1`. Volle Beschreibung: `../docs/architecture.md`, Abschnitt 4. Sicherheitsregeln aus `../CLAUDE.md` gelten unverändert — Änderungen hier wirken sich real auf laufende Kundenanlagen aus, sobald deployed.

## Struktur

- `heizungsbruecke/src/heizungsbruecke/`:
  - `__main__.py` — Boot (`_start_bridge`, `_prime`), dünne Handler des `RegulationWorker` (`_on_*`), `_run_bridge` (Exit-Code: 1 nur bei Konfigurationsfehlern), `main`.
  - `runtime.py` — `Runtime` (Laufzeit-Kontext) und die Ereignisarten `EV_*`.
  - `config.py` — Pflichtfelder, aufgelöste Sicherheitswerte/Fenster/Basis-URL (`resolve_effective_options`, `ConfigError`), Startprüfungen, Dateipfade, MQTT `127.0.0.1:18830`.
  - `state.py` — `BridgeState` + `StateStore`: der gesamte Zustand, `backup.json`/`failsafe_state.json` einmal geladen und nur bei Änderung geschrieben.
  - `override.py` — einzige Stelle, die Kurve/Offset auf die Anlage schreibt: Sollwert-Regel Notfall-Boost > Comfort-Boost > Wiederherstellungspunkt, Schreiben nur beim Wechsel, immer geclampt.
  - `regulation.py` — lokaler Check (Comfort-Boost nur bei Sollwerterhöhung, Notfall-Boost nur im Notbetrieb, Stable-Target-Cache mit 10-s-Entprellung) und „Tick fällig?“.
  - `ticks.py` — führt die Aktionen von `delivery.py` aus, verarbeitet Server-Antworten.
  - `delivery.py` — reine Zustandsmaschine der Tick-Zustellung (Phasen, Retry mit derselben `seq`, Notbetrieb nach 2 Ack-Timeouts, Datenfehler lokal/Server/Anlage).
  - `abo.py` — Abo-inaktiv-Modus und Fristende.
  - `triggers.py` — HA-Trigger-Client (WebSocket) und MQTT-Client-Aufbau; Callbacks stellen nur in den Worker ein.
  - `worker.py` — Regel-Worker (Event-Queue + Zeitplan), alle Regelungsereignisse nacheinander im Hauptthread.
  - `notifier.py` — einziger Weg für Meldungen an den Kunden (`Notifier.notify(key, state, message, *, critical)`): Push an alle `notify_services`, zusätzlich `persistent_notification` bei kritischen Anlässen, Zustandsentprellung je Schlüssel (Normalzustand `"ok"`, gemeldet wird nur beim Wechsel), übersteht Neustarts (`backup.json`); `seed()` übernimmt einen in `failsafe_state.json` persistierten Notbetrieb-Zustand beim Start, ohne selbst zu melden.
  - `status.py` — Status-Entity `sensor.smartheat_<tenant>_status` (`startet`/`bereit`/`konfigurationsfehler` mit Attribut `grund`), per `POST /api/states` gesetzt; `republish()` setzt den letzten Zustand bei jedem (Wieder-)Verbinden des WS-Trigger-Clients erneut, weil die Entity keinen HA-Neustart übersteht.
  - `battery.py`/`room_sensors.py` — Überwachung der `battery_entities`/`room_sensors` über einen eigenen `EV_HEALTH`-Takt, nicht kritisch. Eine gelöschte Batterie-Entity (HTTP 404) wird übersprungen; ein gelöschter/ausgefallener Raumfühler (HTTP 404 oder unplausibler Wert) zählt als ausgefallen. Einzelfühler-Meldungen nur ab 2 konfigurierten Raumfühlern (bei einem Fühler ist dessen Ausfall der lokale Datenfehler auf `room_actual`).
  - `plausibility.py` — Wertebereiche Raum-/Außentemperatur (`ROOM_TEMP_RANGE`/`OUTDOOR_TEMP_RANGE`), geteilt mit `helper_templates.py`; muss zu `messages.py::PLAUSIBLE_RANGES` auf dem Server passen (Contract-Check).
  - `helper_templates.py` — Jinja-Texte der Template-Hilfssensoren (Raummittel über alle gültigen Raumfühler, Außentemperatur aus `weather.*`).
  - `derived_sensors.py` — Template-Sensoren für Raum-/Außentemperatur, Quellwechsel (legt/löscht/erkennt Neuanlage bei geändertem `room_sensors`/`entity_outdoor_temp`).
  - `snapshot.py`, `telemetry.py`, `boost.py`, `emergency_boost.py`, `safety.py` (lokale Sicherheitswerte je Verteilsystem, nur hier), `windows.py` (Fenster aus den Optionen), `mqtt_client.py`, `manifest.py`, `ha_api.py`, `ha_trigger_client.py`, `entitlement.py`, `daynight_snapshot.py`, `target_history.py`, `clamping.py`, `backup_store.py`.
- `heizungsbruecke/config.yaml` — hat einen echten `schema:`-Block, wird aber **ausschließlich** von der SmartHeat-Integration befüllt, nie manuell in der Add-on-UI.
- `cloudflared_access_mqtt/` — nur `run.sh`-Wrapper um `cloudflared access tcp`, keine eigene Logik.

## Tests

```
cd heizungsbruecke
pip install -e ".[dev]"
pytest
```
(`pyproject.toml`: `testpaths = ["tests"]`, `pythonpath = ["src"]`.) 746 Testfunktionen in 37 Dateien (`pytest -q --collect-only`). Zusätzlich Shell-Integrationstests im Repo-Root unter `tests/` (`run_all.sh`, Docker-Build/Happy-Path).

## Besonderheiten

- MQTT-Lokalport `18830` ist in `heizungsbruecke` hart codiert — muss zum `local_port`-Default von `cloudflared_access_mqtt` passen (Cross-Repo-Invariante, siehe `../docs/architecture.md` §9).
- Lokale Sicherheitswerte (`safety.py`) gibt es nur im Add-on. Ihre Schlüssel (Verteilsysteme) spiegelt der Server; Fenster und Basis-URL kommen per Optionen von Server bzw. Integration. `python3 ../tools/contract_check.py` prüft alle Cross-Repo-Duplikate — vor jedem Release grün.
- Versionierung/Changelog lebt in `DOCS.md` je Add-on (aktuell `heizungsbruecke` v0.19.0, `cloudflared_access_mqtt` v1.0.0), kein separates `CHANGELOG.md`.
- Startfehler (`__main__.py::StartupError`/`_fail_start`) melden über einen stabilen `notifier`-Schlüssel `fehler:<key>` (z. B. `hilfs_entities`, `entity_fehlt:<sortierte IDs>`) — der ausführliche Grund steht nur im Status-Entity-Attribut `grund`, in der Meldung und im Log, nicht in der Meldeidentität, sonst würde ein Neustart mit demselben Fehler jedes Mal erneut melden. Ein im Fehlertext zitierter `mqtt_password`-Wert wird überall (Text, `grund`, Meldung, Retry-Log) durch `***` ersetzt (`_without_credentials`).
- `bereit` (Status-Entity) setzt der erste MQTT-Connect (`EV_MQTT_CONNECTED`); im Abo-inaktiv-Modus (kein MQTT-Client) setzt es der Start direkt nach dem Hochfahren.
- Ein uncommitteter Worktree/Branch zu einem Ingress-Wizard (`docs/superpowers/{plans,specs}/2026-09-14-heizungsbruecke-ingress-wizard-*.md`) existierte zuletzt als Entwurf, nicht gemerged — vor Arbeit an `web.py`/Ingress-UI prüfen, ob das noch aktuell ist.
