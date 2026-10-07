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
# Waechter gegen doppeltes Anhaengen: nur der eigene Block (Markierung, [all], Overlay in drei Zeilen hintereinander);
# ein disable-wifi in einem anderen Abschnitt (z. B. [pi5]) wirkt auf dem Pi 4 nicht und zaehlt nicht.
MARK='# SmartHeat-Gateway: nur Ethernet, WLAN-Chip abgeschaltet (gateway/image/wlan_off.sh)'
if ! awk -v mark="$MARK" '{ l[NR] = $0 }
      END { for (i = 1; i + 2 <= NR; i++)
              if (l[i] == mark && l[i + 1] == "[all]" && l[i + 2] == "dtoverlay=disable-wifi") exit 0
            exit 1 }' "$CONFIG"; then
  printf '\n%s\n[all]\ndtoverlay=disable-wifi\n' "$MARK" >>"$CONFIG"
fi
echo "WLAN aus: iwd maskiert, 02-wlan0.network entfernt, dtoverlay=disable-wifi in config.txt"
