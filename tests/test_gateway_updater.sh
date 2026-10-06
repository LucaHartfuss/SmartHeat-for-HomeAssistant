#!/bin/bash
# Updater-Integrationstest (Spec G2b-1 10, Plan G2b-1 Task 15): echte lokale Registry, drei signierte Test-Bundles
# (0.9.1 gesund, 0.9.2 gesund, 0.9.3 kaputt), die Fake-Geraete-API als Release-Host und Soll-Versions-Quelle und der
# echte Updater (python3 -m smartheat_host.updater --once) in einem Debian-13-Container, der docker compose ueber den
# Docker-Socket des Hosts steuert. Geprueft: 0.9.1 -> 0.9.2 gelingt, 0.9.3 wird zurueckgerollt, ein manipuliertes
# Manifest (Signatur einer anderen Version) wird abgelehnt, ohne Container anzufassen (Review Focus 1 und 2).
# Laufzeit rund 6 min: die Healthchecks (30 s/60 s) und die Gesundheitsfrist (180 s) im Rueckweg-Fall.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
IMAGE="smartheat-gateway-test:local"
UPDATER_IMAGE="shg-updater-test:local"
PROJECT="smartheat-updtest"
NET="shg-upd-wan"
API="shg-upd-api"
API_URL="http://127.0.0.1:18092"
REGISTRY="shg-upd-registry"
PUSHED="localhost:15000/smartheat-gateway:test"
# Multi-Arch-Index-Digests (docker buildx imagetools inspect <image>, 2026-10-06).
REGISTRY_IMAGE="registry:2@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373"
DEBIAN_IMAGE="debian:trixie@sha256:913f6706df59a68922d1dd08f78c2476560a8d367897200a6005b00e5f67c2d5"
BUILDER_IMAGE="python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f"
COMPOSE_IMAGE="docker:29-cli@sha256:b1805116a6a86cc591b5d5f60a910a0715cdcc9d18d866ad68b1457ead25c35c"
DATA="$(mktemp -d)"
REF=""
FAIL=0
fail() { echo "FAIL: $1"; FAIL=1; }
# label=disable: auf SELinux-Hosts (Fedora) duerfen Container die Bind-Mounts sonst nicht lesen (wie tools/e2e).
run_in_image() { docker run --rm --security-opt label=disable "$@"; }

if docker compose version >/dev/null 2>&1; then COMPOSE=(docker compose)
else COMPOSE=(docker run --rm --security-opt label=disable -v /var/run/docker.sock:/var/run/docker.sock
              -v "$DATA:$DATA" "$COMPOSE_IMAGE" docker compose); fi
# Compose wie der Updater (bundles.ComposeRunner): gleicher Projektname, gleiche env-Datei, Bundle-Pfad $1.
compose_bundle() {
  local version=$1; shift
  "${COMPOSE[@]}" -p "$PROJECT" --env-file "$DATA/host/gateway.env" -f "$DATA/bundles/$version/docker-compose.yml" "$@"
}
project_containers() { docker ps -aq --filter "label=com.docker.compose.project=$PROJECT"; }
service_container() {
  docker ps -aq --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=$1"
}
py() { python3 -c "$@"; }

# Die Container legen Dateien als uid 1000 bzw. root an (device/, Bundles, state.json); ein Skript unter anderer uid
# kann sie nicht loeschen. Deshalb raeumt ein Wegwerf-Container als root.
# Anonyme Volumes (Mosquitto: /mosquitto/data, /mosquitto/log) werden vorher eingesammelt: ein von Compose 2.26
# (Updater) neu erstelltes Volume entfernt das neuere Compose des Testskripts bei "down -v" nicht.
cleanup() {
  local id ids volumes=""
  for id in $(project_containers); do
    volumes+=" $(docker inspect -f '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}} {{end}}{{end}}' "$id")"
  done
  [ -f "$DATA/bundles/0.9.1/docker-compose.yml" ] && compose_bundle 0.9.1 down -v --remove-orphans >/dev/null 2>&1
  ids="$(project_containers)"
  # shellcheck disable=SC2086  # IDs und Volumes bewusst einzeln
  [ -n "$ids" ] && docker rm -f -v $ids >/dev/null 2>&1
  # shellcheck disable=SC2086
  [ -n "${volumes// /}" ] && docker volume rm $volumes >/dev/null 2>&1
  docker rm -f -v "$API" "$REGISTRY" >/dev/null 2>&1
  docker network rm "${PROJECT}_bus" >/dev/null 2>&1
  docker network rm "$NET" >/dev/null 2>&1
  docker image rm "$PUSHED" >/dev/null 2>&1
  [ -n "$REF" ] && docker image rm "$REF" >/dev/null 2>&1
  run_in_image --user 0:0 -v "$DATA:/d" "$IMAGE" sh -c 'find /d -mindepth 1 -delete' >/dev/null 2>&1
  rm -rf "$DATA"
}
trap cleanup EXIT
# Abbruch einer Stufe mit Logs (die folgenden Stufen bauen aufeinander auf).
die() {
  echo "FAIL: $1"
  [ -f "$DATA/bundles/0.9.1/docker-compose.yml" ] && compose_bundle 0.9.1 logs --no-color 2>/dev/null | tail -n 80
  exit 1
}
stage_done() { [ "$FAIL" = 0 ] || die "$1"; }

