#!/bin/bash
# customize-Hook des Layers smartheat-gateway: kein statischer Hostname im Image. Der Bau (device.hostname) legt
# /etc/hostname an; systemd (hostname(7)) gibt dem statischen Hostnamen Vorrang vor einem fluechtigen, also
# schluege `hostnamectl --transient hostname <Name aus werk.json>` (smartheat-werk.service) nie durch. Darum wird die Datei
# entfernt und der Name des Images als Rueckfallname gesetzt: DEFAULT_HOSTNAME= in os-release (os-release(5): "A string
# specifying the hostname if hostname(5) is not present and no other configuration source specifies the hostname", seit
# systemd 248; Trixie hat 257). Ohne gueltige werk.json bleibt es dieser Name, mit ihr gilt der fluechtige Name aus werk.json.
# Aufruf (mmdebstrap): hostname_default.sh ROOTFS
set -euo pipefail
R="${1:?Root-Dateisystem fehlt}"
NAME=smartheat-gateway  # wie device.hostname in config/smartheat-gateway.yaml (test_image_scripts.py prueft die Gleichheit)
OSREL="$R/usr/lib/os-release"
if [ ! -f "$OSREL" ]; then
  echo "hostname_default.sh: $OSREL fehlt (Paket base-files)" >&2
  exit 1
fi
if grep -q '^DEFAULT_HOSTNAME=' "$OSREL"; then
  sed -i "s/^DEFAULT_HOSTNAME=.*/DEFAULT_HOSTNAME=$NAME/" "$OSREL"
else
  printf 'DEFAULT_HOSTNAME=%s\n' "$NAME" >>"$OSREL"
fi
rm -f "$R/etc/hostname"
echo "Kein statischer Hostname, Rueckfallname $NAME (DEFAULT_HOSTNAME in os-release)"
