#!/bin/bash
# Pruefungen am Root-Dateisystem des Gateway-Images (Plan G2b-2 Task 9): bricht den Image-Bau ab, wenn ein Geraet aus
# dem Image eine geteilte Identitaet (machine-id, SSH-Hostschluessel, Geraeteschluessel, Bus-Zugangsdaten), ein
# Passwort, ein Tunnel-Token oder im Serien-Image SSH bekaeme, wenn der Erststart ohne Pull nicht moeglich waere oder
# die Geraete-API-/Portal-Adresse kein eigener DNS-Name per https ist (own_url.sh).
# Aufruf: rootfs_checks.sh ROOTFS [--pilot] [--customize]
#   --customize: Lauf im customize-Hook (customize.sh). Die machine-id legt dort noch systemd an; zurueckgesetzt wird
#                sie erst beim Aufraeumen von mmdebstrap, geprueft dann im post-build-Hook (post-build.sh, voller Lauf).
# Exit 0 = in Ordnung, 1 = Verstoesse (je Zeile "FAIL: ..."), 2 = falscher Aufruf.
# shellcheck source-path=SCRIPTDIR
set -u
R="${1:?ROOTFS fehlt}"
shift
PILOT=0 CUSTOMIZE=0
for arg in "$@"; do
  case "$arg" in
    --pilot) PILOT=1 ;;
    --customize) CUSTOMIZE=1 ;;
    *) echo "Unbekannte Option: $arg" >&2; exit 2 ;;
  esac
done
# shellcheck source=own_url.sh
. "$(dirname "${BASH_SOURCE[0]}")/own_url.sh" || { echo "FAIL: own_url.sh fehlt"; exit 1; }
FAIL=0
fail() { echo "FAIL: $1"; FAIL=1; }
WANTS="$R/etc/systemd/system/multi-user.target.wants"
SH="$R/var/lib/smartheat"

# Identitaet: jedes Geraet erzeugt sie selbst beim ersten Start
if [ "$CUSTOMIZE" = 0 ]; then
  mid="$(cat "$R/etc/machine-id" 2>/dev/null || true)"
  case "$mid" in ""|uninitialized) ;; *) fail "machine-id im Image gesetzt" ;; esac
fi
if [ -f "$R/var/lib/dbus/machine-id" ] && [ ! -L "$R/var/lib/dbus/machine-id" ] \
   && [ -s "$R/var/lib/dbus/machine-id" ]; then
  fail "dbus-machine-id im Image gesetzt"
fi
if compgen -G "$R/etc/ssh/ssh_host_*" >/dev/null; then fail "SSH-Hostschluessel im Image"; fi
[ -z "$(ls -A "$SH/data" 2>/dev/null)" ] || fail "data/ nicht leer (Geraeteschluessel, Agent-Zustand)"
if compgen -G "$SH/bus/credentials/*/bus.json" >/dev/null; then fail "Bus-Zugangsdaten im Image"; fi
[ ! -e "$SH/zigbee2mqtt/configuration.yaml" ] || fail "Zigbee2MQTT-Konfiguration im Image"
[ ! -e "$R/etc/smartheat/pilot-ssh-tunnel.env" ] || fail "Tunnel-Token im Image"

# Keine Anmeldung per Passwort (leeres Feld = ohne Passwort, also ebenfalls verboten)
if [ -r "$R/etc/shadow" ]; then
  while IFS=: read -r user hash _; do
    [ -n "$user" ] || continue
    case "$hash" in '!'*|'*'*) ;; *) fail "Benutzer $user kann sich mit Passwort (oder ohne) anmelden" ;; esac
  done <"$R/etc/shadow"
else
  fail "etc/shadow fehlt"
fi

# Dienste
for unit in smartheat-firewall smartheat-hoststatus smartheat-led smartheat-firstboot smartheat-updater docker; do
  [ -L "$WANTS/$unit.service" ] || fail "$unit nicht aktiviert"
done
if [ "$PILOT" = 1 ]; then
  [ -L "$WANTS/ssh.service" ] || fail "ssh im Pilot-Image nicht aktiviert"
  [ -s "$R/root/.ssh/authorized_keys" ] || fail "Pilot-Schluessel fehlt"
else
  # -L statt -e: die Links zeigen absolut ins Image (/lib/systemd/...) und waeren vom Build-Host aus oft "kaputt".
  if [ -L "$WANTS/ssh.service" ] || [ -e "$WANTS/ssh.service" ]; then fail "ssh im Serien-Image aktiviert"; fi
  if [ -L "$R/etc/systemd/system/sockets.target.wants/ssh.socket" ]; then
    fail "ssh.socket im Serien-Image aktiviert"
  fi
  for keys in "$R"/root/.ssh/authorized_keys "$R"/home/*/.ssh/authorized_keys; do
    [ ! -s "$keys" ] || fail "authorized_keys im Serien-Image (${keys#"$R"})"
  done
fi

# Erststart ohne Pull, signiertes laufendes Bundle
if ! compgen -G "$SH/images/*.tar" >/dev/null; then fail "keine Image-Archive fuer den Erststart"; fi
current="$(sed -n 's/.*"current": *"\([^"]*\)".*/\1/p' "$SH/updater/state.json" 2>/dev/null)"
if [ -z "$current" ] || [ ! -f "$SH/bundles/$current/manifest.json.minisig" ]; then
  fail "laufendes Bundle fehlt oder ist nicht signiert"
fi

# Sicherheitsupdates aus dem Raspberry-Pi-Archiv moeglich
grep -rqs "archive.raspberrypi.com" "$R/etc/apt/sources.list.d" "$R/etc/apt/sources.list" \
  || fail "Raspberry-Pi-Archiv nicht eingebunden"

# Geraete-API und Portal: eigener DNS-Name per https (letzte Zuweisung zaehlt, wie bei Docker und systemd)
env_value() { sed -n "s/^$1=//p" "$SH/host/gateway.env" 2>/dev/null | tail -n 1; }
for pair in "SHG_DEVICE_API_URL:Geraete-API" "SHG_PORTAL_BASE_URL:Portal"; do
  value="$(env_value "${pair%%:*}")"
  if ! why="$(shg_own_url_problem "$value")"; then
    fail "${pair#*:}-Adresse '${value}' in gateway.env: $why"
  fi
done
exit "$FAIL"
