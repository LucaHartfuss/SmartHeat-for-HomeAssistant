#!/bin/bash
# customize-Hook des Layers smartheat-gateway: das A/B-System (GW 6.3) im Root-Dateisystem vorbereiten.
#   - /werk anlegen und werk.mount aktivieren (die Unit liefert das Overlay des Layers; /etc/fstab ueberschreibt
#     image-rota beim Bau, darum eine eigene Mount-Unit),
#   - rauc.service in multi-user.target aufnehmen (add-wants): die Unit aus dem Paket rauc-service hat keinen
#     [Install]-Abschnitt (D-Bus-Aktivierung), systemctl enable bliebe wirkungslos (Bau 2026-10-27),
#   - rauc-mark-good.service maskieren, falls das Paket sie mitbringt: sie bestaetigte jeden Probestart sofort, bevor
#     die lokale Gesundheit geprueft ist; bestaetigt wird nur vom Updater (mark-good nach der Gesundheitsprobe).
#     Die Maske ist ein Link auf /dev/null und geht auch ohne Unit-Datei.
# Aufruf (mmdebstrap): ab_system.sh ROOTFS
set -euo pipefail
R="${1:?Root-Dateisystem fehlt}"
if [ ! -f "$R/etc/systemd/system/werk.mount" ]; then
  echo "ab_system.sh: $R/etc/systemd/system/werk.mount fehlt (Overlay des Layers)" >&2
  exit 1
fi
mkdir -p "$R/werk"
chmod 0755 "$R/werk"
chroot "$R" systemctl enable werk.mount
chroot "$R" systemctl add-wants multi-user.target rauc.service
chroot "$R" systemctl mask rauc-mark-good.service
echo "A/B-System: /werk, werk.mount und rauc.service aktiviert, rauc-mark-good.service maskiert"
