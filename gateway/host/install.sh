#!/bin/bash
# Host-Installer des SmartHeat-Gateways (Spec SHG G2 8.2, G2b-1 6): idempotent, eine Quelle fuer das Image (G2b-2,
# chroot mit --no-activate) und fuer Bastler auf frischem Raspberry Pi OS Lite 64 bit (Debian 13 trixie).
# Aufruf: install.sh --device-api-url URL [--portal-base-url URL] [--diag-hostnames NAMEN] [--timezone ZONE]
#                    [--bundle ORDNER] [--pilot-ssh PUBKEY] [--root PFAD] [--no-docker] [--no-activate]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
GW="$(cd "$HERE/.." && pwd)"
ROOT=/var/lib/smartheat OPT=/opt/smartheat/host API_URL="" PORTAL_URL="" DIAG_NAMES="" TZ_NAME="Europe/Berlin"
BUNDLE="" PILOT_KEY="" DOCKER=1 ACTIVATE=1
while [ $# -gt 0 ]; do
  case "$1" in
    --device-api-url) API_URL="$2"; shift ;;
    --portal-base-url) PORTAL_URL="$2"; shift ;;
    --diag-hostnames) DIAG_NAMES="$2"; shift ;;
    --timezone) TZ_NAME="$2"; shift ;;
    --bundle) BUNDLE="$2"; shift ;;
    --pilot-ssh) PILOT_KEY="$2"; shift ;;
    --root) ROOT="$2"; shift ;;
    --no-docker) DOCKER=0 ;;
    --no-activate) ACTIVATE=0 ;;
    *) echo "Unbekannte Option: $1" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$API_URL" ] || { echo "--device-api-url fehlt" >&2; exit 2; }
ROOT="${ROOT%/}"
[ -n "$ROOT" ] || { echo "--root darf nicht / sein" >&2; exit 2; }
CHANGED=0 WROTE=0 FIREWALL_CHANGED=0

# Bricht ab, wenn $1 (unter ROOT) oder eine Komponente zwischen ROOT und $1 ein Symlink ist. Ordner unter ROOT
# gehoeren zum Teil uid 1000 (Container): ein dort angelegter Symlink duerfte den Installer (root) nie auf einen
# Systempfad umlenken (chown/chmod/Schreiben). Restrisiko: Wettlauf zwischen Pruefung und Zugriff.
refuse_symlinks() {
  local path=$1 cur rest part
  case "$path" in "$ROOT"|"$ROOT"/*) ;; *) return 0 ;; esac
  cur="$ROOT"
  rest="${path#"$ROOT"}"
  while :; do
    if [ -L "$cur" ]; then echo "FEHLER: $cur ist ein Symlink, Installer bricht ab" >&2; exit 1; fi
    rest="${rest#/}"
    [ -n "$rest" ] || break
    part="${rest%%/*}"
    cur="$cur/$part"
    case "$rest" in */*) rest="${rest#*/}" ;; *) rest="" ;; esac
  done
}

# Schreibt stdin nach $1 (Modus $2, Eigentuemer $3), nur wenn sich der Inhalt aendert (WROTE=1 bei jeder Aenderung).
# Nie hinter einer Pipe aufrufen (CHANGED ginge in der Unter-Shell verloren): Eingabe per Umleitung oder < <(...).
put() {
  local target=$1 mode=$2 owner=$3 tmp
  refuse_symlinks "$target"
  tmp="$(mktemp)"; cat >"$tmp"
  mkdir -p "$(dirname "$target")"
  if [ -f "$target" ] && cmp -s "$tmp" "$target"; then rm -f "$tmp"
  else install -m "$mode" -o "${owner%:*}" -g "${owner#*:}" "$tmp" "$target"; rm -f "$tmp"; CHANGED=1; WROTE=1; echo "geschrieben: $target"; fi
  if [ "$(stat -c '%a %u:%g' "$target")" != "${mode#0} $owner" ]; then
    chmod "$mode" "$target"; chown -h "$owner" "$target"; CHANGED=1; WROTE=1
  fi
}
dir() {  # Ordner $1 mit Modus $2 und Eigentuemer $3
  refuse_symlinks "$1"
  if [ ! -d "$1" ]; then mkdir -p "$1"; CHANGED=1; fi
  if [ "$(stat -c '%a %u:%g' "$1")" != "${2#0} $3" ]; then chmod "$2" "$1"; chown -h "$3" "$1"; CHANGED=1; fi
}
subst() { sed -e "s|@ROOT@|$ROOT|g" "$1"; }

