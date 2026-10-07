#!/bin/bash
# post-build-Hook (Plan G2b-2 Task 9): rpi-image-gen v2.8.0 ruft <SRCROOT>/post-build.sh nach bdebstrap und vor dem
# Image-Bau mit dem fertigen Root-Dateisystem als $1 auf (bin/runner, Phase post-build; ein Fehler bricht den Bau ab).
# Voller Lauf von rootfs_checks.sh auf dem Stand, der ins Image geht - nach dem Aufraeumen von mmdebstrap, also auch
# mit der machine-id, dazu der Nachweis, dass die USB-Startregel in der initramfs liegt (initramfs_check.sh; beide
# Pruefungen laufen immer, damit ein Bau alle Verstoesse auf einmal zeigt). Muss ausfuehrbar sein (der Runner
# uebergeht sonst den Hook nur mit einer Warnung).
set -euo pipefail
R="${1:?Root-Dateisystem fehlt}"
STAGE="${IGconf_shg_stage:?IGconf_shg_stage fehlt}"
check=(env -u SHG_ROOTFS_CHECK_UID bash "$(dirname "$0")/rootfs_checks.sh" "$R")  # Variable nur fuer Tests
if [ -f "$STAGE/pilot" ]; then check+=(--pilot); fi
status=0
"${check[@]}" || status=1
bash "$(dirname "$0")/initramfs_check.sh" "$R" || status=1
exit "$status"
