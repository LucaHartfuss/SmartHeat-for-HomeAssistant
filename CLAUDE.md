# SmartHeat-for-HomeAssistant — Repo-Kontext

HA-Add-on-Repository mit zwei Add-ons: `heizungsbruecke` (Client-seitige Bridge-Logik: Snapshot-Publish, Boost, Fail-Safe, lokale Sicherheits-Clamps) und `cloudflared_access_mqtt` (TCP-Tunnel-Forwarder zum Server). Beide laufen auf jedem Kunden-Pi, inkl. `client1`. Volle Beschreibung: `../docs/architecture.md`, Abschnitt 4. Sicherheitsregeln aus `../CLAUDE.md` gelten unverändert — Änderungen hier wirken sich real auf laufende Kundenanlagen aus, sobald deployed.

## Struktur

- `heizungsbruecke/src/heizungsbruecke/`:
  - `__main__.py` — Regelschleife (aktuell einzelne synchrone Schleife, Default 1h-Poll — der in `docs/superpowers/specs/2026-09-16-heizungsbruecke-trigger-kadenz-entkopplung-design.md` entworfene Split ist **noch nicht implementiert**, siehe `../docs/architecture.md` §8).
  - `boost.py` — aktiviert nur bei Sollwerterhöhung (nicht bei Kälte aus anderer Ursache).
  - `failsafe.py` — Staleness-Watchdog, Default 4h.
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
- Versionierung/Changelog lebt in `DOCS.md` je Add-on (aktuell `heizungsbruecke` v0.8.0, `cloudflared_access_mqtt` v1.0.0), kein separates `CHANGELOG.md`.
- Ein uncommitteter Worktree/Branch zu einem Ingress-Wizard (`docs/superpowers/{plans,specs}/2026-09-14-heizungsbruecke-ingress-wizard-*.md`) existierte zuletzt als Entwurf, nicht gemerged — vor Arbeit an `web.py`/Ingress-UI prüfen, ob das noch aktuell ist.
