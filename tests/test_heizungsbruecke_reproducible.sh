#!/bin/bash
# Zwei Builds ohne Cache liefern dieselbe Paketliste, und sie entspricht der Lock-Datei (TP12e, AU-018).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ADDON_DIR="$HERE/../heizungsbruecke"
FAIL=0
for tag in a b; do
  docker build --no-cache --platform linux/amd64 -t "heizungsbruecke-repro:$tag" "$ADDON_DIR" >/dev/null \
    || { echo "FAIL: docker build $tag"; exit 1; }
done
list_a=$(docker run --rm --entrypoint pip heizungsbruecke-repro:a freeze | sort)
list_b=$(docker run --rm --entrypoint pip heizungsbruecke-repro:b freeze | sort)
if [ "$list_a" = "$list_b" ]; then echo "PASS: zwei Builds, dieselbe Paketliste"; else echo "FAIL: Paketlisten verschieden"; diff <(echo "$list_a") <(echo "$list_b"); FAIL=1; fi
locked=$(grep -E '^[A-Za-z0-9._-]+==' "$ADDON_DIR/requirements.txt" | sed 's/ .*//; s/\\$//' | tr 'A-Z_' 'a-z-' | sort)
installed=$(echo "$list_a" | tr 'A-Z_' 'a-z-' | grep -E '^(paho-mqtt|requests|websocket-client|urllib3|certifi|idna|charset-normalizer)==' | sort)
for entry in $installed; do
  echo "$locked" | grep -qx "$entry" || { echo "FAIL: $entry steht nicht in requirements.txt"; FAIL=1; }
done
docker rmi "heizungsbruecke-repro:a" "heizungsbruecke-repro:b" >/dev/null 2>&1
exit $FAIL
