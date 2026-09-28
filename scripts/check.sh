#!/usr/bin/env bash
# Lokale und CI-Pruefung (docs/ci-cd-runbook.md). Aufruf: scripts/check.sh [--full] [--only SCHRITT]
# Ohne --full laufen Docker-Schritte nur, wenn docker direkt verfuegbar ist (CI); lokal in der
# VS-Code-Sandbox laufen sie mit --full ueber flatpak-spawn --host.
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
finish() {
  if [ ${#FAILED[@]} -eq 0 ]; then echo "=== ALLES GRUEN ==="; exit 0; fi
  echo "=== FEHLER in: ${FAILED[*]} ==="; exit 1
}
# Docker-Befehl: direkt (CI) oder ueber den Host (Sandbox, nur mit --full).
docker_host_run() {
  if command -v docker >/dev/null 2>&1; then "$@"
  elif [ "$FULL" = 1 ] && command -v flatpak-spawn >/dev/null 2>&1; then flatpak-spawn --host "$@"
  else echo "Docker nicht verfuegbar - Schritt nur mit --full (Sandbox) oder in CI"; return 1; fi
}
PY="${PYTHON:-python3}"
DEV="${DEV_ROOT:-$(cd .. && pwd)}"
lint() { (cd heizungsbruecke && "$PY" -m ruff check . && "$PY" -m pyright --pythonpath "$("$PY" -c 'import sys; print(sys.executable)')"); }
tests() { (cd heizungsbruecke && "$PY" -m pytest -q); }
contract() { "$PY" "$DEV/tools/contract_check.py"; }
docker_tests() {
  local rc=0 script
  for script in test_run_sh.sh test_docker_build.sh test_heizungsbruecke_docker_build.sh \
                test_heizungsbruecke_happy_path.sh test_mosquitto_will_acl.sh; do
    echo "--- tests/$script"
    docker_host_run bash "$PWD/tests/$script" || rc=1
  done
  return $rc
}
step lint lint
step test tests
step contract contract
if [ "$FULL" = 1 ] || [ "$ONLY" = docker ]; then step docker docker_tests; fi
finish
