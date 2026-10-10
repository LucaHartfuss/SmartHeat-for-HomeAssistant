# SmartHeat-for-HomeAssistant — Repo-Kontext

Add-on-Repository: `heizungsbruecke` (Client: Snapshot und Telemetrie, Boost, Notbetrieb, lokale Sicherheitswerte),
`cloudflared_access_mqtt` (nur `run.sh` um `cloudflared access tcp`) und `gateway/` (SmartHeat-Gateway ohne Home
Assistant, **kein** Add-on). Die Add-ons laufen auf jedem Kunden-Pi inkl. `client1`: Änderungen wirken real, sobald sie
released sind. Die Sicherheitsregeln aus `../CLAUDE.md` gelten unverändert.

## Struktur (Details: `../docs/architecture.md` §4, §5, §5a)

- `heizungsbruecke/src/smartheat_core/` — HA-freier Client-Kern, nur Standardbibliothek (`tests/test_core_purity.py`).
  `safety.py` = lokale Sicherheitswerte je Hebelsatz × Verteilsystem (Regel 4), `pipeline.py` = einzige Stelle, die
  Hebel schreibt, `boost.py`/`emergency_boost.py`.
- `heizungsbruecke/src/smartheat_runtime/` — hostneutraler Betrieb für Add-on und Gateway (`app.py`, `delivery.py`,
  `status.py` …); Grenzen in `tests/test_runtime_purity.py`: nie `heizungsbruecke`, `paho` nur über
  `smartheat_transport.mqtt_client`, keine Tenant-IDs.
- `heizungsbruecke/src/smartheat_transport/` — Transport-Deskriptor, MQTT (`mqtt_client.py` = einzige Stelle mit
  `paho` im Optionen-Pfad; im Gerätekern nur `smartheat_device/link.py`).
- `heizungsbruecke/src/smartheat_device/` — Gerätekern (Spec 5b): Vertragskopie `wire.py` (wörtlich aus Server
  `device_protocol.py`), Identität, Bootstrap mit Rettungsweg, die eine MQTT-Verbindung (`link.py`), Dokumentspeicher,
  Befehle, Bedienwunsch, Inventur, `device.py` (Thread, hello, Status), `laufzeit.py` (Laufzeit auf dem Link). Grenzen:
  `tests/test_device_purity.py`; das Add-on nutzt ihn erst ab 5c, das Gateway ab 0.6.0. `smartheat_core.config_check`
  prüft eine Konfiguration lokal vor dem Übernehmen.
- `heizungsbruecke/src/heizungsbruecke/` — HA-Host: `HaHost`, REST/WebSocket, Bindings für mypyllant, Weishaupt, Viessmann.
- `gateway/` — Laufzeit-Host, Agent, Compose (§5a.1–5a.7); `gateway/host/` Host-Dienste, Updater, Installer auf dem
  System-Python des Pi (§5a.8–5a.12); `gateway/image/` Basis-Image mit `rpi-image-gen` (§5a.13); `gateway/release/`
  Bundle und Signatur (§5a.10). Grenzen: `gateway/tests/test_boundaries.py`, `gateway/host/tests/test_boundaries.py`.

## Prüfen und Branches

```
scripts/check.sh          # lint, test, contract (--only <schritt> für einzelne Schritte)
scripts/check.sh --full   # zusätzlich die Docker-Schritte (Build, Happy-Path, run.sh, Will-ACL, Gateway-Image und Compose-Lauf)
```
`scripts/check.sh` prüft und testet `heizungsbruecke/`, `gateway/`, `gateway/host/` **und** `gateway/release/`; `--full` baut zusätzlich das Gateway-Image
(lokal nur amd64) und fährt den Compose-Lauf mit dem Dev-Overlay (`tests/test_gateway_docker_build.sh`; dabei Bus-Anmeldung und ACL, Masken, Netz-Wache; die statische
Prüfung der Compose-Datei steckt in `gateway/tests/test_compose.py`). Drei weitere Docker-Skripte gehören zum Gateway: `tests/test_gateway_install.sh` (Host-Installer zweimal in `debian:trixie`, Plattform per `SHG_INSTALL_PLATFORM`, die CI fährt zusätzlich arm64 im Job `install-arm64`), `tests/test_gateway_updater.sh` (Updater gegen eine lokale Registry mit drei signierten Test-Bundles, Rückweg und Manipulation; Laufzeit rund 6 min, steuert Compose über den Docker-Socket des Hosts) und `tests/test_gateway_firstboot.sh` (Export wie im Image-Bau, Laden ohne Netz in einem Docker mit containerd-Speicher, Start per Index-Digest). Der Image-Bau selbst (`gateway/image/make_image.sh`) braucht einen arm64-Host und gehört nicht zu `check.sh`. Im Dev-Root fährt `scripts/check.sh --only e2e --full`
zusätzlich die Gateway-Modi des Ende-zu-Ende-Tests.
Direkter Aufruf bleibt möglich: `cd heizungsbruecke && pip install -e ".[dev]" && pytest` bzw.
`cd gateway && pip install -e ".[dev]" && pytest` (`pyproject.toml`: `testpaths = ["tests"]`, `pythonpath = ["src"]`). Testzahl: siehe CI (Job
`test`). Shell-Integrationstests liegen im Repo-Root unter `tests/` (`run_all.sh`).

