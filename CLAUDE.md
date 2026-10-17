# SmartHeat-for-HomeAssistant — Repo-Kontext

Add-on-Repository: `heizungsbruecke` (ab 1.0.0 ein **SmartHeat-Gerät** für Home Assistant, wie das Gateway auf dem
Gerätekern: Snapshot und Telemetrie, Boost, Notbetrieb, lokale Sicherheitswerte; `client1` läuft bis Phase 1 Schritt 3 noch
auf 0.36 im Optionsmodus) und `gateway/` (SmartHeat-Gateway ohne Home Assistant, **kein** Add-on). Das Add-on läuft auf
jedem Kunden-Pi inkl. `client1`: Änderungen wirken real, sobald sie released sind. Die Sicherheitsregeln aus
`../CLAUDE.md` gelten unverändert.

## Struktur und Module

Dateien, Schnittstellen, Daten, Invarianten (Regel 4, Boost, Notbetrieb) und Tests je Modul stehen in der Modul-Doku des
Dev-Roots, nicht hier:

- Modulkarte, Verträge V1–V5: `../docs/module/README.md`
- Client-Kern (`heizungsbruecke/src/smartheat_{core,runtime,device}`, hostfrei; lokale Sicherheit in
  `smartheat_core/safety.py`, `pipeline.py` ist die einzige Stelle, die Hebel schreibt): `../docs/module/client-kern.md`
- Gateway-Host (`gateway/`, kein Add-on): `../docs/module/gateway-host.md`
- HA-Host (`heizungsbruecke/src/heizungsbruecke/`, Discovery, Bindings) und Integration als Oberfläche:
  `../docs/module/ha-host.md`
- Überblick, Datenflüsse, Deployment: `../docs/architecture.md`

Paketgrenzen erzwingt `heizungsbruecke/tests/test_modulgrenzen.py` (Tabellen `ERLAUBT` und `DRITTE`: erlaubte Importe je
Paket, Drittpakete je Datei, das Gateway-Image kopiert jedes Kernpaket); dazu `test_runtime_purity.py` und
`test_device_purity.py` (Tenant-IDs, `wire.py`), `gateway/tests/test_boundaries.py` und
`gateway/host/tests/test_boundaries.py` (Host-Dienste: nur Standardbibliothek, `cryptography`, `paths`, `version` und
`smartheat_core.dateien`). Wer ein Modul ändert, zieht dessen Datei in `../docs/module/` im selben Merge nach.

## Prüfen und Branches

```
scripts/check.sh          # lint, test, contract (--only <schritt> für einzelne Schritte)
scripts/check.sh --full   # zusätzlich die Docker-Schritte (Add-on-Image bauen und reproduzierbar, Gateway-Image, Installer, Updater, Erststart)
```
`scripts/check.sh` prüft und testet `heizungsbruecke/`, `gateway/`, `gateway/host/` **und** `gateway/release/`; `--full` baut zusätzlich das Gateway-Image
(lokal nur amd64) und fährt den Compose-Lauf mit dem Dev-Overlay (`tests/test_gateway_docker_build.sh`; dabei Bootstrap gegen einen Fake, MQTT gegen einen Fake-Broker mit Client-Zertifikat, Fälschungsproben und sauberes Trennen, Bus-Anmeldung und ACL; die statische
Prüfung der Compose-Datei steckt in `gateway/tests/test_compose.py`). Der Image-Scan `tests/test_image_scan.sh` (`trivy` in einem gepinnten Container über Add-on-Image, Gateway-Image und die Fremd-Images der Compose-Datei; CRITICAL mit Korrektur ist rot, Ausnahmen begründet in `.trivyignore`; braucht Netz, Datenbank im Docker-Volume `smartheat-trivy-cache`) läuft im selben Schritt. Drei weitere Docker-Skripte gehören zum Gateway: `tests/test_gateway_install.sh` (Host-Installer zweimal in `debian:trixie`, Plattform per `SHG_INSTALL_PLATFORM`, die CI fährt zusätzlich arm64 im Job `install-arm64`), `tests/test_gateway_updater.sh` (Updater gegen eine lokale Registry mit drei signierten Test-Bundles, Soll-Version als Konfigurationsdokument über den Fake-Broker, Rückweg und Manipulation; Laufzeit rund 6 min, steuert Compose über den Docker-Socket des Hosts) und `tests/test_gateway_firstboot.sh` (Export wie im Image-Bau, Laden ohne Netz in einem Docker mit containerd-Speicher, Start per Index-Digest). Der Image-Bau selbst (`gateway/image/make_image.sh`) braucht einen arm64-Host und gehört nicht zu `check.sh`. Im Dev-Root fährt `scripts/check.sh --only e2e --full`
zusätzlich die Gateway-Modi des Ende-zu-Ende-Tests.
Direkter Aufruf bleibt möglich: `cd heizungsbruecke && pip install -e ".[dev]" && pytest` bzw.
`cd gateway && pip install -e ".[dev]" && pytest` (`pyproject.toml`: `testpaths = ["tests"]`, `pythonpath = ["src"]`). Testzahl: siehe CI (Job
`test`). Shell-Integrationstests liegen im Repo-Root unter `tests/` (`run_all.sh`).

