#!/bin/bash
# Prueft im fertigen Root-Dateisystem, dass die USB-Startregel (usbboot.sh) wirklich in der initramfs liegt (Plan
# G2b-2 Task 9, CI-Spike 2026-10-07): ohne sie fehlen beim Start vom USB die by-slot-Links und der Start bliebe in der
# initramfs haengen. Getrennt von rootfs_checks.sh, weil lsinitramfs nur im Root-Dateisystem selbst laeuft (chroot)
# und rootfs_checks.sh auf einem blossen entpackten Root-Dateisystem laufen muss (Tests). Nur im post-build-Hook.
# Aufruf: initramfs_check.sh ROOTFS
# Pruefobjekte: /boot/firmware/initramfs* (was der Pi startet, rpi-image-gen v2.8.0: initramfs8) und /boot/initrd.img-*
# (Ausgabe von update-initramfs); gefunden wird per Glob, jede gefundene Datei muss die Regel enthalten.
# Exit 0 = in Ordnung, 1 = Verstoesse (je Zeile "FAIL: ..."), 2 = falscher Aufruf.
set -u
R="${1:?ROOTFS fehlt}"
RULE=etc/udev/rules.d/99-rpi-01-smartheat-usbboot.rules
FAIL=0
fail() { echo "FAIL: $1"; FAIL=1; }
files=()
for f in "$R"/boot/firmware/initramfs* "$R"/boot/initrd.img-*; do
  [ -f "$f" ] && files+=("$f")
done
if [ "${#files[@]}" = 0 ]; then
  fail "keine initramfs im Image gefunden (/boot/firmware/initramfs*, /boot/initrd.img-*)"
fi
for f in "${files[@]}"; do
  rel="${f#"$R"}"
  if ! listing="$(chroot "$R" lsinitramfs "$rel" 2>&1)"; then
    fail "lsinitramfs $rel fehlgeschlagen: $(head -n 1 <<<"$listing")"
  elif ! grep -qF "$RULE" <<<"$listing"; then
    fail "USB-Startregel fehlt in der initramfs $rel (usbboot.sh, initramfs-Hook)"
  fi
done
exit "$FAIL"