Feature-Branches (`feat/…`/`fix/…`) zweigen von `develop` ab und werden `--no-ff` nach `develop`
gemergt — nie direkt nach `main`. `main` bewegt sich nur per Release-Tag
(`heizungsbruecke-vX.Y.Z`, `cloudflared_access_mqtt-vX.Y.Z` über `release.yml`, `gateway-vX.Y.Z` über
`release-gateway.yml`; `-dryrun`-Suffix = Probelauf) —
ein Push nach `main` ist ein Release an alle Kunden-Pis, deren Supervisor den Default-Branch
verfolgt. Release-Ablauf, CI-Jobs, Token: `../docs/ci-cd-runbook.md`.

## Regeln und Fallen

- `heizungsbruecke/config.yaml` hat einen `schema:`-Block, wird aber **nur** von der Integration befüllt, nie von Hand.
- **Golden-Master** (`heizungsbruecke/tests/test_golden_master.py`, `tests/golden/addon_scenario.json`) nie neu erzeugen,
  ohne dass ein Plan es verlangt: ändert er sich, ist der Code falsch, nicht die Datei.
- **Verträge:** Gerätevertrag `gateway/src/smartheat_gateway/agent/wire.py` ↔ `../tools/contracts/shg_device_v1.json` ↔
  Server `devices_wire.py` nur gemeinsam ändern (Check 44); Status-Event, Hebel-, Rollen- und Optionsnamen, Ports und
  Sicherheitswert-Schlüssel prüft `python3 ../tools/contract_check.py` (vor jedem Release grün; `architecture.md` §9).
- **Versionen:** `<addon>/config.yaml`, bei `heizungsbruecke` zusätzlich `ADDON_VERSION` in `src/heizungsbruecke/version.py`
  (Test prüft den Gleichlauf), je Add-on `CHANGELOG.md` mit `## X.Y.Z`; Gateway: `gateway/VERSION` und
  `gateway/CHANGELOG.md`.
- **`gateway/` ist kein Add-on** (keine `config.yaml`): Image aus der Repo-Wurzel (`docker build -f gateway/Dockerfile .`),
  eigener Release-Workflow; ohne echten Schlüssel in `gateway/host/release.pub` lehnt das Gate echte `gateway-v*`-Tags ab.
  Änderungen in `smartheat_core`/`smartheat_runtime` wirken auf beide Clienttypen.
- **Startfehler** melden mit stabilem Schlüssel `fehler:<key>`, der Grund steht nur im Feld `grund`; Geheimnisse
  (`config.SECRET_OPTIONS`) werden überall durch `***` ersetzt (`host._without_credentials`).
- **Endzustände sind ein Ruhezustand**, kein Exit, weil der Supervisor-Watchdog auch Exit 0 neu startet
  (`tests/test_heizungsbruecke_docker_build.sh`). Kein Last Will, nichts unter `smartheat/<tenant>/status/`.
- **Lock-Datei** `heizungsbruecke/requirements.txt`: das Image installiert nur daraus (`--require-hashes`). Erneuern nur bei
  geänderten `dependencies` in `pyproject.toml` oder bewusstem Bump (dann `--upgrade` vor `--generate-hashes`, Diff lesen),
  im Ordner `heizungsbruecke/`:

  ```
  tar cf - pyproject.toml requirements.txt | docker run --rm -i \
    python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f \
    sh -c "mkdir /w && cd /w && tar xf - && pip install --quiet pip-tools >/dev/null 2>&1 \
           && pip-compile --quiet --generate-hashes --strip-extras --output-file requirements.txt pyproject.toml \
           && tar cf - requirements.txt" | tar xf -
  ```

  Danach `scripts/check.sh --only docker --full`; den Drift-Check übernimmt `scripts/ci/pin_check.py`.
