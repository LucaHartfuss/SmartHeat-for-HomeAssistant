#!/bin/bash
# Zwei Builds ohne Cache liefern dieselbe Paketliste, und sie entspricht der Lock-Datei (TP12e, AU-018).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ADDON_DIR="$HERE/../heizungsbruecke"
FAIL=0
# Aufraeumen auch bei Abbruch (z. B. wenn der zweite Build scheitert): nur die eigenen Tags.
trap 'docker rmi "heizungsbruecke-repro:a" "heizungsbruecke-repro:b" >/dev/null 2>&1' EXIT

normalize() { tr 'A-Z_' 'a-z-' | sort; }

for tag in a b; do
  build_log=$(docker build --no-cache --platform linux/amd64 -t "heizungsbruecke-repro:$tag" "$ADDON_DIR" 2>&1) \
    || { echo "$build_log" | tail -30; echo "FAIL: docker build $tag"; exit 1; }
done
# pip freeze blendet pip/setuptools/wheel selbst aus; sie werden hier trotzdem ausdruecklich gefiltert,
# damit die Pruefung nicht vom Verhalten einer bestimmten pip-Version abhaengt.
freeze() {
  docker run --rm --entrypoint pip "heizungsbruecke-repro:$1" freeze | grep -E '^[A-Za-z0-9._-]+==' \
    | grep -viE '^(pip|setuptools|wheel)==' | normalize
}
list_a=$(freeze a)
list_b=$(freeze b)
locked=$(grep -E '^[A-Za-z0-9._-]+==' "$ADDON_DIR/requirements.txt" | sed 's/ .*//' | normalize)

# Gegen einen Scheinerfolg: leere Listen (z. B. pip freeze scheitert) duerfen nicht "gleich" sein.
if [ -z "$list_a" ] || [ -z "$locked" ]; then
  echo "FAIL: leere Paketliste (installiert: '$(echo "$list_a" | wc -l)' Zeilen, Lock-Datei: '$(echo "$locked" | wc -l)' Zeilen)"
  exit 1
fi
if [ "$list_a" = "$list_b" ]; then echo "PASS: zwei Builds, dieselbe Paketliste"; else echo "FAIL: Paketlisten verschieden"; diff <(echo "$list_a") <(echo "$list_b"); FAIL=1; fi
# Genau die gelockten Pakete, nicht mehr und nicht weniger (jeweils samt Version).
if [ "$list_a" = "$locked" ]; then
  echo "PASS: installierte Pakete entsprechen der Lock-Datei exakt ($(echo "$locked" | wc -l) Pakete)"
else
  echo "FAIL: installierte Pakete weichen von requirements.txt ab (< installiert, > Lock-Datei)"
  diff <(echo "$list_a") <(echo "$locked"); FAIL=1
fi
exit $FAIL
