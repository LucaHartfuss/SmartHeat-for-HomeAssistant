# SmartHeat-for-HomeAssistant — Repo-Kontext

Add-on-Repository: `heizungsbruecke` (ein **SmartHeat-Gerät** für Home Assistant, wie das Gateway auf dem Gerätekern:
Snapshot und Telemetrie, Boost, Notbetrieb, lokale Sicherheitswerte) und `gateway/` (SmartHeat-Gateway ohne Home
Assistant, **kein** Add-on). Das Add-on läuft auf jedem Kunden-Pi inkl. `client1`: Änderungen wirken real, sobald sie
released sind. Die Sicherheitsregeln aus `../CLAUDE.md` gelten unverändert.

## Struktur und Module

Dateien, Schnittstellen, Daten, Invarianten (Regel 4, Boost, Notbetrieb) und Tests je Modul stehen in der Modul-Doku des
Dev-Roots, nicht hier:

- Modulkarte, Verträge V1–V5, Paketgrenzen („Abhängigkeiten im Geräte-Repo“): `../docs/module/README.md`
- Client-Kern (`heizungsbruecke/src/smartheat_{core,runtime,device}`, hostfrei; lokale Sicherheit in
  `smartheat_core/safety.py`, `pipeline.py` ist die einzige Stelle, die Hebel schreibt; Grenztests unter „Tests“):
  `../docs/module/client-kern.md`
- Gateway-Host (`gateway/`, kein Add-on): `../docs/module/gateway-host.md`
- HA-Host (`heizungsbruecke/src/heizungsbruecke/`, Discovery, Bindings) und Integration als Oberfläche:
  `../docs/module/ha-host.md`
- Überblick, Datenflüsse, Deployment: `../docs/architecture.md`; Branches, Releases, Versionen, CI, Pins:
  `../docs/ci-cd-runbook.md`

## Prüfen

```
scripts/check.sh          # lint (mit vulture), test, contract (--only <schritt> für einzelne Schritte)
scripts/check.sh --full   # zusätzlich der Schritt docker (Liste: docker_tests in scripts/check.sh)
```

`scripts/check.sh` prüft und testet `heizungsbruecke/`, `gateway/`, `gateway/host/` **und** `gateway/release/` (alle vier
pytest-Läufe auch bei einem roten; ohne `--full` steht `docker` als „uebersprungen“ in der Ausgabe, der Schlusssatz
heißt `ALLES GRUEN (ohne: docker)`). Das Skript und `tests/test_heizungsbruecke_docker_build.sh` prüft
`tools/tests/test_addon_skripte.py` im Dev-Root. Toter
Code (vulture, `vulture_ausnahmen.py`): `../docs/ci-cd-runbook.md` §3; Image-Scan
(`tests/test_image_scan.sh`, `.trivyignore`, Volume `smartheat-trivy-cache`): §12. Die übrigen Docker-Skripte
(Shell-Tests im Repo-Root unter `tests/`):

| Skript | Prüft |
| --- | --- |
| `test_heizungsbruecke_docker_build.sh`, `test_heizungsbruecke_reproducible.sh` | Add-on-Image bauen (Start ohne Server mit `SMARTHEAT_BOOTSTRAP_URL=http://127.0.0.1:9`, Ruhezustand) und reproduzierbar |
| `test_gateway_docker_build.sh` | Gateway-Image (lokal nur amd64) und Compose-Lauf mit Dev-Overlay: Bootstrap gegen einen Fake, MQTT gegen einen Fake-Broker mit Client-Zertifikat, Fälschungsproben, sauberes Trennen, Bus-Anmeldung und ACL; statisch prüft `gateway/tests/test_compose.py` |
| `test_gateway_install.sh` | Host-Installer zweimal in `debian:trixie`, Plattform per `SHG_INSTALL_PLATFORM` (arm64 nur in der CI, Job `install-arm64`) |
| `test_gateway_updater.sh` | Updater gegen eine lokale Registry mit drei signierten Test-Bundles, Soll-Version als Konfigurationsdokument über den Fake-Broker, Rückweg und Manipulation; rund 6 min, steuert Compose über den Docker-Socket des Hosts |
| `test_gateway_firstboot.sh` | Export wie im Image-Bau, Laden ohne Netz in einem Docker mit containerd-Speicher, Start per Index-Digest |

Der Image-Bau selbst (`gateway/image/make_image.sh`) braucht einen arm64-Host und gehört nicht zu `check.sh`.
Ende zu Ende (Server, Broker, Fake-HA bzw. Gateway: Discovery, Einrichtung, Regelbetrieb, Wünsche, Entfernen) fährt der
Dev-Root (`tools/e2e/run.sh`, Modi `gateway2`, `gateway2_vicare`, `ha2`; `scripts/check.sh --only e2e --full`).

Direkter Aufruf: `cd heizungsbruecke && pip install --require-hashes --no-deps --only-binary :all: -r
requirements-hypothesis.lock && pip install -e ".[dev]" && pytest` (`hypothesis` mit Hash-Pin, nur als Wheel,
Profile über `HYPOTHESIS_PROFILE`, `--full` setzt `voll`; `pyproject.toml`: `pythonpath = ["src", "testhilfen"]`) bzw.
`cd gateway && pip install -e ".[dev]" && pytest` (`pythonpath = ["src", "../heizungsbruecke/src",
"../heizungsbruecke/testhilfen"]`); `gateway/host` und `gateway/release` setzen eigene Pfade in ihrer `pyproject.toml`.

