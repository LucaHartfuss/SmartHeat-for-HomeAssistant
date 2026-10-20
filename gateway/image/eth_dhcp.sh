#!/bin/bash
# customize-Hook des Layers smartheat-gateway: DHCPv4 auf eth0 mit der MAC als Client-ID. rpi-device-base (v2.8.0)
# schreibt /etc/systemd/network/01-eth0.network mit DHCP=yes; systemd-networkd nimmt als Client-ID dann die DUID
# (RFC 4361). Am Router des Labors kam darauf nie ein Angebot (H2, 2026-10-20), mit der MAC sofort; die Docker-Netze
# sind nur IPv4, ohne IPv4-Adresse erreicht die Laufzeit den Server nicht. Das Drop-in laesst 01-eth0.network
# unveraendert.
# Aufruf (mmdebstrap): eth_dhcp.sh ROOTFS
set -euo pipefail
R="${1:?Root-Dateisystem fehlt}"
NET="$R/etc/systemd/network/01-eth0.network"
if [ ! -f "$NET" ]; then
  echo "eth_dhcp.sh: $NET fehlt (Layer rpi-device-base)" >&2
  exit 1
fi
mkdir -p "$NET.d"
printf '%s\n' '# SmartHeat-Gateway: MAC als DHCPv4-Client-ID (gateway/image/eth_dhcp.sh)' '[DHCPv4]' \
  'ClientIdentifier=mac' >"$NET.d/10-smartheat-client-id.conf"
echo "DHCPv4 auf eth0 mit der MAC als Client-ID"
