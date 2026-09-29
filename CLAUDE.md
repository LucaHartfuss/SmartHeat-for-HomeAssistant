# SmartHeat-for-HomeAssistant — Repo-Kontext

HA-Add-on-Repository mit zwei Add-ons: `heizungsbruecke` (Client-seitige Bridge-Logik: Snapshot-Publish, Boost, Notbetrieb bei ausbleibender Server-Antwort, lokale Sicherheits-Clamps) und `cloudflared_access_mqtt` (TCP-Tunnel-Forwarder zum Server). Beide laufen auf jedem Kunden-Pi, inkl. `client1`. Volle Beschreibung: `../docs/architecture.md`, Abschnitt 4. Sicherheitsregeln aus `../CLAUDE.md` gelten unverändert — Änderungen hier wirken sich real auf laufende Kundenanlagen aus, sobald deployed.

## Struktur

- `heizungsbruecke/src/heizungsbruecke/`:
  - `__main__.py` — Boot (`_start_bridge`, `_prime`), dünne Handler des `RegulationWorker` (`_on_*`), Ruhezustand (`IdleBridge`/`Runtime.idle`, nie ein absichtlicher Exit), Abmelden (`_sign_off`), `main`.
  - `runtime.py` — `Runtime` (Laufzeit-Kontext) und die Ereignisarten `EV_*`.
  - `config.py` — Pflichtfelder, aufgelöste Sicherheitswerte/Fenster/Basis-URL (`resolve_effective_options`, `ConfigError`), Startprüfungen, Dateipfade, MQTT `127.0.0.1:18830`.
  - `state.py` — `BridgeState` + `StateStore`: der gesamte Zustand, `backup.json`/`failsafe_state.json` einmal geladen und nur bei Änderung geschrieben.
  - `override.py` — einzige Stelle, die Kurve/Offset auf die Anlage schreibt: Sollwert-Regel Notfall-Boost > Comfort-Boost > Wiederherstellungspunkt, Schreiben nur beim Wechsel, immer geclampt.
  - `regulation.py` — lokaler Check (Comfort-Boost nur bei Sollwerterhöhung, Notfall-Boost nur im Notbetrieb, Stable-Target-Cache mit 10-s-Entprellung) und „Tick fällig?“.
  - `ticks.py` — führt die Aktionen von `delivery.py` aus, verarbeitet Server-Antworten.
  - `delivery.py` — reine Zustandsmaschine der Tick-Zustellung (Phasen, Retry mit derselben `seq`, Datenfehler lokal/Server/Anlage). Fail-Safe = Ack-Timeout: bleibt die Antwort auf einen Snapshot 30 s aus (`ACK_TIMEOUT_SECONDS`), folgt der nächste Versuch nach der Server-Retry-Kette (0/5/15/60 min, danach stündlich); nach 2 Ack-Timeouts in Folge fragt das Add-on den Abo-Status ab und geht bei inaktivem Abo in den Abo-inaktiv-Modus, sonst in den Notbetrieb (Notfall-Boost auf die Clamp-Obergrenzen, sobald der Raum mehr als 1 K unter dem Sollwert liegt, `emergency_boost.py`). Jede passende Server-Antwort beendet den Notbetrieb; eine Ablehnung (`rejected`, auch ein unbekanntes `schema`) oder ein Schreibfehler zur Anlage (`WriteFailed`) zählt als Antwort mit Datenfehler, nie als Notbetrieb. Den zeitbasierten 26-h-Fail-Safe gibt es seit 0.12.0 nicht mehr.
  - `abo.py` — Abo-inaktiv-Modus und Fristende.
  - `triggers.py` — HA-Trigger-Client (WebSocket) und MQTT-Client-Aufbau; Callbacks stellen nur in den Worker ein.
  - `worker.py` — Regel-Worker (Event-Queue + Zeitplan), alle Regelungsereignisse nacheinander im Hauptthread.
  - `notifier.py` — einziger Weg für Meldungen an den Kunden (`Notifier.notify(key, state, message, *, critical)`): Push an alle `notify_services`, zusätzlich `persistent_notification` bei kritischen Anlässen, Zustandsentprellung je Schlüssel (Normalzustand `"ok"`, gemeldet wird nur beim Wechsel), übersteht Neustarts (`backup.json`); `seed()` übernimmt einen in `failsafe_state.json` persistierten Notbetrieb-Zustand beim Start, ohne selbst zu melden. HA hält `persistent_notification`s nur im Speicher: der Text jeder offenen kritischen Meldung liegt in `notify_messages` (`backup.json`), `republish_persistent()` legt sie bei jedem (Wieder-)Verbinden mit HA ohne Push neu an, `refresh_persistent()` beim wiederholten Startfehler. Hinweis-Kategorien (`HINT_CATEGORIES`, Option `notify_hints_off`) ohne Push; `silent_ok`; `clear_all()` beim Abmelden.
  - `status.py` — Status-Event `smartheat_status` an die Integration (voller Status, `schema` 1, Felder/Wertemengen = Contract-Check 14): nach jedem Worker-Ereignis bei Änderung (`RegulationWorker.after_each`), bei jedem WS-Verbinden und als Lebenszeichen alle 300 s (`EV_HEARTBEAT`).
  - `manual_override.py` — R6: Abweichung von Kurve/Offset ohne Boost/Datenfehler nach 2 Runden (`EV_HEALTH`) melden, nie innerhalb von `OWN_WRITE_SETTLE_SECONDS` (2100 s) nach einem eigenen erfolgreichen Schreiben (`Override.seconds_since_last_write`, nur Laufzeit; mypyllant zeigt den geschriebenen Wert ggf. erst mit dem nächsten 30-min-Poll) (Hinweis-Kategorie `manueller_eingriff`) und als `manual_override` im nächsten Snapshot schicken.
  - `battery.py`/`room_sensors.py` — Überwachung der `battery_entities`/`room_sensors` über einen eigenen `EV_HEALTH`-Takt, nicht kritisch. Eine gelöschte Batterie-Entity (HTTP 404) wird übersprungen; ein gelöschter/ausgefallener Raumfühler (HTTP 404 oder unplausibler Wert) zählt als ausgefallen. Einzelfühler-Meldungen nur ab 2 konfigurierten Raumfühlern (bei einem Fühler ist dessen Ausfall der lokale Datenfehler auf `room_actual`) und erst nach 2 aufeinanderfolgenden Runden ohne gültigen Wert (Laufzeitzähler `room_sensor_misses`); die Rückkehr wird sofort gemeldet.
  - `plausibility.py` — Wertebereiche Raum-/Außentemperatur (`ROOM_TEMP_RANGE`/`OUTDOOR_TEMP_RANGE`), geteilt mit `helper_templates.py`; muss zu `messages.py::PLAUSIBLE_RANGES` auf dem Server passen (Contract-Check).
  - `helper_templates.py` — Jinja-Texte der Template-Hilfssensoren (Raummittel über alle gültigen Raumfühler, Außentemperatur aus `weather.*`).
  - `derived_sensors.py` — Template-Sensoren für Raum-/Außentemperatur, Quellwechsel (legt/löscht/erkennt Neuanlage bei geändertem `room_sensors`/`entity_outdoor_temp`). Gelöscht wird nur über `ha_api.delete_helper`, das ausschließlich Config-Entries von `statistics`-/`template`-Entities löscht (Schutz fremder Integrationen wie `mypyllant`).
  - `snapshot.py`, `telemetry.py`, `boost.py`, `emergency_boost.py`, `safety.py` (lokale Sicherheitswerte je Verteilsystem, nur hier), `windows.py` (Fenster aus den Optionen), `mqtt_client.py`, `manifest.py`, `ha_api.py`, `ha_trigger_client.py`, `entitlement.py`, `daynight_snapshot.py`, `target_history.py`, `clamping.py`, `backup_store.py`.