# 1. Gateway-Image
docker build --platform linux/amd64 -f "$REPO/gateway/Dockerfile" -t "$IMAGE" "$REPO" >/dev/null || die "docker build"

# 2. Registry, Image mit Digest
docker run -d --name "$REGISTRY" -p 127.0.0.1:15000:5000 "$REGISTRY_IMAGE" >/dev/null || die "Registry startet nicht"
ok=0
for _ in $(seq 1 30); do curl -fsS http://127.0.0.1:15000/v2/ >/dev/null 2>&1 && { ok=1; break; }; sleep 1; done
[ "$ok" = 1 ] || die "Registry antwortet nicht"
docker tag "$IMAGE" "$PUSHED" || die "docker tag"
docker push -q "$PUSHED" >/dev/null || die "docker push"
REF="$(docker inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$PUSHED" \
  | grep -m1 '^localhost:15000/smartheat-gateway@sha256:')"
[ -n "$REF" ] || die "kein Digest nach dem Push"

# 3. Netz und Fake-Geraete-API (zugleich Release-Host: /_e2e/files)
docker network create "$NET" >/dev/null || die "Netz $NET"
docker run -d --name "$API" --network "$NET" --security-opt label=disable -p 127.0.0.1:18092:8090 \
  -v "$REPO/gateway/tests:/gw-tests:ro" -e PYTHONPATH=/app/src "$IMAGE" python /gw-tests/fake_device_api.py >/dev/null \
  || die "Fake-Geraete-API startet nicht"
ok=0
for _ in $(seq 1 30); do curl -fsS "$API_URL/_e2e/state" >/dev/null 2>&1 && { ok=1; break; }; sleep 1; done
[ "$ok" = 1 ] || die "Fake-Geraete-API antwortet nicht"

# 4. Datenordner wie install.sh (die Container schreiben als uid 1000, daher 777 wie im Compose-Test)
mkdir -p "$DATA/data" "$DATA/zigbee2mqtt" "$DATA/host" "$DATA/bus/mosquitto" "$DATA/bus/credentials/agent" \
         "$DATA/bus/credentials/runtime" "$DATA/bus/credentials/zigbee2mqtt" "$DATA/bundles" "$DATA/updater"
printf 'SHG_ROOT=%s\nSHG_DEVICE_API_URL=http://%s:8090\nSHG_DIAG_HOSTNAMES=agent\nTZ=Europe/Berlin\n' \
  "$DATA" "$API" >"$DATA/host/gateway.env"
chmod -R 777 "$DATA"

# 5. Test-Bundles (Wegwerf-Schluessel) bauen und bei der Fake-API ablegen; dazu ein manipuliertes Manifest 0.9.4
# (Manifest von 0.9.2 mit geaenderter Version, also andere Bytes als die Signatur von 0.9.2).
run_in_image -v "$REPO/gateway:/gw:ro" -v "$DATA:$DATA" "$BUILDER_IMAGE" sh -c \
  'pip install --quiet --disable-pip-version-check --root-user-action=ignore pyyaml==6.0.3 cryptography==50.0.2 >&2 \
   && python /gw/host/tests/fixtures/make_test_bundles.py "$@"' \
  sh --image "$REF" --out "$DATA/release" --base-url "http://$API:8090" >"$DATA/bundles.json" \
  || die "Test-Bundles nicht gebaut"
py '
import base64, hashlib, json, sys
from pathlib import Path
release, answer_path = Path(sys.argv[1]), Path(sys.argv[2])
answer = json.loads(answer_path.read_text())
files = {f"{v}/{n}": (release / v / n).read_bytes()
         for v in answer for n in ("docker-compose.yml", "mosquitto.conf", "manifest.json")}
