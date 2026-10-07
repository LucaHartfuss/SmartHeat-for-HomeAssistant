#!/bin/bash
# post-build-Hook (Plan G2b-2 Task 9): rpi-image-gen v2.8.0 ruft <SRCROOT>/post-build.sh nach bdebstrap und vor dem
# Image-Bau mit dem fertigen Root-Dateisystem als $1 auf (bin/runner, Phase post-build; ein Fehler bricht den Bau ab).
# Voller Lauf von rootfs_checks.sh auf dem Stand, der ins Image geht - nach dem Aufraeumen von mmdebstrap, also auch
# mit der machine-id. Muss ausfuehrbar sein (der Runner uebergeht sonst den Hook nur mit einer Warnung).
set -euo pipefail
R="${1:?Root-Dateisystem fehlt}"
STAGE="${IGconf_shg_stage:?IGconf_shg_stage fehlt}"
check=(bash "$(dirname "$0")/rootfs_checks.sh" "$R")
if [ -f "$STAGE/pilot" ]; then check+=(--pilot); fi
"${check[@]}"
