#!/bin/bash
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
OVERALL=0

bash "$HERE/test_run_sh.sh" || OVERALL=1
bash "$HERE/test_docker_build.sh" || OVERALL=1
bash "$HERE/test_heizungsbruecke_pytest.sh" || OVERALL=1
bash "$HERE/test_heizungsbruecke_docker_build.sh" || OVERALL=1
bash "$HERE/test_heizungsbruecke_happy_path.sh" || OVERALL=1

if [ "$OVERALL" = "0" ]; then
  echo "=== ALLE TESTS BESTANDEN ==="
else
  echo "=== MINDESTENS EIN TEST FEHLGESCHLAGEN ==="
fi
exit $OVERALL