tampered = files["0.9.2/manifest.json"].replace(b"\"version\": \"0.9.2\"", b"\"version\": \"0.9.4\"")
assert tampered != files["0.9.2/manifest.json"]
files["0.9.4/manifest.json"] = tampered
answer["0.9.4"] = {"manifest_url": answer["0.9.2"]["manifest_url"].replace("/0.9.2/", "/0.9.4/"),
                   "manifest_sha256": hashlib.sha256(tampered).hexdigest(), "signature": answer["0.9.2"]["signature"]}
answer_path.write_text(json.dumps(answer))
print(json.dumps({name: base64.b64encode(data).decode() for name, data in files.items()}))
' "$DATA/release" "$DATA/bundles.json" \
  | curl -fsS -X POST -H 'Content-Type: application/json' --data-binary @- "$API_URL/_e2e/files" >/dev/null \
  || die "Bundle-Dateien nicht bei der Fake-API abgelegt"
curl -fsS "$API_URL/_e2e/files/0.9.2/docker-compose.yml" | cmp -s - "$DATA/release/0.9.2/docker-compose.yml" \
  || die "Fake-API liefert die Bundle-Dateien nicht aus"

# 6. Erstes Bundle wie install.sh --bundle, Start per Compose
mkdir -p "$DATA/bundles/0.9.1"
for name in docker-compose.yml mosquitto.conf manifest.json manifest.json.minisig; do
  cp "$DATA/release/0.9.1/$name" "$DATA/bundles/0.9.1/"
done
printf '{"current": "0.9.1", "previous": null, "rejected": [], "in_progress": null}' >"$DATA/updater/state.json"
compose_bundle 0.9.1 up -d >/dev/null 2>&1 || die "compose up 0.9.1"
ok=0
for _ in $(seq 1 45); do
  curl -fsS "$API_URL/_e2e/state" 2>/dev/null | grep -q '"registrations": [1-9]' && { ok=1; break; }; sleep 2
done
[ "$ok" = 1 ] || die "Agent hat sich nicht bei der Fake-Geraete-API registriert"
echo "PASS: Registry, Bundles, Start mit 0.9.1"