Release-Tags: `heizungsbruecke-vX.Y.Z` (`release.yml`), `gateway-vX.Y.Z` (`release-gateway.yml`), Suffix `-dryrun` =
Probelauf. Ein Push nach `main` ist ein Release an alle Kunden-Pis, deren Supervisor den Default-Branch verfolgt.

## Regeln und Fallen

- **Optionen** (`heizungsbruecke/config.yaml`): nur `bootstrap_url` und `log_level`, beide nur für Labor und Tests
  (Pflichtfelder der `config.yaml` und Startweg: `../docs/module/ha-host.md`). Eine ungültige `bootstrap_url` oder eine
  kaputte `options.json` lässt das Add-on **ruhen**, nie auf die Produktion ausweichen. Die Option nimmt nur `https`;
  `http` geht nur über die Umgebung `SMARTHEAT_BOOTSTRAP_URL` (E2E und Tests, im Add-on nicht setzbar).
- **Golden-Master** (`heizungsbruecke/tests/test_golden_master.py`, `heizungsbruecke/tests/golden/addon_scenario.json`
  und `addon_scenario_0_36.json`) nie neu erzeugen, ohne dass ein Plan es verlangt: ändert er sich, ist der Code falsch,
  nicht die Datei.
- **Verträge:** `heizungsbruecke/src/smartheat_device/wire.py` und `../tools/contracts/geraet_v1.json` nur gemeinsam
  ändern (V1, `../docs/module/README.md`); `python3 ../tools/contract_check.py` vor jedem Release grün (Liste:
  `--liste`). Verträge mit der Integration: Status-Ereignis Schema 3 (`geraet_status.EVENT_FIELDS`), Felder der
  Supervisor-Discovery (`discovery.FIELDS`), Ereignis `smartheat_wunsch`.
- **`gateway/` ist kein Add-on** (keine `config.yaml`): Image aus der Repo-Wurzel
  (`docker build -f gateway/Dockerfile .`), eigener Release-Workflow; ohne echten Schlüssel in
  `gateway/host/release.pub` lehnt das Gate echte `gateway-v*`-Tags ab. Änderungen in
  `smartheat_core`/`smartheat_runtime` wirken auf beide Clienttypen.
- **Startfehler** melden mit stabilem Schlüssel `fehler:<key>`, der Grund steht nur im Feld `grund`. Schlüssel,
  Zertifikate, Übernahme-Code, Supervisor-Token und Inventur-Inhalte stehen nie in Log, Status-Ereignis oder
  Diagnose (Regel 6); der Übernahme-Code geht nur in die Discovery (bleibt in der HA-Instanz), ins Log kommt bei
  Fehlern nur die Fehlerart.
- **Neustart und Ende:** ein Lebenszyklus für beide Hosts (`smartheat_device/prozess.py`,
  `../docs/module/client-kern.md` „Gemeinsamer Teil der Hosts“). Falle: jede gewollte Neustart-Logzeile endet auf
  „, Laufzeit startet neu“, der E2E `gateway2` zählt sie.
- **Gemeinsamer Teil der Hosts:** ein Verhalten, das beide Hosts teilen, gehört in
  `smartheat_device/geraet_host_basis.py` bzw. `smartheat_runtime/raum.py`, nicht doppelt in die Hosts; es wird zuerst
  in den Konformitätssuiten festgehalten (`client-kern.md`, „Konformität“).
- **Endzustände sind ein Ruhezustand**, kein Exit, weil der Supervisor ein sauber beendetes Add-on nicht zuverlässig neu
  startet (`heizungsbruecke/src/heizungsbruecke/__main__.py`). Kein Last Will (`link.py`).
- **Soll-Entity (HA):** Ablauf in `ha-host.md` („Soll-Entity und Wunschtemperatur“). Änderungen dort immer mit den Tests
  `test_geraet_trigger.py`/`test_host_dokument.py` (Echo/Wunsch-Schleife ist das Hauptrisiko).
- **Lock-Datei** `heizungsbruecke/requirements.txt`: das Image installiert nur daraus (`--require-hashes`). Erneuern nur
  bei geänderten `dependencies` in `pyproject.toml` oder bewusstem Bump (dann `--upgrade` vor `--generate-hashes`, Diff
  lesen), im Ordner `heizungsbruecke/`:

  ```
  tar cf - pyproject.toml requirements.txt | docker run --rm -i \
    python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f \
    sh -c "mkdir /w && cd /w && tar xf - && pip install --quiet pip-tools >/dev/null 2>&1 \
           && pip-compile --quiet --generate-hashes --strip-extras --output-file requirements.txt pyproject.toml \
           && tar cf - requirements.txt" | tar xf -
  ```

  Danach `scripts/check.sh --only docker --full`; den Drift-Check übernimmt `scripts/ci/pin_check.py`.
