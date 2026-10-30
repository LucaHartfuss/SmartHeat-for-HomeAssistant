#!/usr/bin/env bash
# Lokale und CI-Pruefung (docs/ci-cd-runbook.md). Aufruf: scripts/check.sh [--full] [--only SCHRITT]
# Docker-Schritte: im Add-on laeuft "docker" nur mit --full oder --only docker. Docker wird direkt genutzt;
# ohne Docker auf dem PATH enden die Docker-Schritte mit einer Meldung.
# Ein unbekannter Schrittname bei --only bricht mit Exit 2 ab (gueltige Namen: check_only-Aufruf unten).
set -uo pipefail
cd "$(dirname "$0")/.."
FULL=0
ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --full) FULL=1 ;;
    --only) ONLY="${2:?--only braucht einen Schrittnamen}"; shift ;;
    *) echo "Unbekannte Option: $1" >&2; exit 2 ;;
  esac
  shift
done
FAILED=()
step() {
  local name=$1; shift
  if [ -n "$ONLY" ] && [ "$ONLY" != "$name" ]; then return 0; fi
  echo "=== $name ==="
  if "$@"; then echo "--- $name: OK"; else echo "--- $name: FEHLER"; FAILED+=("$name"); fi
}
# Schritt, der ohne --full nicht laeuft: die Ausgabe nennt ihn, statt ihn still auszulassen; bei --only
# (der Nutzer hat gewaehlt) bleibt es still.
SKIPPED=()
skipped() {
  if [ -n "$ONLY" ]; then return 0; fi
  echo "--- $1: uebersprungen (nur mit --full)"; SKIPPED+=("$1")
}
finish() {
  if [ ${#FAILED[@]} -eq 0 ]; then
    if [ ${#SKIPPED[@]} -eq 0 ]; then echo "=== ALLES GRUEN ==="; else echo "=== ALLES GRUEN (ohne: ${SKIPPED[*]}) ==="; fi
    exit 0
  fi
  echo "=== FEHLER in: ${FAILED[*]} ==="; exit 1
}
# Bricht ab, wenn --only keinen der uebergebenen (gueltigen) Schrittnamen nennt.
check_only() {
  if [ -z "$ONLY" ]; then return 0; fi
  local valid
  for valid in "$@"; do [ "$ONLY" = "$valid" ] && return 0; done
  echo "Unbekannter Schritt: $ONLY (gueltig: $*)" >&2; exit 2
}
# Docker-Befehl: direkt, sofern docker auf dem PATH liegt.
docker_host_run() {
  if command -v docker >/dev/null 2>&1; then "$@"
  else echo "Docker nicht verfuegbar - Schritt braucht Docker auf dem PATH"; return 1; fi
}
# Property-Tests (heizungsbruecke/tests): --full waehlt das Profil voll von hypothesis (500 Beispiele, zufaelliger
# Seed; heizungsbruecke/tests/conftest.py), sonst gilt HYPOTHESIS_PROFILE aus der Umgebung oder schnell.
if [ "$FULL" = 1 ]; then export HYPOTHESIS_PROFILE=voll; fi
PY="${PYTHON:-python3}"
# Dev-Root: in CI aus siblings.sh (DEV_ROOT), lokal der Ordner ueber dem Haupt-Checkout - auch aus einem
# Worktree unter .devtools/wt/ (git-common-dir zeigt auf .git des Haupt-Checkouts).
DEV="${DEV_ROOT:-$(cd "$(git rev-parse --git-common-dir)/../.." && pwd)}"
pyright_in() { (cd "$1" && "$PY" -m ruff check . && "$PY" -m pyright --pythonpath "$("$PY" -c 'import sys; print(sys.executable)')"); }
# Toter Code: je Paket mit seinen Ausnahmen (vulture_ausnahmen.py, jede Zeile begruendet;
# Vertragsnamen nur mit Leser auf der Gegenseite der Kopie). Kern und Gateway in einem Lauf: das
# Gateway baut auf dem Kern auf, der Kern ruft dessen Treiber- und Host-Methoden ueber Protocols, das Gateway liest
# Vertragsnamen des Kerns.
# Grenze: vulture vergleicht nur Namen. Ein gleich geschriebener Name im anderen Paket verdeckt toten Code (Beispiele
# bis Stufe 4 write_budget.QUOTA neben dem QUOTA des ViCare-Treibers); solche Faelle stehen trotzdem begruendet in
# den Ausnahmen.
vulture_in() { (cd "$1" && shift && "$PY" -m vulture "$@" --min-confidence 60); }
toter_code() {
  vulture_in heizungsbruecke src ../gateway/src vulture_ausnahmen.py \
    ../gateway/vulture_ausnahmen.py \
  && vulture_in gateway/host smartheat_host vulture_ausnahmen.py \
  && vulture_in gateway/release build_bundle.py verify_bundle.py ca.py signieren.py rauc_signieren.py rauc_bundle.py changelog_section.py \
    vulture_ausnahmen.py
}
lint() { pyright_in heizungsbruecke && pyright_in gateway && pyright_in gateway/host && pyright_in gateway/release && toter_code \
  && "$PY" scripts/ci/pin_check.py --repo "$PWD" && "$PY" scripts/ci/geheimnis_scan.py --repo "$PWD"; }
# Kern- und HA-Host-Tests (heizungsbruecke/tests), Gateway-Tests, Host-Dienste des Gateways,
# Bundle-Bau. Alle vier laufen, auch wenn einer rot ist (wie tools/check_all.sh).
tests() {
  local rc=0 teil
  for teil in heizungsbruecke gateway gateway/host gateway/release; do
    (cd "$teil" && "$PY" -m pytest -q) || rc=1
  done
  return $rc
}
# Das eigene Repo ist dieser Checkout (auch ein Worktree), die Nachbarn kommen aus dem Dev-Root.
contract() { "$PY" "$DEV/tools/contract_check.py" --addon "$PWD"; }
docker_tests() {
  local rc=0 script
  for script in test_heizungsbruecke_docker_build.sh test_heizungsbruecke_reproducible.sh \
                test_gateway_docker_build.sh test_gateway_install.sh test_gateway_updater.sh \
                test_gateway_firstboot.sh test_gateway_kette_trixie.sh test_gateway_rauc.sh test_image_scan.sh; do
    echo "--- tests/$script"
    docker_host_run bash "$PWD/tests/$script" || rc=1
  done
  return $rc
}
check_only lint test contract docker
step lint lint
step test tests
step contract contract
if [ "$FULL" = 1 ] || [ "$ONLY" = docker ]; then step docker docker_tests; else skipped docker; fi
finish
