#!/bin/bash
# Updater-Integrationstest: drei signierte Test-Bundles mit je einem eigenen Image als OCI-Archiv (0.9.1 gesund, 0.9.2
# gesund, 0.9.3: die Laufzeit wirft im laufenden Prozess, make_test_bundles.py; die Archive entstehen wie im Release aus
# einem Index per skopeo, eine lokale Registry ist nur ihre Quelle), ein Dateiserver als Release-Host und der
# Fake-Bootstrap fuer den Bootstrap des Geraetekerns, der Fake-Broker als Weg der Soll-Version (Konfigurationsdokument mit
# software, gesendet in der Rolle des Dienstes; die Laufzeit schreibt es nach data/software_soll.json) und der echte
# Updater (host/tests/updater_testlauf.py: smartheat_host.updater.main --once mit eigenen Einstellungen) in einem
# Debian-13-Container, der docker compose ueber den
# Docker-Socket des Hosts steuert. Geprueft: 0.9.1 -> 0.9.2 gelingt (Archiv geladen und geloescht, Start ohne Pull,
# `compose pull` ueberspringt das Image mit pull_policy: never), 0.9.3 wird zurueckgerollt (der Rueckweg startet das
# Image der vorigen Version, das noch da ist), ein manipuliertes
# Manifest (Signatur einer anderen Version) wird abgelehnt, ohne Container anzufassen. Der Docker braucht den
# containerd-Speicher wie das Geraet (docker-daemon.json): nur er loest name@<Index-Digest> aus einem geladenen Archiv auf. Die
# Meldungen des Updaters (data/software_meldungen.json) kommen als up/result mit dokument "software" am Fake-Broker an.
# Laufzeit rund 7 min: die Healthchecks (30 s) und die Gesundheitsfrist (180 s) im Rueckweg-Fall.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
IMAGE="smartheat-gateway-test:local"
VARIANT="smartheat-gateway-variant"  # ein Image je Version: <VARIANT>:<Version>
UPDATER_IMAGE="shg-updater-test:local"
PROJECT="smartheat-updtest"
NET="shg-upd-wan"
BOOT="shg-upd-bootstrap"  # Name wie in gateway/host/tests/fixtures/updater-compose.yml (SHG_DEVICE_API_URL)
BROKER="shg-upd-broker"   # Endpunkt aus der Bootstrap-Antwort, SAN des Broker-Zertifikats (gateway_lib.sh)
RESULTS="shg-upd-results" # Mitschnitt von up/result am Fake-Broker
API_URL="http://127.0.0.1:18092"
REGISTRY="shg-upd-registry"
FILES="shg-upd-files"     # Release-Host: Manifeste, Bundle-Dateien und Archive (Name wie im manifest_url)
VERSIONS=(0.9.1 0.9.2 0.9.3)
# Multi-Arch-Index-Digests (docker buildx imagetools inspect <image>, 2026-10-06).
REGISTRY_IMAGE="registry:2@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373"
DEBIAN_IMAGE="debian:trixie@sha256:913f6706df59a68922d1dd08f78c2476560a8d367897200a6005b00e5f67c2d5"
BUILDER_IMAGE="python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f"
COMPOSE_IMAGE="docker:29-cli@sha256:b1805116a6a86cc591b5d5f60a910a0715cdcc9d18d866ad68b1457ead25c35c"
MOSQ_IMAGE="eclipse-mosquitto:2@sha256:38c0da4f2ef84284d47b3b3eeea1cb3bdeabe81ee10caf0cd5c5ff61ee3ea408"
DATA="$(mktemp -d)"
PKI="$DATA/pki"  # Test-PKI, je Lauf neu (Regel 6)
# shellcheck source=tests/gateway_lib.sh
source "$HERE/gateway_lib.sh"
REFS=()  # Index-Digests der drei Archive (smartheat-gateway@<Digest>), gefuellt in Schritt 2
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
  local id ids ref version volumes=""
  for id in $(project_containers); do
    volumes+=" $(docker inspect -f '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}} {{end}}{{end}}' "$id")"
  done
  [ -f "$DATA/bundles/0.9.1/docker-compose.yml" ] && compose_bundle 0.9.1 down -v --remove-orphans >/dev/null 2>&1
  ids="$(project_containers)"
  # shellcheck disable=SC2086  # IDs und Volumes bewusst einzeln
  [ -n "$ids" ] && docker rm -f -v $ids >/dev/null 2>&1
  # shellcheck disable=SC2086
  [ -n "${volumes// /}" ] && docker volume rm $volumes >/dev/null 2>&1
  docker rm -f -v "$BOOT" "$BROKER" "$RESULTS" "$REGISTRY" "$FILES" >/dev/null 2>&1
  docker network rm "${PROJECT}_bus" >/dev/null 2>&1
  docker network rm "$NET" >/dev/null 2>&1
  for ref in "${REFS[@]}"; do docker image rm "smartheat-gateway@$ref" >/dev/null 2>&1; done
  for version in "${VERSIONS[@]}"; do docker image rm "$VARIANT:$version" "localhost:15000/smartheat-gateway:$version" >/dev/null 2>&1; done
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