# 7. Updater-Image (nur Docker-Client und Compose-Plugin aus Debian 13, wie auf dem Geraet) und Aufruf
docker build -q -t "$UPDATER_IMAGE" - >/dev/null <<EOF || die "Updater-Image"
FROM $DEBIAN_IMAGE
RUN apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      python3 python3-cryptography docker-cli docker-compose >/dev/null && rm -rf /var/lib/apt/lists/*
EOF
updater() {
  docker run --rm --network "$NET" --security-opt label=disable \
    -v /var/run/docker.sock:/var/run/docker.sock -v "$DATA:$DATA" -v "$REPO/gateway:/gw:ro" \
    -e PYTHONPATH=/gw/host:/gw/src -e SHG_ROOT="$DATA" -e SHG_DEVICE_API_URL="http://$API:8090" \
    -e SHG_UPDATER_ALLOW_HTTP=1 -e SHG_UPDATER_HEALTH_URL=http://agent:8080/healthz -e SHG_UPDATER_HEALTH_SECONDS=180 \
    -e SHG_COMPOSE_PROJECT="$PROJECT" -e SHG_RELEASE_PUB="$DATA/release/test.pub" \
    "$UPDATER_IMAGE" python3 -m smartheat_host.updater --once
}
expect_word() {  # $1 erwartetes Ergebnis, $2 Version; Log des Updaters (stderr) nur bei Abweichung
  # Ergebniswort nur aus stdout: docker run kopiert stdout und stderr getrennt, ihre Reihenfolge ist nicht garantiert.
  local word
  word="$(updater 2>"$DATA/updater.log" | tail -n 1)"
  if [ "$word" != "$1" ]; then
    tail -n 40 "$DATA/updater.log"
    fail "Updater fuer $2: '$word' statt '$1'"
  fi
}
set_desired() {  # Soll-Version $1 mit den Werten aus bundles.json
  py 'import json, sys; e = json.load(open(sys.argv[1]))[sys.argv[2]]; print(json.dumps({"version": sys.argv[2], **e}))' \
    "$DATA/bundles.json" "$1" \
    | curl -fsS -X POST -H 'Content-Type: application/json' --data-binary @- "$API_URL/_e2e/desired" >/dev/null \
    || die "Soll-Version $1 nicht gesetzt"
}
# state.json pruefen: $1 = Python-Ausdruck ueber s (Zustand); eval nur fuer die festen Ausdruecke dieses Skripts.
state_is() { py 'import json, sys; s = json.load(open(sys.argv[1])); sys.exit(0 if eval(sys.argv[2]) else 1)' \
  "$DATA/updater/state.json" "$1"; }
# Meldung an die Fake-API: $1 Version, $2 Ergebnis, $3 Grund
reported() {
  curl -fsS "$API_URL/_e2e/state" | py '
import json, sys
results = json.load(sys.stdin)["update_results"]
sys.exit(0 if any(r.get("version") == sys.argv[1] and r.get("result") == sys.argv[2] and r.get("reason") == sys.argv[3]
                  for r in results) else 1)' "$1" "$2" "$3"
}
created_times() {
  local id
  for id in $(project_containers); do docker inspect -f '{{.Name}} {{.Id}} {{.Created}}' "$id"; done | sort
}

# 8. 0.9.1 -> 0.9.2: aktualisiert
set_desired 0.9.2
start=$SECONDS
expect_word aktualisiert 0.9.2
echo "    0.9.2: $((SECONDS - start)) s"
state_is 's["current"] == "0.9.2" and s["previous"] == "0.9.1" and s["in_progress"] is None and s["pending_reports"] == []' \
  || fail "state.json nach 0.9.2: $(cat "$DATA/updater/state.json")"
reported 0.9.2 ok "" || fail "update_result ok fuer 0.9.2 fehlt"
for name in docker-compose.yml mosquitto.conf manifest.json manifest.json.minisig; do  # Spec 2.2
  cmp -s "$DATA/release/0.9.2/$name" "$DATA/bundles/0.9.2/$name" || fail "Bundle 0.9.2: $name fehlt oder weicht ab"
done
stage_done "Update auf 0.9.2"
echo "PASS: Update 0.9.1 -> 0.9.2"

# 9. 0.9.3 (Laufzeit beendet sich sofort): Rueckweg auf 0.9.2
set_desired 0.9.3
start=$SECONDS
expect_word zurueckgerollt 0.9.3
echo "    0.9.3: $((SECONDS - start)) s"
state_is 's["current"] == "0.9.2" and s["previous"] == "0.9.1" and "0.9.3" in s["rejected"] and s["in_progress"] is None' \
  || fail "state.json nach dem Rueckweg: $(cat "$DATA/updater/state.json")"
reported 0.9.3 rollback ungesund || fail "update_result rollback/ungesund fuer 0.9.3 fehlt"
runtime_id="$(service_container runtime)"
docker inspect -f '{{json .Config.Cmd}}' "$runtime_id" 2>/dev/null | grep -q runtime_main \
  || fail "Laufzeit nach dem Rueckweg nicht mit dem Befehl von 0.9.2"
ok=0
for _ in $(seq 1 40); do  # Healthcheck der Laufzeit: interval 60 s, spaetestens nach 3 min gesund
  [ "$(docker inspect -f '{{.State.Status}} {{.State.Health.Status}}' "$runtime_id" 2>/dev/null)" = "running healthy" ] \
    && { ok=1; break; }
  sleep 5
done
[ "$ok" = 1 ] || fail "Laufzeit nach dem Rueckweg nicht gesund"
compose_bundle 0.9.2 ps runtime 2>/dev/null | grep -q "(healthy)" || fail "compose ps zeigt die Laufzeit nicht gesund"
stage_done "Rueckweg von 0.9.3"
echo "PASS: Rueckweg von 0.9.3"

# 10. Manipuliertes Manifest 0.9.4 (Signatur von 0.9.2): abgelehnt, kein Container neu erstellt
before="$(created_times)"
set_desired 0.9.4
expect_word abgelehnt 0.9.4
after="$(created_times)"
if [ -z "$before" ] || [ "$before" != "$after" ]; then
  fail "Container nach der Ablehnung veraendert: $before / $after"
fi
printf '%s\n' "$before" | grep -q "${PROJECT}-agent-1 " || fail "Agent-Container nicht im Vergleich"
state_is 's["current"] == "0.9.2" and "0.9.4" in s["rejected"] and s["in_progress"] is None' \
  || fail "state.json nach der Ablehnung: $(cat "$DATA/updater/state.json")"
reported 0.9.4 rollback signatur_ungueltig || fail "update_result signatur_ungueltig fuer 0.9.4 fehlt"
[ -e "$DATA/bundles/0.9.4" ] && fail "manipuliertes Bundle abgelegt"
stage_done "Manipulation"
echo "PASS: Updater v1 -> v2, Rueckweg von v3, Manipulation abgelehnt"
