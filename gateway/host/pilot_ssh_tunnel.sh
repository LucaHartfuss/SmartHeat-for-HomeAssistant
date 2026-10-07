#!/bin/bash
# Pilot-SSH-Tunnel des SmartHeat-Gateways (Plan G2b-2 Task 8): eigener Cloudflare-Tunnel nur fuer SSH, als Host-Dienst
# ausserhalb von Docker (Rettungsweg, wenn Docker oder der Updater haengen). Nur fuer Pilotgeraete mit install.sh
# --pilot-ssh. Der oeffentliche Hostname (ssh://localhost:22) und die Access-Policy werden in Cloudflare angelegt
# (Runbook); hierher kommt nur das Token des Tunnels, als Datei, nie als Argument.
# Aufruf: pilot_ssh_tunnel.sh install --token-file DATEI [--deb DATEI] [--no-activate]
#         pilot_ssh_tunnel.sh remove [--no-activate]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE=/etc/smartheat/pilot-ssh-tunnel.env
UNIT=/etc/systemd/system/smartheat-pilot-ssh.service
ACTION="${1:-}"; shift || true
TOKEN_FILE="" DEB="" ACTIVATE=1 CHANGED=0 TOKEN=""
while [ $# -gt 0 ]; do
  case "$1" in
    --token-file) TOKEN_FILE="${2:?--token-file braucht eine Datei}"; shift ;;
    --deb) DEB="${2:?--deb braucht eine Datei}"; shift ;;
    --no-activate) ACTIVATE=0 ;;
    *) echo "Unbekannte Option: $1" >&2; exit 2 ;;
  esac
  shift
done

pin() { sed -n "s/^$1=//p" "$HERE/cloudflared.pin"; }

# Liest und prueft das Token (eine Zeile, Zeichen wie in einem Base64-Token), noch bevor etwas installiert wird.
read_token() {
  [ -r "$TOKEN_FILE" ] || { echo "--token-file fehlt oder ist nicht lesbar" >&2; exit 2; }
  [ "$(grep -c . "$TOKEN_FILE")" = 1 ] || { echo "Token-Datei muss genau eine Zeile enthalten" >&2; exit 2; }
  TOKEN="$(tr -d '[:space:]' <"$TOKEN_FILE")"
  [[ "$TOKEN" =~ ^[A-Za-z0-9+/=_-]{20,}$ ]] || { echo "Token hat ein unerwartetes Format" >&2; exit 2; }
}

install_deb() {
  local arch version sha file
  arch="$(dpkg --print-architecture)"
  version="$(pin VERSION)"; sha="$(pin "SHA256_$arch")"
  if [ -z "$version" ] || [ -z "$sha" ]; then echo "Kein Pin fuer $arch in cloudflared.pin" >&2; exit 1; fi
  if [ "$(dpkg-query -W -f='${Version}' cloudflared 2>/dev/null || true)" = "$version" ]; then return 0; fi
  file="$(mktemp --suffix=.deb)"
  if [ -n "$DEB" ]; then cp "$DEB" "$file"
  else
    python3 -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' \
      "https://github.com/cloudflare/cloudflared/releases/download/$version/cloudflared-linux-$arch.deb" "$file"
  fi
  if [ "$(sha256sum "$file" | cut -d' ' -f1)" != "$sha" ]; then
    rm -f "$file"; echo "FEHLER: SHA-256 von cloudflared $version ($arch) passt nicht zur Pin-Datei" >&2; exit 1
  fi
  dpkg -i "$file" >/dev/null; rm -f "$file"; CHANGED=1; echo "installiert: cloudflared $version"
}

write_token() {
  local tmp
  mkdir -p /etc/smartheat
  tmp="$(mktemp)"; printf 'TUNNEL_TOKEN=%s\n' "$TOKEN" >"$tmp"
  if [ -f "$ENV_FILE" ] && cmp -s "$tmp" "$ENV_FILE"; then rm -f "$tmp"
  else install -m 0600 -o 0 -g 0 "$tmp" "$ENV_FILE"; rm -f "$tmp"; CHANGED=1; echo "geschrieben: $ENV_FILE"; fi
}

write_unit() {
  if [ -f "$UNIT" ] && cmp -s "$HERE/pilot/smartheat-pilot-ssh.service" "$UNIT"; then return 0; fi
  install -m 0644 -o 0 -g 0 "$HERE/pilot/smartheat-pilot-ssh.service" "$UNIT"; CHANGED=1; echo "geschrieben: $UNIT"
}

case "$ACTION" in
  install)
    read_token; install_deb; write_token; write_unit
    if [ "$ACTIVATE" = 1 ]; then
      systemctl daemon-reload; systemctl enable smartheat-pilot-ssh.service
      if [ "$CHANGED" = 1 ]; then systemctl restart smartheat-pilot-ssh.service; fi
    fi ;;
  remove)
    if [ "$ACTIVATE" = 1 ]; then systemctl disable --now smartheat-pilot-ssh.service 2>/dev/null || true; fi
    if [ -e "$UNIT" ] || [ -e "$ENV_FILE" ]; then rm -f "$UNIT" "$ENV_FILE"; CHANGED=1; echo "entfernt: Pilot-SSH-Tunnel"; fi
    if [ "$ACTIVATE" = 1 ]; then systemctl daemon-reload; fi ;;
  *) echo "Aufruf: $0 install --token-file DATEI [--deb DATEI] [--no-activate] | remove [--no-activate]" >&2; exit 2 ;;
esac
if [ "$CHANGED" = 0 ]; then echo "Nichts zu tun"; fi