- `heizungsbruecke/config.yaml` — hat einen echten `schema:`-Block, wird aber **ausschließlich** von der SmartHeat-Integration befüllt, nie manuell in der Add-on-UI.
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

- MQTT-Lokalport `18830` ist in `heizungsbruecke` hart codiert — muss zum `local_port`-Default von `cloudflared_access_mqtt` passen (Cross-Repo-Invariante, siehe `../docs/architecture.md` §9).
- Lokale Sicherheitswerte (`safety.py`) gibt es nur im Add-on. Ihre Schlüssel (Verteilsysteme) spiegelt der Server; Fenster und Basis-URL kommen per Optionen von Server bzw. Integration. `python3 ../tools/contract_check.py` prüft alle Cross-Repo-Duplikate — vor jedem Release grün.
- Versionsstand in `<addon>/config.yaml` (bei `heizungsbruecke` zusätzlich `ADDON_VERSION` in `status.py`, Test prüft den Gleichlauf); seit der CI/CD-Umstellung (2026-09-28) hat jedes Add-on zusätzlich ein `CHANGELOG.md` (Pflichtabschnitt `## X.Y.Z` je Release, geprüft vom Release-Workflow, im Update-Dialog des Supervisors sichtbar) neben dem bisherigen `DOCS.md`.
- Startfehler (`__main__.py::StartupError`/`_fail_start`) melden über einen stabilen `notifier`-Schlüssel `fehler:<key>` (z. B. `hilfs_entities`, `entity_fehlt:<sortierte IDs>`) — der ausführliche Grund steht nur im Feld `grund` des Status-Events, in der Meldung und im Log, nicht in der Meldeidentität, sonst würde ein Neustart mit demselben Fehler jedes Mal erneut melden. Ein im Fehlertext zitierter `mqtt_password`-Wert wird überall (Text, `grund`, Meldung, Retry-Log) durch `***` ersetzt (`_without_credentials`).
- Startbereitschaft: HA gilt erst als erreichbar, wenn `GET /api/config` `state == "RUNNING"` meldet (`ha_api.is_reachable`); vorher wartet der Start unbegrenzt, das ~4-min-Budget für fehlende Entities/Hilfs-Entities zählt nur bei laufendem HA (Cloud-Integrationen wie `mypyllant` laden erst nach dem HTTP-Server).
- Der Status `regelt` gilt ab dem ersten MQTT-Connect (`EV_MQTT_CONNECTED`); im Abo-inaktiv-Modus direkt nach dem Hochfahren. Endzustände sind ein Ruhezustand, weil der Supervisor-Watchdog auch einen Exit 0 neu startet.
- Kein Last Will und nichts unter `smartheat/<tenant>/status/` (B4): der Server erwartet dort nichts mehr. Ein Connect mit einem von der ACL verbotenen Will-Topic nimmt Mosquitto 2 (verifiziert 2.0.11/2.1.2) trotzdem an, nur ein direktes Publish dorthin lehnt sie ab (`tests/test_mosquitto_will_acl.sh`) — kein Verbindungsschutz, also kein Grund, `refresh-acl` auf ältere Add-ons zu warten.
- Ein uncommitteter Worktree/Branch zu einem Ingress-Wizard (`docs/superpowers/{plans,specs}/2026-09-14-heizungsbruecke-ingress-wizard-*.md`) existierte zuletzt als Entwurf, nicht gemerged — vor Arbeit an `web.py`/Ingress-UI prüfen, ob das noch aktuell ist.
