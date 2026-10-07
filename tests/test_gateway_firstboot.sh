#!/bin/bash
# Erststart ohne Pull (Plan G2b-2 Task 7): Images werden wie im Image-Bau exportiert (gateway/image/export_images.sh),
# in einem Debian-13-Docker OHNE Netz geladen, und Compose startet einen Dienst, der das Image per Index-Digest nennt.
# Speicher des Ziel-Dockers wie auf dem Geraet: mit gateway/host/docker-daemon.json, falls vorhanden, sonst klassisch.
# SHG_TARGET_DAEMON=<datei> ueberschreibt daemon.json (fuer Vergleichslaeufe mit anderem Speicherweg).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
DEBIAN_IMAGE="debian:trixie@sha256:913f6706df59a68922d1dd08f78c2476560a8d367897200a6005b00e5f67c2d5"
MOSQ="eclipse-mosquitto:2@sha256:38c0da4f2ef84284d47b3b3eeea1cb3bdeabe81ee10caf0cd5c5ff61ee3ea408"
TEST_IMAGE="shg-firstboot-test:local"
PLATFORM="linux/$(docker version -f '{{.Server.Arch}}')"
WORK="$(mktemp -d)"
trap 'docker run --rm -v "$WORK:/w" --security-opt label=disable "$DEBIAN_IMAGE" rm -rf /w/images >/dev/null 2>&1; rm -rf "$WORK"' EXIT
printf 'FROM %s\nRUN apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends docker.io docker-cli docker-compose skopeo ca-certificates >/dev/null\n' \
  "$DEBIAN_IMAGE" >"$WORK/Dockerfile"
docker build -q -t "$TEST_IMAGE" "$WORK" >/dev/null || { echo "FAIL: Testimage"; exit 1; }
daemon="${SHG_TARGET_DAEMON:-$REPO/gateway/host/docker-daemon.json}"
if [ -f "$daemon" ]; then cp "$daemon" "$WORK/daemon.json"; else echo '{}' >"$WORK/daemon.json"; fi
printf 'services:\n  probe:\n    image: %s\n    network_mode: none\n    command: ["sleep", "120"]\n' "$MOSQ" >"$WORK/compose.yml"
# 1. Export mit Netz
docker run --rm --security-opt label=disable -v "$WORK:/work" \
  -v "$REPO/gateway/image:/img:ro" "$TEST_IMAGE" \
  bash /img/export_images.sh "$PLATFORM" /work/images "$MOSQ" || { echo "FAIL: Export"; exit 1; }
# 2. Laden und Starten ohne Netz
docker run --rm --privileged --network none --security-opt label=disable -v /var/lib/docker -v "$WORK:/work:ro" \
  "$TEST_IMAGE" bash -c '
    mkdir -p /etc/docker && cp /work/daemon.json /etc/docker/daemon.json
    (dockerd --iptables=false --ip6tables=false --bridge=none >/tmp/dockerd.log 2>&1 &)
    for _ in $(seq 60); do docker info >/dev/null 2>&1 && break; sleep 1; done
    for archive in /work/images/*.tar; do docker load -i "$archive" >/dev/null || exit 1; done
    docker compose -p probe -f /work/compose.yml up -d || exit 1
    [ "$(docker inspect -f "{{.State.Running}}" probe-probe-1)" = true ]' \
  || { echo "FAIL: Erststart ohne Pull (Ziel-Docker $(cat "$WORK/daemon.json"))"; exit 1; }
echo "PASS: Erststart ohne Pull (Ziel-Docker $(cat "$WORK/daemon.json"))"