# 0. Der Docker muss den containerd-Speicher haben wie das Geraet (gateway/host/docker-daemon.json): nur er legt ein
# geladenes Archiv unter name@<Index-Digest> ab; im klassischen Speicher laesst sich das Image so nicht aufloesen.
docker info --format '{{.DriverStatus}}' 2>/dev/null | grep -q 'io.containerd.snapshotter' \
  || die "Docker ohne containerd-Speicher (daemon.json: {\"features\": {\"containerd-snapshotter\": true}}); die CI schaltet ihn im Job docker ein"

# 1. Gateway-Image, Werkzeug-Image (Debian 13 mit Docker-Client, Compose-Plugin und skopeo wie auf dem Geraet bzw. im Image-Bau)
docker build --platform linux/amd64 -f "$REPO/gateway/Dockerfile" -t "$IMAGE" "$REPO" >/dev/null || die "docker build"
docker build -q -t "$UPDATER_IMAGE" - >/dev/null <<EOF || die "Updater-Image"
FROM $DEBIAN_IMAGE
RUN apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      python3 python3-cryptography docker-cli docker-compose skopeo ca-certificates >/dev/null && rm -rf /var/lib/apt/lists/*
EOF

# 2. Je Version ein eigenes Image (nur ein Label anders, die Schichten sind gleich), in die lokale Registry, von dort per
# skopeo als OCI-Archiv des ganzen Index mit unveraenderten Digests (wie gateway/image/export_images.sh)
docker run -d --name "$REGISTRY" -p 127.0.0.1:15000:5000 "$REGISTRY_IMAGE" >/dev/null || die "Registry startet nicht"
ok=0
for _ in $(seq 1 30); do curl -fsS http://127.0.0.1:15000/v2/ >/dev/null 2>&1 && { ok=1; break; }; sleep 1; done
[ "$ok" = 1 ] || die "Registry antwortet nicht"
mkdir -p "$DATA/archives"
for version in "${VERSIONS[@]}"; do
  printf 'FROM %s\nLABEL shg.test.version=%s\n' "$IMAGE" "$version" | docker build -q -t "$VARIANT:$version" - >/dev/null \
    || die "Image fuer $version"
  docker tag "$VARIANT:$version" "localhost:15000/smartheat-gateway:$version" || die "docker tag"
  docker push -q "localhost:15000/smartheat-gateway:$version" >/dev/null || die "docker push"
  digest="$(docker inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "localhost:15000/smartheat-gateway:$version" \
    | grep -m1 '^localhost:15000/smartheat-gateway@sha256:' | cut -d@ -f2)"
  [ -n "$digest" ] || die "kein Digest nach dem Push ($version)"
  docker run --rm --network host --security-opt label=disable -v "$DATA/archives:/out" "$UPDATER_IMAGE" \
    skopeo copy -q --src-tls-verify=false --all --preserve-digests \
    "docker://localhost:15000/smartheat-gateway@$digest" "oci-archive:/out/$version.oci.tar" || die "Export des Archivs ($version)"
  REFS+=("$digest")
done
[ "$(printf '%s\n' "${REFS[@]}" | sort -u | wc -l)" = 3 ] || die "die drei Images haben nicht drei verschiedene Digests"

# 3. Netz, Test-PKI, Fake-Broker und Fake-Bootstrap
docker network create "$NET" >/dev/null || die "Netz $NET"
{ make_pki "$PKI" "$BROKER" && broker_conf "$PKI"; } || die "Test-PKI"
docker run -d --name "$BROKER" --network "$NET" --security-opt label=disable --user "$PKI_USER" -v "$PKI:/pki:ro" \
  "$MOSQ_IMAGE" mosquitto -c /pki/mosquitto.conf >/dev/null || die "Fake-Broker startet nicht"
docker run -d --name "$BOOT" --network "$NET" --security-opt label=disable --user "$PKI_USER" -p 127.0.0.1:18092:8090 \
  -v "$REPO/gateway/tests:/gw-tests:ro" -v "$PKI:/pki:ro" -e PYTHONPATH=/app/src -e SHG_FAKE_CA_DIR=/pki \
  -e SHG_FAKE_MQTT_HOST="$BROKER" "$IMAGE" python /gw-tests/fake_bootstrap.py >/dev/null \
  || die "Fake-Bootstrap startet nicht"
ok=0
for _ in $(seq 1 30); do curl -fsS "$API_URL/_e2e/state" >/dev/null 2>&1 && { ok=1; break; }; sleep 1; done
[ "$ok" = 1 ] || die "Fake-Bootstrap antwortet nicht"

# 4. Datenordner wie install.sh (die Container schreiben als uid 1000, daher 777 wie im Compose-Test)
mkdir -p "$DATA/data" "$DATA/zigbee2mqtt" "$DATA/host" "$DATA/bus/mosquitto" \
         "$DATA/bus/credentials/runtime" "$DATA/bus/credentials/zigbee2mqtt" "$DATA/bundles" "$DATA/updater"
printf 'SHG_ROOT=%s\nSHG_DEVICE_API_URL=http://%s:8090\nSHG_DIAG_HOSTNAMES=runtime\nTZ=Europe/Berlin\n' \
  "$DATA" "$BOOT" >"$DATA/host/gateway.env"
# Die Test-PKI bleibt 0700/0600 (gateway_lib.sh): ihre Leser laufen als PKI_USER.
find "$DATA" -path "$PKI" -prune -o -path "$DATA/archives" -prune -o -exec chmod 777 {} +

# 5. Test-Bundles (Wegwerf-Root und -Release-Zertifikat) bauen und von einem Dateiserver ausliefern; dazu ein manipuliertes
# Manifest 0.9.4 (make_test_bundles.py: Manifest von 0.9.2 mit geaenderter Version, andere Bytes als die Signatur von 0.9.2).
run_in_image -v "$REPO/gateway:/gw:ro" -v "$DATA:$DATA" "$BUILDER_IMAGE" sh -c \
  'pip install --quiet --disable-pip-version-check --root-user-action=ignore pyyaml==6.0.3 cryptography==50.0.2 >&2 \
   && python /gw/host/tests/fixtures/make_test_bundles.py "$@"' \
  sh --archive-dir "$DATA/archives" --out "$DATA/release" --base-url "http://$FILES:8000" >"$DATA/bundles.json" \
  || die "Test-Bundles nicht gebaut"
run_in_image --user 0:0 -v "$DATA:/d" "$IMAGE" rm -rf /d/archives  # die Archive liegen jetzt in den Bundles (root-eigen)
docker run -d --name "$FILES" --network "$NET" --security-opt label=disable -v "$DATA/release:/srv:ro" \
  "$BUILDER_IMAGE" python -m http.server 8000 --directory /srv >/dev/null || die "Dateiserver startet nicht"
ok=0
for _ in $(seq 1 30); do
  docker run --rm --network "$NET" "$BUILDER_IMAGE" python -c \
    "import urllib.request; urllib.request.urlopen('http://$FILES:8000/0.9.2/manifest.json', timeout=3)" >/dev/null 2>&1 \
    && { ok=1; break; }
  sleep 1
done
[ "$ok" = 1 ] || die "Dateiserver antwortet nicht"

# 6. Erstes Bundle wie install.sh --bundle und der Erststart (firstboot.py): das Archiv laden (kein Pull), Start per Compose
docker load -i "$DATA/release/0.9.1/gateway-image.oci.tar" >/dev/null || die "docker load des Archivs 0.9.1"
image_known() { docker image inspect "smartheat-gateway@${REFS[$1]}" >/dev/null 2>&1; }  # $1 = 0 (0.9.1), 1 (0.9.2), 2 (0.9.3)
image_known 0 || die "Image von 0.9.1 nach dem Laden nicht als smartheat-gateway@<Index-Digest> bekannt"
mkdir -p "$DATA/bundles/0.9.1"
for name in docker-compose.yml mosquitto.conf manifest.json manifest.json.sig; do
  cp "$DATA/release/0.9.1/$name" "$DATA/bundles/0.9.1/"
done
printf '{"current": "0.9.1", "previous": null, "rejected": [], "in_progress": null}' >"$DATA/updater/state.json"
compose_bundle 0.9.1 up -d >/dev/null 2>&1 || die "compose up 0.9.1"
ok=0
for _ in $(seq 1 45); do
  curl -fsS "$API_URL/_e2e/state" 2>/dev/null | grep -q '"registrations": [1-9]' && { ok=1; break; }; sleep 2
done
[ "$ok" = 1 ] || die "Gateway hat sich nicht beim Fake-Bootstrap registriert"
DEVICE_ID="$(curl -fsS "$API_URL/_e2e/state" | py 'import json,sys; print(json.load(sys.stdin)["device_ids"][0])')"
[ -n "$DEVICE_ID" ] || die "Fake-Bootstrap kennt keine Geraete-ID"
CONFIG_VERSION=0
# Ergebnisse der Laufzeit am Broker mitschneiden (ab hier bis zum Ende; persistence false: nur was ab jetzt kommt).
docker run -d --name "$RESULTS" --network "$NET" --security-opt label=disable --user "$PKI_USER" -v "$PKI:/pki:ro" \
  "$MOSQ_IMAGE" \
  mosquitto_sub -h "$BROKER" -p 8883 --cafile /pki/ca.pem --cert /pki/dienst.pem --key /pki/dienst.key -q 1 \
  -t "smartheat/$DEVICE_ID/up/result" >/dev/null || die "Mitschnitt startet nicht"
echo "PASS: Archive, Bundles, Start mit 0.9.1 (Archiv geladen, kein Pull)"

# 7. Aufruf des Updaters im Werkzeug-Image (Schritt 1: Docker-Client und Compose-Plugin aus Debian 13, wie auf dem Geraet)
# Vom Kern nur, was install.sh auf das Geraet kopiert (SHIPPED_CORE: smartheat_core/__init__.py und dateien.py).
CORE="$REPO/heizungsbruecke/src/smartheat_core"
# Uhr wie auf dem Geraet: der Hoststatus-Dienst schreibt host/status.json; der Updater versucht nur mit synchroner Uhr und
# frischem Status ein Update. Der Test schreibt sie vor jedem Aufruf frisch (UHR=aus: keine Datei, "wartet_auf_zeit").
host_status() {
  if [ "${UHR:-an}" = aus ]; then rm -f "$DATA/host/status.json"; return; fi
  printf '{"netz": true, "dns": true, "zeit_synchron": true, "ts": "%s"}\n' "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)" \
    >"$DATA/host/status.json" && chmod 666 "$DATA/host/status.json"
}
updater() {
  host_status
  docker run --rm --network "$NET" --security-opt label=disable \
    -v /var/run/docker.sock:/var/run/docker.sock -v "$DATA:$DATA" -v "$REPO/gateway:/gw:ro" \
    -v "$CORE/__init__.py:/core/smartheat_core/__init__.py:ro" \
    -v "$CORE/dateien.py:/core/smartheat_core/dateien.py:ro" \
    -e PYTHONPATH=/gw/host:/gw/src:/core -e SHG_ROOT="$DATA" \
    "$UPDATER_IMAGE" python3 /gw/host/tests/updater_testlauf.py --release-roots "$DATA/release/test-roots.pem" \
    --health-host runtime --health-seconds 180 --compose-project "$PROJECT" --once
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
# Leere Konfiguration (anlage null, alle Grundfelder leer wie device_config.leer() des Servers, raumfuehler []) mit der
# Soll-Version $1 als software, gesendet als Dienst auf down/config. Anders als configs.leer() der Gateway-Tests ohne
# mindest_software (null): der Test haengt so nicht an der Version des Images. Eine leere Konfiguration wird immer
# uebernommen, ihr software geht an den Updater; das Gateway bleibt "nicht eingerichtet".
set_desired() {
  CONFIG_VERSION=$((CONFIG_VERSION + 1))
  local body ok=0
  body="$(py 'import json, sys
e = json.load(open(sys.argv[1]))[sys.argv[2]]
leer = dict.fromkeys(("anlage", "profil", "hebelsatz", "verteilsystem", "bindung", "tagestick", "abo",
                      "mindest_software"))
print(json.dumps({"schema": 1, "version": int(sys.argv[3]), **leer, "raumfuehler": [],
                  "software": {"version": sys.argv[2], **e}}))' "$DATA/bundles.json" "$1" "$CONFIG_VERSION")" \
    || die "Konfiguration fuer $1 nicht gebaut"
  dienst_pub "$NET" "$PKI" "smartheat/$DEVICE_ID/down/config" "$body" || die "Soll-Version $1 nicht gesendet"
  for _ in $(seq 1 30); do
    py 'import json,sys; sys.exit(0 if (json.load(open(sys.argv[1])) or {}).get("version") == sys.argv[2] else 1)' \
      "$DATA/data/software_soll.json" "$1" 2>/dev/null && { ok=1; break; }
    sleep 1
  done
  [ "$ok" = 1 ] || die "Soll-Version $1 nicht bei der Laufzeit angekommen"
}
# state.json pruefen: $1 = Python-Ausdruck ueber s (Zustand); eval nur fuer die festen Ausdruecke dieses Skripts.
state_is() { py 'import json, sys; s = json.load(open(sys.argv[1])); sys.exit(0 if eval(sys.argv[2]) else 1)' \
  "$DATA/updater/state.json" "$1"; }
# up/result (dokument software) am Fake-Broker: $1 Version, $2 ok|rollback, $3 Grund ("" bei ok). Die Laufzeit
# sendet die Meldungen des Updaters hoechstens alle 10 s (geraet_host.SOFTWARE_SECONDS), daher bis zu 60 s warten.
reported() {
  local ok=0
  for _ in $(seq 1 30); do
    docker logs "$RESULTS" 2>/dev/null | py '
import json, sys
version, result, reason = sys.argv[1:4]
for line in sys.stdin:
    try:
        body = json.loads(line)
    except ValueError:
        continue
    if not isinstance(body, dict) or body.get("dokument") != "software" or body.get("version") != version:
        continue
    if result == "ok" and body.get("ok") is True:
        sys.exit(0)
    if result == "rollback" and body.get("ok") is False and (body.get("error") or {}).get("grund") == reason:
        sys.exit(0)
sys.exit(1)' "$1" "$2" "$3" && { ok=1; break; }
    sleep 2
  done
  [ "$ok" = 1 ]
}
created_times() {
  local id
  for id in $(project_containers); do docker inspect -f '{{.Name}} {{.Id}} {{.Created}}' "$id"; done | sort
}

# 8. 0.9.1 -> 0.9.2: ohne synchrone Uhr passiert nichts (kein Abruf, keine Ablehnung), mit Uhr: aktualisiert
set_desired 0.9.2
UHR=aus expect_word wartet_auf_zeit 0.9.2
state_is 's["current"] == "0.9.1" and s["rejected"] == [] and s["in_progress"] is None and s.get("pending_reports", []) == []' \
  || fail "state.json ohne Uhr: $(cat "$DATA/updater/state.json")"
[ -e "$DATA/bundles/0.9.2" ] && fail "ohne synchrone Uhr wurde ein Bundle abgelegt"
image_known 1 && fail "ohne synchrone Uhr wurde ein Image geladen"
[ ! -e "$DATA/archive" ] || fail "ohne synchrone Uhr wurde ein Archiv abgerufen"
stage_done "Update ohne Uhr"
start=$SECONDS
expect_word aktualisiert 0.9.2
echo "    0.9.2: $((SECONDS - start)) s"
state_is 's["current"] == "0.9.2" and s["previous"] == "0.9.1" and s["in_progress"] is None and s["pending_reports"] == [] and s["zertifikat_untergrenze"] is not None' \
  || fail "state.json nach 0.9.2: $(cat "$DATA/updater/state.json")"
reported 0.9.2 ok "" || fail "update_result ok fuer 0.9.2 fehlt"
for name in docker-compose.yml mosquitto.conf manifest.json manifest.json.sig; do
  cmp -s "$DATA/release/0.9.2/$name" "$DATA/bundles/0.9.2/$name" || fail "Bundle 0.9.2: $name fehlt oder weicht ab"
done
# Eigenes Image aus dem Archiv: geladen, vom Dienst benutzt, Archiv geloescht, das Image der vorigen Version bleibt (Rueckweg),
# und `compose pull` fragt keine Registry nach dem Image mit pull_policy: never.
image_known 1 || fail "Image von 0.9.2 nach dem Update nicht bekannt"
image_known 0 || fail "Image von 0.9.1 (vorige Version) ist weg: der Rueckweg haette kein Image"
[ -z "$(ls -A "$DATA/archive" 2>/dev/null)" ] || fail "Archiv nach dem Laden nicht geloescht: $(ls -A "$DATA/archive")"
[ "$(docker inspect -f '{{.Config.Image}}' "$(service_container runtime)" 2>/dev/null)" = "smartheat-gateway@${REFS[1]}" ] \
  || fail "runtime laeuft nicht mit dem Image aus dem Archiv von 0.9.2"
compose_bundle 0.9.2 pull runtime >/dev/null 2>&1 || fail "compose pull ueberspringt das Image mit pull_policy: never nicht"
stage_done "Update auf 0.9.2"
echo "PASS: Update 0.9.1 -> 0.9.2 (Archiv geladen, Start ohne Pull)"

# 9. 0.9.3 (Regel-Worker wirft 10 s nach dem Start, danach 60 s Pause im Prozess): Rueckweg auf 0.9.2. Die Pause
# darf dem Updater kein gesundes Gateway zeigen.
set_desired 0.9.3
start=$SECONDS
expect_word zurueckgerollt 0.9.3
echo "    0.9.3: $((SECONDS - start)) s, Laufzeitfehler: $(wc -l <"$DATA/data/testfehler" 2>/dev/null || echo 0)"
[ -s "$DATA/data/testfehler" ] || fail "0.9.3: die Laufzeit hat nicht im laufenden Prozess geworfen"
state_is 's["current"] == "0.9.2" and s["previous"] == "0.9.1" and any(r.startswith("0.9.3@") for r in s["rejected"]) and s["in_progress"] is None' \
  || fail "state.json nach dem Rueckweg: $(cat "$DATA/updater/state.json")"
reported 0.9.3 rollback ungesund || fail "update_result rollback/ungesund fuer 0.9.3 fehlt"
runtime_id="$(service_container runtime)"
docker inspect -f '{{json .Config.Cmd}}' "$runtime_id" 2>/dev/null | grep -q runtime_main \
  || fail "Laufzeit nach dem Rueckweg nicht mit dem Befehl von 0.9.2"
ok=0
for _ in $(seq 1 40); do  # Healthcheck der Laufzeit: interval 30 s, spaetestens nach 3 min gesund
  [ "$(docker inspect -f '{{.State.Status}} {{.State.Health.Status}}' "$runtime_id" 2>/dev/null)" = "running healthy" ] \
    && { ok=1; break; }
  sleep 5
done
[ "$ok" = 1 ] || fail "Laufzeit nach dem Rueckweg nicht gesund"
compose_bundle 0.9.2 ps runtime 2>/dev/null | grep -q "(healthy)" || fail "compose ps zeigt die Laufzeit nicht gesund"
image_known 2 || fail "Image von 0.9.3 wurde nicht geladen (das Update lief gar nicht erst an)"
[ "$(docker inspect -f '{{.Config.Image}}' "$runtime_id" 2>/dev/null)" = "smartheat-gateway@${REFS[1]}" ] \
  || fail "Rueckweg: runtime laeuft nicht mit dem Image der vorigen Version"
[ -z "$(ls -A "$DATA/archive" 2>/dev/null)" ] || fail "Archiv von 0.9.3 nicht geloescht"
stage_done "Rueckweg von 0.9.3"
echo "PASS: Rueckweg von 0.9.3 (mit dem Image der vorigen Version)"

# 10. Manipuliertes Manifest 0.9.4 (Signatur von 0.9.2): abgelehnt, kein Container neu erstellt
before="$(created_times)"
set_desired 0.9.4
expect_word abgelehnt 0.9.4
after="$(created_times)"
if [ -z "$before" ] || [ "$before" != "$after" ]; then
  fail "Container nach der Ablehnung veraendert: $before / $after"
fi
printf '%s\n' "$before" | grep -q "${PROJECT}-runtime-1 " || fail "Container runtime nicht im Vergleich"
state_is 's["current"] == "0.9.2" and any(r.startswith("0.9.4@") for r in s["rejected"]) and s["in_progress"] is None' \
  || fail "state.json nach der Ablehnung: $(cat "$DATA/updater/state.json")"
reported 0.9.4 rollback signatur_ungueltig || fail "update_result signatur_ungueltig fuer 0.9.4 fehlt"
[ -e "$DATA/bundles/0.9.4" ] && fail "manipuliertes Bundle abgelegt"
stage_done "Manipulation"
echo "PASS: Updater v1 -> v2, Rueckweg von v3, Manipulation abgelehnt"
