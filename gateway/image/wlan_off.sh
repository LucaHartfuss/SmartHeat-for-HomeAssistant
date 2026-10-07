#!/bin/bash
# customize-Hook des Layers smartheat-gateway: WLAN aus in jedem Image (Serie und Pilot; Nutzer-Vorgabe 2026-10-07,
# das Gateway laeuft nur am Ethernet). rpi-image-gen v2.8.0 bringt WLAN ueber zwei Layer mit: trixie-minbase verlangt
# den Layer iwd (Paket iwd, enable-units aktiviert iwd.service), rpi-device-base schreibt
# /etc/systemd/network/02-wlan0.network (DHCP auf wlan0). Dieser Hook
#   - deaktiviert und maskiert iwd.service offline im chroot (wie install.sh --image; die Maske haelt iwd auch dann
#     aus, wenn eine Abhaengigkeit oder D-Bus-Aktivierung es starten wollte),
#   - entfernt 02-wlan0.network,
#   - haengt dtoverlay=disable-wifi in einem eigenen [all]-Abschnitt an /boot/firmware/config.txt an. Die Datei legt
#     rpi-boot-firmware vorher aus der Vorlage an (endet mit [all]); danach aendert sie in v2.8.0 kein Layer und kein
#     Hook mehr, genimage packt /boot/firmware unveraendert in die Boot-Partition.
# Bluetooth bleibt unberuehrt: das Image hat keinen Bluetooth-Dienst (kein bluez, nur bluez-firmware aus
# rpi-device-base), und dtoverlay=disable-bt wuerde die UART-Zuordnung des Pi 4 umstellen (PL011 auf GPIO 14/15).
# Aufruf (mmdebstrap): wlan_off.sh ROOTFS
set -euo pipefail
R="${1:?Root-Dateisystem fehlt}"
CONFIG="$R/boot/firmware/config.txt"
if [ ! -f "$CONFIG" ]; then
  echo "wlan_off.sh: $CONFIG fehlt (Layer rpi-boot-firmware)" >&2
  exit 1
fi
chroot "$R" systemctl disable iwd.service
chroot "$R" systemctl mask iwd.service
rm -f "$R/etc/systemd/network/02-wlan0.network"
if ! grep -qx 'dtoverlay=disable-wifi' "$CONFIG"; then
  printf '\n# SmartHeat-Gateway: nur Ethernet, WLAN-Chip abgeschaltet (gateway/image/wlan_off.sh)\n[all]\n%s\n' \
    'dtoverlay=disable-wifi' >>"$CONFIG"
fi
echo "WLAN aus: iwd maskiert, 02-wlan0.network entfernt, dtoverlay=disable-wifi in config.txt"