packages() {
  local wanted=(python3 python3-cryptography unattended-upgrades nftables)
  [ "$DOCKER" = 1 ] && wanted+=(docker.io docker-compose)
  local missing=() pkg
  for pkg in "${wanted[@]}"; do
    dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed" || missing+=("$pkg")
  done
  if [ ${#missing[@]} -gt 0 ]; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "${missing[@]}"
    CHANGED=1
  fi
}

layout() {
  local path
  dir "$ROOT" 0755 0:0
  dir "$ROOT/data" 0755 1000:1000          # Agent und Laufzeit lesen/schreiben als uid 1000 (G2a-Restpunkt)
  # Bind-Quellen vor dem ersten Start mit uid 1000 anlegen: fehlende Quellen legte Docker als root an, der Init-Schritt
  # (uid 1000) koennte dann nichts schreiben (Plan-Praezisierung 4).
  for path in zigbee2mqtt bus bus/mosquitto bus/credentials bus/credentials/agent bus/credentials/runtime \
              bus/credentials/zigbee2mqtt; do dir "$ROOT/$path" 0700 1000:1000; done
  dir "$ROOT/host" 0755 0:0
  dir "$ROOT/bundles" 0755 0:0
  dir "$ROOT/updater" 0755 0:0
  put "$ROOT/host/gateway.env" 0644 0:0 < <(printf \
    'SHG_ROOT=%s\nSHG_DEVICE_API_URL=%s\nSHG_PORTAL_BASE_URL=%s\nSHG_DIAG_HOSTNAMES=%s\nZIGBEE_ADAPTER=ember\nTZ=%s\n' \
    "$ROOT" "$API_URL" "$PORTAL_URL" "$DIAG_NAMES" "$TZ_NAME")
}

host_package() {
  local file name rel
  dir "$OPT" 0755 0:0
  for file in "$HERE"/smartheat_host/*.py; do
    name="$(basename "$file")"
    put "$OPT/smartheat_host/$name" 0644 0:0 <"$file"
  done
  for rel in __init__.py files.py paths.py version.py agent/__init__.py agent/wire.py agent/identity.py; do
    put "$OPT/smartheat_gateway/$rel" 0644 0:0 <"$GW/src/smartheat_gateway/$rel"
  done
  put "$OPT/release.pub" 0644 0:0 <"$HERE/release.pub"
  put "$OPT/VERSION" 0644 0:0 <"$GW/VERSION"
}

system_files() {
  local unit ssh_rule=""
  for unit in "$HERE"/systemd/*.service; do
    WROTE=0
    put "/etc/systemd/system/$(basename "$unit")" 0644 0:0 < <(subst "$unit")
    if [ "$WROTE" = 1 ] && [ "$(basename "$unit")" = smartheat-firewall.service ]; then FIREWALL_CHANGED=1; fi
  done
  put /etc/udev/rules.d/99-smartheat-zigbee.rules 0644 0:0 < <(subst "$HERE/udev/99-smartheat-zigbee.rules")
  [ -n "$PILOT_KEY" ] && ssh_rule="tcp dport 22 accept"
  WROTE=0
  put /etc/smartheat/nftables.conf 0644 0:0 < <(sed -e "s|@PILOT_SSH@|$ssh_rule|" "$HERE/nftables.conf")
  if [ "$WROTE" = 1 ]; then FIREWALL_CHANGED=1; fi
  put /etc/systemd/journald.conf.d/smartheat.conf 0644 0:0 <"$HERE/journald.conf.d/smartheat.conf"
  put /etc/apt/apt.conf.d/52smartheat-unattended 0644 0:0 <"$HERE/apt/52smartheat-unattended"
  if [ -n "$PILOT_KEY" ]; then
    put /etc/ssh/sshd_config.d/00-smartheat.conf 0644 0:0 < <(printf \
      'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin prohibit-password\n')
    dir /root/.ssh 0700 0:0
    put /root/.ssh/authorized_keys 0600 0:0 < <(printf '%s\n' "$PILOT_KEY")
  elif [ -f /etc/ssh/sshd_config.d/00-smartheat.conf ]; then
    rm -f /etc/ssh/sshd_config.d/00-smartheat.conf; CHANGED=1
  fi
  # sshd nimmt je Schluesselwort den ersten Wert: nur ein Drop-in vor allen anderen (00-) setzt sich durch. Der fruehere
  # Name smartheat.conf wuerde von lexikalisch frueheren Dateien (z. B. 50-cloud-init.conf) uebergangen.
  if [ -f /etc/ssh/sshd_config.d/smartheat.conf ]; then rm -f /etc/ssh/sshd_config.d/smartheat.conf; CHANGED=1; fi
}

first_bundle() {
  [ -n "$BUNDLE" ] || return 0
  local version name
  version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$BUNDLE/manifest.json")"
  for name in docker-compose.yml mosquitto.conf manifest.json; do
    put "$ROOT/bundles/$version/$name" 0644 0:0 <"$BUNDLE/$name"
  done
  if [ ! -f "$ROOT/updater/state.json" ]; then
    put "$ROOT/updater/state.json" 0644 0:0 < <(printf \
      '{"current": "%s", "in_progress": null, "previous": null, "rejected": []}' "$version")
  fi
}

activate() {
  [ "$ACTIVATE" = 1 ] || return 0
  systemctl daemon-reload
  udevadm control --reload
  udevadm trigger --subsystem-match=tty
  systemctl restart systemd-journald
  systemctl enable --now smartheat-firewall.service smartheat-hoststatus.service smartheat-led.service
  # Oneshot mit RemainAfterExit: geaenderte Regeln (z. B. Pilot-SSH an/aus) greifen erst nach einem Neustart der Unit.
  if [ "$FIREWALL_CHANGED" = 1 ]; then systemctl restart smartheat-firewall.service; fi
  if [ "$DOCKER" = 1 ]; then systemctl enable --now docker.service smartheat-updater.service; fi
  if [ -n "$PILOT_KEY" ]; then
    systemctl enable --now ssh.service
    systemctl reload ssh.service
  else systemctl disable --now ssh.service 2>/dev/null || true; fi
  local current
  current="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("current") or "")' \
    "$ROOT/updater/state.json" 2>/dev/null || true)"
  if [ "$DOCKER" = 1 ] && [ -n "$current" ]; then
    docker compose -p smartheat --env-file "$ROOT/host/gateway.env" -f "$ROOT/bundles/$current/docker-compose.yml" up -d
  fi
}

packages
layout
host_package
system_files
first_bundle
activate
if [ "$CHANGED" = 0 ]; then echo "Nichts zu tun (bereits installiert)"; fi
