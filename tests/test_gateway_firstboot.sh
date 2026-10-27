#!/bin/bash
# Erststart ohne Pull: Images werden wie im Image-Bau exportiert (gateway/image/export_images.sh, danach als
# <SHA-256>.tar benannt wie in prepare.sh) und in einem Debian-13-Docker OHNE Netz vom echten Erststart
# (smartheat_host.firstboot start) geladen: je Archiv einmal (Merker host/geladen/<SHA-256>), die Archive bleiben liegen,
# und Compose startet einen Dienst, der das Image per Index-Digest nennt. Ein zweiter Start laedt nichts mehr und startet
# das Bundle trotzdem wieder (GPC31). Speicher des Ziel-Dockers wie auf dem Geraet: mit gateway/host/docker-daemon.json, falls
# vorhanden, sonst klassisch. SHG_TARGET_DAEMON=<datei> ueberschreibt daemon.json (fuer Vergleichslaeufe mit anderem Speicherweg).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
DEBIAN_IMAGE="debian:trixie@sha256:913f6706df59a68922d1dd08f78c2476560a8d367897200a6005b00e5f67c2d5"
MOSQ="eclipse-mosquitto:2@sha256:38c0da4f2ef84284d47b3b3eeea1cb3bdeabe81ee10caf0cd5c5ff61ee3ea408"
TEST_IMAGE="shg-firstboot-test:local"
PLATFORM="linux/$(docker version -f '{{.Server.Arch}}')"
WORK="$(mktemp -d)"
trap 'docker run --rm -v "$WORK:/w" --security-opt label=disable "$DEBIAN_IMAGE" rm -rf /w/images >/dev/null 2>&1; rm -rf "$WORK"' EXIT
printf 'FROM %s\nRUN apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends docker.io docker-cli docker-compose skopeo ca-certificates python3 python3-cryptography >/dev/null\n' \
  "$DEBIAN_IMAGE" >"$WORK/Dockerfile"
docker build -q -t "$TEST_IMAGE" "$WORK" >/dev/null || { echo "FAIL: Testimage"; exit 1; }
daemon="${SHG_TARGET_DAEMON:-$REPO/gateway/host/docker-daemon.json}"
if [ -f "$daemon" ]; then cp "$daemon" "$WORK/daemon.json"; else echo '{}' >"$WORK/daemon.json"; fi
printf 'services:\n  probe:\n    image: %s\n    network_mode: none\n    command: ["sleep", "120"]\n' "$MOSQ" >"$WORK/compose.yml"
# 1. Export mit Netz, Namen nach der SHA-256 des Inhalts (wie prepare.sh)
docker run --rm --security-opt label=disable -v "$WORK:/work" \
  -v "$REPO/gateway/image:/img:ro" "$TEST_IMAGE" \
  bash -c 'bash /img/export_images.sh "$1" /work/images "$2" && for f in /work/images/[0-9][0-9].tar; do
             sum="$(sha256sum "$f")"; mv "$f" "/work/images/${sum%% *}.tar"; done' _ "$PLATFORM" "$MOSQ" \
  || { echo "FAIL: Export"; exit 1; }
# 2. Erststart ohne Netz: Werkseinstellung wie im Image (nur die Archive), Zustand wie nach firstboot.anlegen
docker run --rm --privileged --network none --security-opt label=disable -v /var/lib/docker -v "$WORK:/work:ro" \
  -v "$REPO:/repo:ro" -e PYTHONPATH=/repo/gateway/host:/repo/gateway/src:/repo/heizungsbruecke/src \
  -e SHG_ROOT=/var/lib/smartheat -e PYTHONDONTWRITEBYTECODE=1 "$TEST_IMAGE" bash -c '
    mkdir -p /etc/docker && cp /work/daemon.json /etc/docker/daemon.json
    (dockerd --iptables=false --ip6tables=false --bridge=none >/tmp/dockerd.log 2>&1 &)
    for _ in $(seq 60); do docker info >/dev/null 2>&1 && break; sleep 1; done
    mkdir -p /opt/smartheat/werkseinstellung /var/lib/smartheat/bundles/0.0.1 /var/lib/smartheat/updater
    cp -r /work/images /opt/smartheat/werkseinstellung/images
    printf "SHG_ROOT=/var/lib/smartheat\n" >/opt/smartheat/gateway.env
    cp /work/compose.yml /var/lib/smartheat/bundles/0.0.1/docker-compose.yml
    : >/var/lib/smartheat/bundles/0.0.1/mosquitto.conf
    printf "{\"current\": \"0.0.1\", \"in_progress\": null, \"previous\": null, \"rejected\": []}" >/var/lib/smartheat/updater/state.json
    archives="$(ls /opt/smartheat/werkseinstellung/images | wc -l)"
    [ "$archives" -ge 1 ] || { echo "kein Archiv exportiert"; exit 1; }
    python3 -m smartheat_host.firstboot start >/tmp/start1.log 2>&1 || { cat /tmp/start1.log; exit 1; }
    [ "$(grep -c "Image geladen" /tmp/start1.log)" = "$archives" ] || { cat /tmp/start1.log; echo "nicht jedes Archiv geladen"; exit 1; }
    [ "$(docker inspect -f "{{.State.Running}}" smartheat-probe-1)" = true ] || { cat /tmp/start1.log; echo "Dienst laeuft nicht"; exit 1; }
    for f in /opt/smartheat/werkseinstellung/images/*.tar; do
      [ -e "/var/lib/smartheat/host/geladen/$(basename "$f" .tar)" ] || { echo "Merker fehlt fuer $f"; exit 1; }
    done
    # zweiter Start: nichts mehr laden, die Archive bleiben, das Bundle wird wieder gestartet (Dienst vorher anhalten)
    docker stop smartheat-probe-1 >/dev/null
    python3 -m smartheat_host.firstboot start >/tmp/start2.log 2>&1 || { cat /tmp/start2.log; exit 1; }
    grep -q "Image geladen" /tmp/start2.log && { cat /tmp/start2.log; echo "zweiter Start laedt erneut"; exit 1; }
    [ "$(ls /opt/smartheat/werkseinstellung/images | wc -l)" = "$archives" ] || { echo "Archive nicht mehr alle da"; exit 1; }
    [ "$(docker inspect -f "{{.State.Running}}" smartheat-probe-1)" = true ] || { cat /tmp/start2.log; echo "zweiter Start startet das Bundle nicht"; exit 1; }' \
  || { echo "FAIL: Erststart ohne Pull (Ziel-Docker $(cat "$WORK/daemon.json"))"; exit 1; }
echo "PASS: Erststart ohne Pull (Ziel-Docker $(cat "$WORK/daemon.json"))"
