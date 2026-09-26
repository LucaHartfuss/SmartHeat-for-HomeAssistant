# SmartHeat-for-HomeAssistant — Repo-Kontext

HA-Add-on-Repository mit zwei Add-ons: `heizungsbruecke` (Client-seitige Bridge-Logik: Snapshot-Publish, Boost, Fail-Safe, lokale Sicherheits-Clamps) und `cloudflared_access_mqtt` (TCP-Tunnel-Forwarder zum Server). Beide laufen auf jedem Kunden-Pi, inkl. `client1`. Volle Beschreibung: `../docs/architecture.md`, Abschnitt 4. Sicherheitsregeln aus `../CLAUDE.md` gelten unverändert — Änderungen hier wirken sich real auf laufende Kundenanlagen aus, sobald deployed.

## Struktur

- `heizungsbruecke/src/heizungsbruecke/`:
  - `__main__.py` — Boot (`_start_bridge`, `_prime`), dünne Handler des `RegulationWorker` (`_on_*`), `_run_bridge` (Exit-Code: 1 nur bei Konfigurationsfehlern), `main`.
  - `runtime.py` — `Runtime` (Laufzeit-Kontext) und die Ereignisarten `EV_*`.
  - `config.py` — Pflichtfelder, Profilwerte, Startprüfungen, Dateipfade, feste Adressen (MQTT `127.0.0.1:18830`, accounts-api).
  - `state.py` — `BridgeState` + `StateStore`: der gesamte Zustand, `backup.json`/`failsafe_state.json` einmal geladen und nur bei Änderung geschrieben.
  - `override.py` — einzige Stelle, die Kurve/Offset auf die Anlage schreibt: Sollwert-Regel Notfall-Boost > Comfort-Boost > Wiederherstellungspunkt, Schreiben nur beim Wechsel, immer geclampt.
  - `regulation.py` — lokaler Check (Comfort-Boost nur bei Sollwerterhöhung, Notfall-Boost nur im Notbetrieb, Stable-Target-Cache mit 10-s-Entprellung) und „Tick fällig?“.
  - `ticks.py` — führt die Aktionen von `delivery.py` aus, verarbeitet Server-Antworten.
  - `delivery.py` — reine Zustandsmaschine der Tick-Zustellung (Phasen, Retry mit derselben `seq`, Notbetrieb nach 2 Ack-Timeouts, Datenfehler lokal/Server/Anlage).
  - `abo.py` — Abo-inaktiv-Modus und Fristende.
  - `triggers.py` — HA-Trigger-Client (WebSocket) und MQTT-Client-Aufbau; Callbacks stellen nur in den Worker ein.
  - `worker.py` — Regel-Worker (Event-Queue + Zeitplan), alle Regelungsereignisse nacheinander im Hauptthread.
  - `snapshot.py`, `telemetry.py`, `boost.py`, `emergency_boost.py`, `profiles.py` (lokale Clamps je `profile_id`, dupliziert zum Server), `mqtt_client.py`, `manifest.py`, `ha_api.py`, `ha_trigger_client.py`, `entitlement.py`, `derived_sensors.py`, `daynight_snapshot.py`, `target_history.py`, `clamping.py`, `backup_store.py`.
- `heizungsbruecke/config.yaml` — hat einen echten `schema:`-Block, wird aber **ausschließlich** von der SmartHeat-Integration befüllt, nie manuell in der Add-on-UI.
- `cloudflared_access_mqtt/` — nur `run.sh`-Wrapper um `cloudflared access tcp`, keine eigene Logik.

## Tests

```
cd heizungsbruecke
pip install -e ".[dev]"
pytest
```
(`pyproject.toml`: `testpaths = ["tests"]`, `pythonpath = ["src"]`.) 550 Testfunktionen in 28 Dateien (`pytest -q --collect-only`). Zusätzlich Shell-Integrationstests im Repo-Root unter `tests/` (`run_all.sh`, Docker-Build/Happy-Path).

## Besonderheiten

- MQTT-Lokalport `18830` ist in `heizungsbruecke` hart codiert — muss zum `local_port`-Default von `cloudflared_access_mqtt` passen (Cross-Repo-Invariante, siehe `../docs/architecture.md` §9).
- Lokale Clamps (`profiles.py`) sind bewusst dupliziert zum Server (`heizungsserver/src/heizungsserver/generic/profiles.py`) — bei jeder Profiländerung beide Seiten prüfen.
- Versionierung/Changelog lebt in `DOCS.md` je Add-on (aktuell `heizungsbruecke` v0.17.0, `cloudflared_access_mqtt` v1.0.0), kein separates `CHANGELOG.md`.
- Ein uncommitteter Worktree/Branch zu einem Ingress-Wizard (`docs/superpowers/{plans,specs}/2026-09-14-heizungsbruecke-ingress-wizard-*.md`) existierte zuletzt als Entwurf, nicht gemerged — vor Arbeit an `web.py`/Ingress-UI prüfen, ob das noch aktuell ist.