Feature-Branches (`feat/…`/`fix/…`) zweigen von `develop` ab und werden `--no-ff` nach `develop`
gemergt — nie direkt nach `main`. `main` bewegt sich nur per Release-Tag
(`heizungsbruecke-vX.Y.Z` über `release.yml`, `gateway-vX.Y.Z` über
`release-gateway.yml`; `-dryrun`-Suffix = Probelauf) —
ein Push nach `main` ist ein Release an alle Kunden-Pis, deren Supervisor den Default-Branch
verfolgt. Release-Ablauf, CI-Jobs, Token: `../docs/ci-cd-runbook.md`.

## Regeln und Fallen

- `heizungsbruecke/config.yaml` hat ab 1.0.0 nur noch die Optionen `bootstrap_url` und `log_level` (beide optional,
  nur für Labor und Tests); Zuordnung, Zugang und Intervalle kommen als Konfigurationsdokument vom Server, die Integration
  schreibt keine Optionen mehr. `hassio_api: true` und `discovery: [smartheat]` sind Pflicht (Uhr per `/host/info`,
  Discovery); `host_network` entfällt. `SMARTHEAT_BOOTSTRAP_URL` (Umgebung) schlägt die Option, nur für Tests und E2E.
  Eine ungültige `bootstrap_url` oder eine kaputte `options.json` lässt das Add-on **ruhen** (nie auf die Produktion
  ausweichen). Die Option nimmt nur `https` (Spec 5c F/Q4); `http` geht nur über die Umgebung
  `SMARTHEAT_BOOTSTRAP_URL` (E2E und Tests, im Add-on nicht setzbar).
- **Golden-Master** (`heizungsbruecke/tests/test_golden_master.py`, `tests/golden/addon_scenario.json`) nie neu erzeugen,
  ohne dass ein Plan es verlangt: ändert er sich, ist der Code falsch, nicht die Datei.
- **Verträge:** Gerätevertrag `heizungsbruecke/src/smartheat_device/wire.py` (wörtliche Kopie des Vertragsteils aus
  `HeizungssteuerungServer/src/heizungsserver/geraete/device_protocol.py`, Test `test_device_wire.py`) ↔
  `../tools/contracts/geraet_v1.json` nur gemeinsam ändern (Contract-Check, Vertrag V1, Liste: `python3
  ../tools/contract_check.py --liste`; Topics gegen die IoT-Policies: Server-Test `tests/test_vertrag_im_server.py`);
  Status-Event, Hebel-, Rollen- und Optionsnamen, Ports und
  Sicherheitswert-Schlüssel prüft `python3 ../tools/contract_check.py` (vor jedem Release grün; Übersicht der Verträge `../docs/module/README.md`).
  Das Status-Ereignis des Add-ons hat Schema 3 (`geraet_status.EVENT_FIELDS`, Laufzeit-Felder aus
  `smartheat_runtime/status.py` plus Gerät); die Felder der Supervisor-Discovery (`discovery.FIELDS`) und das Ereignis
  `smartheat_wunsch` sind Verträge mit der Integration (Plan I).
- **Versionen:** `<addon>/config.yaml`, bei `heizungsbruecke` zusätzlich `ADDON_VERSION` in `src/heizungsbruecke/version.py`
  (Test prüft den Gleichlauf), je Add-on `CHANGELOG.md` mit `## X.Y.Z`; Gateway: `gateway/VERSION` und
  `gateway/CHANGELOG.md`.
