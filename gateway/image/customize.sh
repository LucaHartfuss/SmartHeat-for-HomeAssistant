#!/bin/bash
# customize-Hook des Layers smartheat-gateway. $1 = Root-Dateisystem, $2 = Stage von prepare.sh.
# Legt den Installer-Baum und die Werkseinstellung (Container-Archive als <SHA-256>.tar, erstes Bundle, state.json-Vorlage)
# unter /opt/smartheat ins Image, fuehrt install.sh --image im chroot aus und prueft das Ergebnis (rootfs_checks.sh
# --customize; der volle Lauf folgt in hooks/postbuild50-rootfs-checks, nachdem mmdebstrap aufgeraeumt hat). Unter
# /var/lib/smartheat (= daten, ueberdeckt vom Bind-Mount) legt der Bau nichts ab: Ordner, Zustand und erstes Bundle legt
# der erste Start an (smartheat-ablage). Legt ausserdem die RAUC-Konfiguration ab (/etc/rauc/system.conf aus
# host/rauc/system.conf.in, die Vorlage hat keine Platzhalter).
# Keine Netzzugriffe aus dem chroot noetig (Pakete kommen aus dem Layer). Der Layer steht im Plan von rpi-image-gen
# v2.9.0 hinter openssh-server: dessen Hook (ssh aktivieren, Hostschluessel loeschen) ist hier schon gelaufen,
# install.sh --image schaltet ssh im Serien-Image wieder ab.
set -euo pipefail
R=$1 STAGE=$2
WERK="$R/opt/smartheat/werkseinstellung"
mkdir -p "$WERK/images" "$R/tmp/shg-bundle"
# Die Stage gehoert dem Aufrufer von make_image.sh (z. B. uid 1000 = pi bzw. Container auf dem Geraet); root fuehrt den
# Installer aus und laedt die Archive: Eigentuemer root, nichts gruppen-/weltbeschreibbar (rootfs_checks prueft es).
rm -rf "$R/opt/smartheat/installer"
cp -R --preserve=mode,timestamps --no-preserve=ownership "$STAGE/installer" "$R/opt/smartheat/installer"
cp -R --preserve=mode,timestamps --no-preserve=ownership "$STAGE/bundle/." "$R/tmp/shg-bundle/"
cp -R --preserve=mode,timestamps --no-preserve=ownership "$STAGE/images/." "$WERK/images/"
chown -R 0:0 "$R/opt/smartheat"
chmod -R go-w "$R/opt/smartheat"
chmod 0644 "$WERK"/images/*.tar
install -D -m 0644 -o 0 -g 0 "$R/opt/smartheat/installer/gateway/host/rauc/system.conf.in" "$R/etc/rauc/system.conf"
mapfile -t args <"$STAGE/install.args"
chroot "$R" bash /opt/smartheat/installer/gateway/host/install.sh --image "${args[@]}" --bundle /tmp/shg-bundle
rm -rf "$R/tmp/shg-bundle"
env -u SHG_ROOTFS_CHECK_UID bash "$R/opt/smartheat/installer/gateway/image/rootfs_checks.sh" "$R" --customize
