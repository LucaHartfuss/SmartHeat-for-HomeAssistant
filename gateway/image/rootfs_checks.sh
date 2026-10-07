#!/bin/bash
# Pruefungen am Root-Dateisystem des Gateway-Images (Plan G2b-2 Task 9): bricht den Image-Bau ab, wenn ein Geraet aus
# dem Image eine geteilte Identitaet (machine-id, SSH-Hostschluessel, Geraeteschluessel, Bus-Zugangsdaten), ein
# Passwort, ein Tunnel-Token oder im Serien-Image SSH bekaeme, wenn der Erststart ohne Pull nicht moeglich waere oder
# die Geraete-API-/Portal-Adresse kein eigener DNS-Name per https ist (own_url.sh).
# Aufruf: rootfs_checks.sh ROOTFS [--pilot] [--customize]
#   --customize: Lauf im customize-Hook (customize.sh). Die machine-ids (systemd, dbus) bestehen dort noch;
#                zurueckgesetzt werden sie erst beim Aufraeumen von mmdebstrap, geprueft dann im post-build-Hook
#                (post-build.sh, voller Lauf).
# Exit 0 = in Ordnung, 1 = Verstoesse (je Zeile "FAIL: ..."), 2 = falscher Aufruf.
# Eigentuemer: alles unter /opt/smartheat und /var/lib/smartheat/images gehoert uid 0 (root fuehrt es aus bzw. laedt es;
# uid 1000 waere auf dem Geraet der Benutzer pi bzw. die Container). SHG_ROOTFS_CHECK_UID setzt die erwartete uid -
# NUR fuer die Tests (deren Fixtures gehoeren dem Testbenutzer, eine Gegenprobe mit fremdem Eigentuemer geht ohne root
# nur so); customize.sh und post-build.sh rufen dieses Skript ausdruecklich ohne die Variable auf.
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
OWNER_UID="${SHG_ROOTFS_CHECK_UID:-0}"
[[ $OWNER_UID =~ ^[0-9]+$ ]] || { echo "SHG_ROOTFS_CHECK_UID ungueltig: $OWNER_UID" >&2; exit 2; }
FAIL=0
fail() { echo "FAIL: $1"; FAIL=1; }
WANTS="$R/etc/systemd/system/multi-user.target.wants"
SH="$R/var/lib/smartheat"

# Identitaet: jedes Geraet erzeugt sie selbst beim ersten Start
# Beide machine-ids (systemd und dbus) legen die Pakete schon vor dem customize-Hook an; mmdebstrap setzt
# /etc/machine-id beim Aufraeumen zurueck und loescht /var/lib/dbus/machine-id (Spike 2026-10-07, CI auf dem ARM-Runner).
if [ "$CUSTOMIZE" = 0 ]; then
  mid="$(cat "$R/etc/machine-id" 2>/dev/null || true)"
  case "$mid" in ""|uninitialized) ;; *) fail "machine-id im Image gesetzt" ;; esac
  if [ -f "$R/var/lib/dbus/machine-id" ] && [ ! -L "$R/var/lib/dbus/machine-id" ] \
     && [ -s "$R/var/lib/dbus/machine-id" ]; then
    fail "dbus-machine-id im Image gesetzt"
  fi
fi
if compgen -G "$R/etc/ssh/ssh_host_*" >/dev/null; then fail "SSH-Hostschluessel im Image"; fi
[ -z "$(ls -A "$SH/data" 2>/dev/null)" ] || fail "data/ nicht leer (Geraeteschluessel, Agent-Zustand)"
if compgen -G "$SH/bus/credentials/*/bus.json" >/dev/null; then fail "Bus-Zugangsdaten im Image"; fi
[ ! -e "$SH/bus/mosquitto/passwd" ] || fail "Mosquitto-passwd im Image"
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
# passwd: Passwortfeld x (steht in shadow), * oder gesperrt (!...); leer oder ein Hash waere eine Anmeldung ohne shadow
if [ -r "$R/etc/passwd" ]; then
  while IFS=: read -r user field _; do
    [ -n "$user" ] || continue
    case "$field" in x|'*'|'!'*) ;; *) fail "Benutzer $user: Passwortfeld in etc/passwd leer oder mit Hash" ;; esac
  done <"$R/etc/passwd"
else
  fail "etc/passwd fehlt"
fi

# Eigentuemer und Rechte: root fuehrt den Installer aus und laedt die Archive (Links ausgenommen: immer 777)
for tree in "$R/opt/smartheat" "$SH/images"; do
  rel="${tree#"$R"}"
  if [ ! -d "$tree" ]; then fail "$rel fehlt"; continue; fi
  bad="$(find "$tree" ! -uid "$OWNER_UID" -print -quit 2>/dev/null)"
  if [ -n "$bad" ]; then fail "$rel: ${bad#"$R"} gehoert nicht root (uid $OWNER_UID erwartet)"; fi
  bad="$(find "$tree" ! -type l -perm /022 -print -quit 2>/dev/null)"
  if [ -n "$bad" ]; then fail "$rel: ${bad#"$R"} ist gruppen- oder weltbeschreibbar"; fi
done

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

# Geraete-API und Portal: eigener DNS-Name per https. Genau eine Zuweisung KEY=... am Zeilenanfang; jede andere Zeile,
# die den Schluessel nennt (Leerzeichen, export, Kommentar, doppelt), koennte je nach Leser (Docker, systemd, Shell)
# einen anderen Wert ergeben als den hier geprueften.
ENV_FILE="$SH/host/gateway.env"
for pair in "SHG_DEVICE_API_URL:Geraete-API" "SHG_PORTAL_BASE_URL:Portal"; do
  key="${pair%%:*}" name="${pair#*:}"
  plain="$(grep -c "^$key=" "$ENV_FILE" 2>/dev/null)"
  other="$(grep -F "$key" "$ENV_FILE" 2>/dev/null | grep -cv "^$key=")"
  if [ "${plain:-0}" -gt 1 ] || [ "${other:-0}" -gt 0 ]; then
    fail "$name-Adresse in gateway.env: $key mehrfach oder nicht als $key=... am Zeilenanfang"
  fi
  value="$(sed -n "s/^$key=//p" "$ENV_FILE" 2>/dev/null | head -n 1)"
  if ! why="$(shg_own_url_problem "$value")"; then
    fail "$name-Adresse '${value}' in gateway.env: $why"
  fi
done
exit "$FAIL"