- **`gateway/` ist kein Add-on** (keine `config.yaml`): Image aus der Repo-Wurzel (`docker build -f gateway/Dockerfile .`),
  eigener Release-Workflow; ohne echten Schlüssel in `gateway/host/release.pub` lehnt das Gate echte `gateway-v*`-Tags ab.
  Änderungen in `smartheat_core`/`smartheat_runtime` wirken auf beide Clienttypen.
- **Startfehler** melden mit stabilem Schlüssel `fehler:<key>`, der Grund steht nur im Feld `grund`. Schlüssel,
  Zertifikate, Übernahme-Code, Supervisor-Token und Inventur-Inhalte stehen nie in Log, Status-Ereignis oder
  Diagnose (Regel 6); der Übernahme-Code geht nur in die Discovery (bleibt in der HA-Instanz), ins Log kommt bei
  Fehlern nur die Fehlerart.
- **Neustart** (neue laufzeitrelevante Konfiguration, `sign_off`, Abo wieder aktiv, Neuprüfung im Konfigurationsfehler):
  ein Lebenszyklus für beide Hosts in `smartheat_device/prozess.py`. Erst sauber trennen (`prozess.beenden`: Gerätekern
  stoppen, Thread abwarten, noch einmal stoppen; sonst meldet IoT Core `DUPLICATE_CLIENTID`), dann der Neustart des
  Hosts (HA `__main__._exec` mit `os.execv`, das Gateway kehrt aus `main` zurück). Die Laufzeit meldet ihren Neustart
  über den Pflicht-Rückruf `neustart` von `app.start` (`prozess.neustart_der_laufzeit`), nichts wird global
  überschrieben. Ein unerwarteter Fehler pausiert 60 s (in beiden Hosts) und startet dann ebenso neu; davor meldet sich
  das Gateway ungesund (Rückruf `vor_pause`: Lebenszeichen weg, Laufzeit `startet`), damit der Updater ein solches
  Update zurückrollt. SIGTERM endet ohne Neustart. Jede gewollte Neustart-Logzeile endet auf „, Laufzeit startet neu“
  (der E2E `gateway2` zählt sie).
- **Gemeinsamer Teil der Hosts:** `smartheat_device/geraet_host_basis.py` (Eltern von `HaDeviceHost` und
  `GatewayDeviceHost`) und `smartheat_runtime/raum.py`. Ein Verhalten, das beide Hosts teilen, gehört dorthin, nicht
  doppelt in die Hosts; es wird zuerst in den Konformitätssuiten festgehalten (`testhilfen/host_konformitaet.py`,
  `testhilfen/trigger_konformitaet.py`, je ein Adapter in `heizungsbruecke/tests` und `gateway/tests`). Eine bewusste
  Abweichung eines Hosts steht begründet in `ABWEICHUNGEN` seines Adapters.
- **Endzustände sind ein Ruhezustand**, kein Exit, weil der Supervisor-Watchdog auch Exit 0 neu startet
  (`tests/test_heizungsbruecke_docker_build.sh`). Kein Last Will, nichts unter `smartheat/<tenant>/status/`. Der
  Docker-Test startet das Add-on mit `SMARTHEAT_BOOTSTRAP_URL=http://127.0.0.1:9` (ohne Server). Den Ablauf mit Server,
  Broker und Fake-HA (Discovery, Einrichtung, Regelbetrieb, Wünsche, Entfernen) prüft der Root-E2E-Modus `ha2`
  (`tools/e2e/run.sh`, Spec 5c Plan W); das frühere Happy-Path-Skript (Optionsmodus) ist entfallen.
- **Soll-Entity (HA)**: nur Eingang und Anzeige; die Regelung liest den Raum-Soll aus dem Soll-Speicher. Schreiben nur an
  `climate.*` (Attribut `temperature`); ein gescheitertes Angleichen bleibt offen und wird nachgeholt
  (`HaSollQuelle.angleichen_offen`, `geraet_trigger.build`), statt den gezeigten Altwert als Wunsch zu nehmen. Änderungen
  hier immer mit den Tests `test_geraet_trigger.py`/`test_host_dokument.py` (Echo/Wunsch-Schleife ist das Hauptrisiko).
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
