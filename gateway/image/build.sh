#!/bin/bash
# Baut das Image mit rpi-image-gen. Laeuft IN einem privilegierten debian:trixie-Container
# (linux/arm64, make_image.sh) mit /src (Repo, nur lesend), /stage (prepare.sh, nur lesend) und /out. rpi-image-gen
# per Commit gepinnt (v2.9.0). Aufbau laut v2.9.0: -S = Quellbaum mit config/, layer/ und hooks/ (hier gateway/image),
# -B = Arbeitsordner (muss existieren). Ergebnis der Deploy-Hooks: <B>/deploy-<IGconf_artefact_version>/ mit
# <Image-Name>.img.zst, der SBOM (syft, SPDX-JSON; im Deploy-Ordner filesystem-<version>.sbom.zst, erster Bau 2026-10-27) und
# - von hooks/deploy50-slot-images - system-<version>.ext4 und boot-<version>.vfat (rohe Slot-Images fuer das System-Bundle).
# Nach /out kommen je Datei eine Kopie und eine .sha256: smartheat-gateway-<version>.img.zst, system-<version>.ext4,
# boot-<version>.vfat und smartheat-gateway-<version>.sbom[.zst].
# HOST_IDS (uid:gid, optional): Eigentuemer der Dateien in /out.
set -euo pipefail
RIG_COMMIT=d2d5705bcb37284c4b08c0ce43c039b1f1004e25
NAME=smartheat-gateway
version="$(cat /src/gateway/VERSION)"
[[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "gateway/VERSION ungueltig: $version" >&2; exit 1; }
apt-get update -qq
# gnupg: docker_apt.sh prueft den Fingerabdruck des Docker-Schluessels
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends git ca-certificates sudo zstd gnupg >/dev/null
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
deploy="/work/deploy-$version"
files=("$NAME.img.zst" "system-$version.ext4" "boot-$version.vfat")
for f in "${files[@]}"; do
  if [ ! -f "$deploy/$f" ]; then
    echo "Kein $f unter $deploy" >&2
    ls -la "$deploy" >&2 || true
    exit 1
  fi
done
# SBOM von rpi-image-gen (syft, filesystem-<version>.sbom, im Deploy-Ordner mit zstd gepackt): genau eine Datei
shopt -s nullglob
sboms=("$deploy"/*.sbom "$deploy"/*.sbom.zst)
shopt -u nullglob
if [ ${#sboms[@]} -ne 1 ]; then
  echo "SBOM: genau eine Datei *.sbom[.zst] unter $deploy erwartet, gefunden ${#sboms[@]}" >&2
  ls -la "$deploy" >&2 || true
  exit 1
fi
sbom_ext="${sboms[0]#"$deploy"/}"
sbom_ext="${sbom_ext#*.sbom}"  # leer oder .zst
out_names=("$NAME-$version$suffix.img.zst" "system-$version.ext4" "boot-$version.vfat" "$NAME-$version$suffix.sbom$sbom_ext")
sources=("$deploy/$NAME.img.zst" "$deploy/system-$version.ext4" "$deploy/boot-$version.vfat" "${sboms[0]}")
for i in "${!out_names[@]}"; do
  cp --sparse=always "${sources[$i]}" "/out/${out_names[$i]}"
  (cd /out && sha256sum "${out_names[$i]}" >"${out_names[$i]}.sha256")
  if [ -n "${HOST_IDS:-}" ]; then chown "$HOST_IDS" "/out/${out_names[$i]}" "/out/${out_names[$i]}.sha256"; fi
  du -h "/out/${out_names[$i]}"
done
echo "Image: /out/${out_names[0]}"
