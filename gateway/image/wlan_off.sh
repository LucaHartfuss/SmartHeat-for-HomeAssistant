#!/bin/bash
# customize-Hook des Layers smartheat-gateway: WLAN aus in jedem Image (Serie und Pilot; Nutzer-Vorgabe 2026-10-07,
# das Gateway laeuft nur am Ethernet). rpi-image-gen v2.9.0 bringt WLAN ueber zwei Layer mit: trixie-minbase verlangt
# den Layer iwd (Paket iwd, enable-units aktiviert iwd.service), rpi-device-base schreibt
# /etc/systemd/network/02-wlan0.network (DHCP auf wlan0). Dieser Hook
#   - deaktiviert und maskiert iwd.service offline im chroot (wie install.sh --image; die Maske haelt iwd auch dann
#     aus, wenn eine Abhaengigkeit oder D-Bus-Aktivierung es starten wollte),
#   - entfernt 02-wlan0.network.
# Den WLAN-Chip schaltet das Overlay disable-wifi-pi5 ab: es steht im config.txt-Fragment 80-smartheat-funk des Layers
# (layer/smartheat-gateway.d/for/rpi-boot-firmware/config.txt.d), denn config.txt entsteht in v2.9.0 erst in der Phase
# preimage aus den Fragmenten; hooks/finalize10-bootfs-checks prueft das Ergebnis.
# Bluetooth bleibt im Dienst unberuehrt: das Image hat keinen Bluetooth-Dienst (kein bluez, nur bluez-firmware aus
# rpi-device-base); den Chip schaltet dasselbe Fragment mit disable-bt-pi5 ab.
# Aufruf (mmdebstrap): wlan_off.sh ROOTFS
set -euo pipefail
R="${1:?Root-Dateisystem fehlt}"
chroot "$R" systemctl disable iwd.service
chroot "$R" systemctl mask iwd.service
rm -f "$R/etc/systemd/network/02-wlan0.network"
echo "WLAN aus: iwd maskiert, 02-wlan0.network entfernt"
