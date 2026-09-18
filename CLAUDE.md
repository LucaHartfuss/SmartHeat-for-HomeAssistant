# SmartHeat-for-HomeAssistant — Repo-Kontext

HA-Add-on-Repository mit zwei Add-ons: `heizungsbruecke` (Client-seitige Bridge-Logik: Snapshot-Publish, Boost, Fail-Safe, lokale Sicherheits-Clamps) und `cloudflared_access_mqtt` (TCP-Tunnel-Forwarder zum Server). Beide laufen auf jedem Kunden-Pi, inkl. `client1`. Volle Beschreibung: `../docs/architecture.md`, Abschnitt 4. Sicherheitsregeln aus `../CLAUDE.md` gelten unverändert — Änderungen hier wirken sich real auf laufende Kundenanlagen aus, sobald deployed.

## Struktur

- `heizungsbruecke/src/heizungsbruecke/`:
  - `__main__.py` — Regelschleife: entkoppelte Kadenzen (`docs/superpowers/specs/2026-09-16-heizungsbruecke-trigger-kadenz-entkopplung-design.md`, implementiert 2026-09-17) — lokaler Boost-/Aenderungs-Check alle `local_check_interval_seconds` (Default 30s, max 60s, kein Server-/MQTT-Kontakt), voller Snapshot-Publish an den Server nur taeglich (`daily_trigger_time`, profilabhaengig) oder bei `target_rt`-Aenderung seit der letzten Veroeffentlichung, zusaetzlich alle `telemetry_interval_seconds` (Default 300s) ein reines KPI-Beobachtungssignal (`room_actual`/`boost_active`/`failsafe_active`) auf `smartheat/{tenant}/telemetry` (KPI-Erfassungs-Spec, implementiert 2026-09-18) — unabhaengig von curve.py/der Steuerung. Siehe `../docs/architecture.md` §8.
  - `boost.py` — aktiviert nur bei Sollwerterhöhung (nicht bei Kälte aus anderer Ursache).
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
- Versionierung/Changelog lebt in `DOCS.md` je Add-on (aktuell `heizungsbruecke` v0.10.0, `cloudflared_access_mqtt` v1.0.0), kein separates `CHANGELOG.md`.
- Ein uncommitteter Worktree/Branch zu einem Ingress-Wizard (`docs/superpowers/{plans,specs}/2026-09-14-heizungsbruecke-ingress-wizard-*.md`) existierte zuletzt als Entwurf, nicht gemerged — vor Arbeit an `web.py`/Ingress-UI prüfen, ob das noch aktuell ist.
