# SmartHeat-for-HomeAssistant — Repo-Kontext

HA-Add-on-Repository mit zwei Add-ons: `heizungsbruecke` (Client-seitige Bridge-Logik: Snapshot-Publish, Boost, Fail-Safe, lokale Sicherheits-Clamps) und `cloudflared_access_mqtt` (TCP-Tunnel-Forwarder zum Server). Beide laufen auf jedem Kunden-Pi, inkl. `client1`. Volle Beschreibung: `../docs/architecture.md`, Abschnitt 4. Sicherheitsregeln aus `../CLAUDE.md` gelten unverändert — Änderungen hier wirken sich real auf laufende Kundenanlagen aus, sobald deployed.

## Struktur

- `heizungsbruecke/src/heizungsbruecke/`:
  - `__main__.py` — Regelschleife: seit 2026-09-21/22 event-getrieben (`docs/superpowers/specs/2026-09-21-heizungsbruecke-eventgetriebene-trigger-design.md`, live auf `client1` seit v0.11.0) — lokaler Boost-/Aenderungs-Check (`_run_local_check`) laeuft primaer ueber `HaTriggerClient`/HA-Core-`subscribe_trigger` (State-Trigger auf `room_target`/`room_actual`, Zeit-Trigger auf `daily_trigger_time`), `local_check_interval_seconds` (Default 300s, 1-3600s) ist nur noch der Watchdog-Fallback-Takt bei getrennter WS-Verbindung, nicht mehr die primaere Kadenz. Voller Snapshot-Publish an den Server weiterhin nur taeglich oder bei `target_rt`-Aenderung seit der letzten Veroeffentlichung, zusaetzlich alle `telemetry_interval_seconds` (Default 300s) ein reines KPI-Beobachtungssignal auf `smartheat/{tenant}/telemetry` — unabhaengig von curve.py/der Steuerung. **Bekannte Luecke (Fix in Planung, `docs/superpowers/specs/2026-09-22-heizungsbruecke-zieltemperatur-debounce-design.md`):** `room_target` wird bei jedem `_run_local_check`-Aufruf live gelesen, unabhaengig vom ausloesenden Trigger — ein `room_actual`-Event waehrend einer laufenden Solltemp-Aenderung kann daher noch einen nicht-finalen Wert in Boost-Start/Heizkurvenanpassung einfliessen lassen. Siehe `../docs/architecture.md` §4.1/§8.
  - `boost.py` — Start aktiviert nur bei Sollwerterhöhung (`room_target`-Trigger), Ende erst wenn `room_actual` die Solltemperatur erreicht (`room_actual`-Trigger noetig, kein Start-Ausloeser bei Kälte aus anderer Ursache).
  - `failsafe.py` — Staleness-Watchdog, Default 24h (seit 2026-09-17 wieder gelockert, siehe Trigger-Kadenz-Entkopplung-Spec Abschnitt C).
  - `profiles.py` — lokale Sicherheits-Clamps je `profile_id` (dupliziert zum Server, kein gemeinsamer Code).
  - `mqtt_client.py` — Verbindung fest auf `127.0.0.1:18830` codiert.
  - `manifest.py` — Rollen-Definitionen (`ALL_ROLES`).
- `heizungsbruecke/config.yaml` — hat einen echten `schema:`-Block, wird aber **ausschließlich** von der SmartHeat-Integration befüllt, nie manuell in der Add-on-UI.
- `cloudflared_access_mqtt/` — nur `run.sh`-Wrapper um `cloudflared access tcp`, keine eigene Logik.

## Tests

```
cd heizungsbruecke
pip install -e ".[dev]"
pytest
```
(`pyproject.toml`: `testpaths = ["tests"]`, `pythonpath = ["src"]`.) 176 Testfunktionen in 14 Dateien. Zusätzlich Shell-Integrationstests im Repo-Root unter `tests/` (`run_all.sh`, Docker-Build/Happy-Path).

## Besonderheiten

- MQTT-Lokalport `18830` ist in `heizungsbruecke` hart codiert — muss zum `local_port`-Default von `cloudflared_access_mqtt` passen (Cross-Repo-Invariante, siehe `../docs/architecture.md` §9).
- Lokale Clamps (`profiles.py`) sind bewusst dupliziert zum Server (`heizungsserver/src/heizungsserver/generic/profiles.py`) — bei jeder Profiländerung beide Seiten prüfen.
- Versionierung/Changelog lebt in `DOCS.md` je Add-on (aktuell `heizungsbruecke` v0.10.2, `cloudflared_access_mqtt` v1.0.0), kein separates `CHANGELOG.md`.
- Ein uncommitteter Worktree/Branch zu einem Ingress-Wizard (`docs/superpowers/{plans,specs}/2026-09-14-heizungsbruecke-ingress-wizard-*.md`) existierte zuletzt als Entwurf, nicht gemerged — vor Arbeit an `web.py`/Ingress-UI prüfen, ob das noch aktuell ist.
