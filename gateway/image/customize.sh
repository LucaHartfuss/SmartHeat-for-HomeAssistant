#!/bin/bash
# customize-Hook des Layers smartheat-gateway (Plan G2b-2 Task 9). $1 = Root-Dateisystem, $2 = Stage von prepare.sh.
# Legt den Installer-Baum, die Container-Image-Archive und das signierte Bundle ins Image, fuehrt install.sh --image im
# chroot aus und prueft das Ergebnis (rootfs_checks.sh --customize; der volle Lauf folgt in post-build.sh, nachdem
# mmdebstrap aufgeraeumt hat). Keine Netzzugriffe aus dem chroot noetig (Pakete kommen aus dem Layer). Der Layer steht
# im Plan von rpi-image-gen v2.8.0 hinter openssh-server: dessen Hook (ssh aktivieren, Hostschluessel loeschen) ist
# hier schon gelaufen, install.sh --image schaltet ssh im Serien-Image wieder ab.
set -euo pipefail
R=$1 STAGE=$2
mkdir -p "$R/opt/smartheat" "$R/var/lib/smartheat/images" "$R/tmp/shg-bundle"
rm -rf "$R/opt/smartheat/installer" && cp -a "$STAGE/installer" "$R/opt/smartheat/installer"
cp -a "$STAGE/bundle/." "$R/tmp/shg-bundle/"
cp -a "$STAGE/images/." "$R/var/lib/smartheat/images/"
mapfile -t args <"$STAGE/install.args"
chroot "$R" bash /opt/smartheat/installer/gateway/host/install.sh --image "${args[@]}" --bundle /tmp/shg-bundle
rm -rf "$R/tmp/shg-bundle"
check=(bash "$R/opt/smartheat/installer/gateway/image/rootfs_checks.sh" "$R" --customize)
if [ -f "$STAGE/pilot" ]; then check+=(--pilot); fi
"${check[@]}"
