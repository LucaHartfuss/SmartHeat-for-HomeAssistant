#!/bin/bash
# customize-Hook des Layers smartheat-gateway: Start von der SSD am USB (Plan G2b-2 Task 9, CI-Spike 2026-10-07).
# Das Image-Layout image-rpios von rpi-image-gen v2.8.0 setzt root=/dev/disk/by-slot/system (cmdline.txt) und
# by-slot/{system,boot} in etc/fstab. Die Links legt die Regel 99-rpi-05-image.rules nur fuer Partitionen mit
# RPI_ONBOOTDEV=1 an, und das setzt rpi-storage-binder (99-rpi-00-bootdev.rules, rpi-bootdev-tag) nur fuer SD/eMMC
# (mmcblk0p*) und NVMe (nvme0n1p*) - beim Start vom USB (boot-mode 4) fehlte die Root-Partition, der Start bliebe in
# der initramfs haengen. Diese Regel markiert die beiden Partitionen des Images am USB (sd*) ueber die feste
# MBR-Disk-Signatur aus config/smartheat-gateway.yaml (image.disksig, PARTUUID <signatur>-01/-02) und liegt per
# initramfs-Hook auch in der initramfs (die Root-Partition wird dort eingehaengt). Die Datei sortiert zwischen
# 99-rpi-00-bootdev und 99-rpi-05-image, also nach 60-persistent-storage (setzt ID_PART_ENTRY_UUID).
# Aufruf (mmdebstrap): usbboot.sh ROOTFS, Umgebung IGconf_image_disksig (0x + 8 Hex).
set -euo pipefail
R="${1:?Root-Dateisystem fehlt}"
sig="${IGconf_image_disksig:-}"
if [[ ! $sig =~ ^0x[0-9a-fA-F]{8}$ ]]; then
  echo "usbboot.sh: image.disksig muss fest sein (0x + 8 Hex), nicht '${sig}'" >&2
  exit 1
fi
sig="${sig#0x}"
sig="${sig,,}"
RULE=99-rpi-01-smartheat-usbboot.rules
install -d -m 0755 "$R/etc/udev/rules.d" "$R/etc/initramfs-tools/hooks"
cat >"$R/etc/udev/rules.d/$RULE" <<EOF
# SmartHeat-Gateway: Start von der SSD am USB (gateway/image/usbboot.sh). rpi-storage-binder markiert nur SD/eMMC und
# NVMe als Startgeraet; hier die Partitionen des Images (MBR-Signatur $sig) am USB.
SUBSYSTEM=="block", KERNEL=="sd[a-z]*[0-9]", ENV{DEVTYPE}=="partition", ENV{ID_PART_ENTRY_UUID}=="$sig-0[12]", ACTION=="add|change", ENV{RPI_ONBOOTDEV}="1"
EOF
chmod 0644 "$R/etc/udev/rules.d/$RULE"
cat >"$R/etc/initramfs-tools/hooks/smartheat-usbboot" <<EOF
#!/bin/sh
# SmartHeat-Gateway: USB-Startregel in die initramfs (gateway/image/usbboot.sh)
case \$1 in
   prereqs) echo ""; exit 0;;
esac
. /usr/share/initramfs-tools/hook-functions
copy_file config /etc/udev/rules.d/$RULE
EOF
chmod 0755 "$R/etc/initramfs-tools/hooks/smartheat-usbboot"
echo "USB-Start: $RULE (MBR-Signatur $sig)"
