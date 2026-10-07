#!/bin/bash
# Baut das Image mit rpi-image-gen (Plan G2b-2 Task 9). Laeuft IN einem privilegierten debian:trixie-Container
# (linux/arm64, make_image.sh) mit /src (Repo, nur lesend), /stage (prepare.sh, nur lesend) und /out. rpi-image-gen
# per Commit gepinnt (v2.8.0). Aufbau laut v2.8.0: -S = Quellbaum mit config/ und layer/ (hier gateway/image), -B =
# Arbeitsordner (muss existieren), Ergebnis von deploy.sh: <B>/deploy-<IGconf_artefact_version>/<Image-Name>.img.zst.
# HOST_IDS (uid:gid, optional): Eigentuemer der Dateien in /out.
set -euo pipefail
RIG_COMMIT=262d4df5a9f9d4133370465399a7958a7c22cdc7
NAME=smartheat-gateway
version="$(cat /src/gateway/VERSION)"
[[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "gateway/VERSION ungueltig: $version" >&2; exit 1; }
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends git ca-certificates sudo zstd >/dev/null
git init -q /rig
git -C /rig fetch -q --depth 1 https://github.com/raspberrypi/rpi-image-gen.git "$RIG_COMMIT"
git -C /rig checkout -q FETCH_HEAD
[ "$(git -C /rig rev-parse HEAD)" = "$RIG_COMMIT" ] || { echo "rpi-image-gen: falscher Commit" >&2; exit 1; }
(cd /rig && ./install_deps.sh)
cp -a /src/gateway/image /work-src
suffix=""
if [ -f /stage/pilot ]; then suffix="-pilot"; fi
mkdir -p /work
(cd /rig && ./rpi-image-gen build -S /work-src -c "$NAME.yaml" -B /work -- "IGconf_artefact_version=$version")
image="/work/deploy-$version/$NAME.img.zst"
if [ ! -f "$image" ]; then
  echo "Kein Image unter $image" >&2
  ls -la "/work/deploy-$version" >&2 || true
  exit 1
fi
target="/out/$NAME-$version$suffix.img.zst"
cp "$image" "$target"
(cd /out && sha256sum "$(basename "$target")" >"$(basename "$target").sha256")
if [ -n "${HOST_IDS:-}" ]; then chown "$HOST_IDS" "$target" "$target.sha256"; fi
du -h "$target"
echo "Image: $target"
