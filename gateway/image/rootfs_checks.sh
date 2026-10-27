#!/bin/bash
# Pruefungen am Root-Dateisystem des Gateway-Images: bricht den Image-Bau ab, wenn ein Geraet aus
# dem Image eine geteilte Identitaet (machine-id, SSH-Hostschluessel, Geraeteschluessel, Bus-Zugangsdaten), ein
# Passwort, ein Tunnel-Token oder SSH bekaeme, wenn der Erststart ohne Pull nicht moeglich waere, wenn
# WLAN an waere, Zeitzone oder Standard-Locale nicht den Vorgaben entsprechen oder die Geraete-API-/Portal-Adresse
# kein eigener DNS-Name per https in der eigenen Zone ist (own_url.sh) oder ss, ip, vcgencmd fehlen (Diagnose), und wenn
# das A/B-System nicht vollstaendig ist (slot-shared.d, werk.mount, /werk, Watchdog, RAUC, Docker-Pins) oder Altlasten
# des Pi-4-Images (usbboot, unattended-upgrades, docker.io) im Image stecken.
# config.txt und cmdline.txt entstehen erst in der Phase preimage und stehen hier noch nicht; sie prueft
# hooks/finalize10-bootfs-checks.
# Aufruf: rootfs_checks.sh ROOTFS [--customize]
#   --customize: Lauf im customize-Hook (customize.sh). Die machine-ids (systemd, dbus) bestehen dort noch;
#                zurueckgesetzt werden sie erst beim Aufraeumen von mmdebstrap, geprueft dann im postbuild-Hook
#                (hooks/postbuild50-rootfs-checks, voller Lauf). Der volle Lauf prueft auch die Docker-Pakete
#                gegen docker-apt/version (neben diesem Skript; im Installer-Baum der Stage fehlt der Ordner).
# Exit 0 = in Ordnung, 1 = Verstoesse (je Zeile "FAIL: ..."), 2 = falscher Aufruf.
# Eigentuemer: alles unter /opt/smartheat gehoert uid 0 (root fuehrt es aus bzw. laedt es; uid 1000 waere auf dem Geraet
# der Benutzer pi bzw. die Container). SHG_ROOTFS_CHECK_UID setzt die erwartete uid -
# NUR fuer die Tests (deren Fixtures gehoeren dem Testbenutzer, eine Gegenprobe mit fremdem Eigentuemer geht ohne root
# nur so); customize.sh und der postbuild-Hook rufen dieses Skript ausdruecklich ohne die Variable auf.
# shellcheck source-path=SCRIPTDIR
set -u
R="${1:?ROOTFS fehlt}"
shift
CUSTOMIZE=0
for arg in "$@"; do
  case "$arg" in
    --customize) CUSTOMIZE=1 ;;
    *) echo "Unbekannte Option: $arg" >&2; exit 2 ;;
  esac
done
# shellcheck source=own_url.sh
. "$(dirname "${BASH_SOURCE[0]}")/own_url.sh" || { echo "FAIL: own_url.sh fehlt"; exit 1; }
OWNER_UID="${SHG_ROOTFS_CHECK_UID:-0}"
[[ $OWNER_UID =~ ^[0-9]+$ ]] || { echo "SHG_ROOTFS_CHECK_UID ungueltig: $OWNER_UID" >&2; exit 2; }
# Zeitzone und Standard-Locale: dieselben Werte wie im Abschnitt locale von config/smartheat-gateway.yaml (der Bau
# setzt sie von dort; test_image_scripts.py prueft die Gleichheit).
SHG_IMAGE_TIMEZONE=Europe/Berlin
SHG_IMAGE_LOCALE=de_DE.UTF-8
SHG_IMAGE_KEYMAP=de
FAIL=0
fail() { echo "FAIL: $1"; FAIL=1; }
WANTS="$R/etc/systemd/system/multi-user.target.wants"
SH="$R/var/lib/smartheat"

# Identitaet: jedes Geraet erzeugt sie selbst beim ersten Start
# Beide machine-ids (systemd und dbus) legen die Pakete schon vor dem customize-Hook an; mmdebstrap setzt
# /etc/machine-id beim Aufraeumen zurueck und loescht /var/lib/dbus/machine-id (Spike 2026-10-07, ARM-Runner).
if [ "$CUSTOMIZE" = 0 ]; then
  mid="$(cat "$R/etc/machine-id" 2>/dev/null || true)"
  case "$mid" in ""|uninitialized) ;; *) fail "machine-id im Image gesetzt" ;; esac
  if [ -f "$R/var/lib/dbus/machine-id" ] && [ ! -L "$R/var/lib/dbus/machine-id" ] \
     && [ -s "$R/var/lib/dbus/machine-id" ]; then
    fail "dbus-machine-id im Image gesetzt"
  fi
fi
if compgen -G "$R/etc/ssh/ssh_host_*" >/dev/null; then fail "SSH-Hostschluessel im Image"; fi
# Der Bau legt unter SHG_ROOT (= daten, ein Bind-Mount ueberdeckt es) nichts ab: weder Geraeteschluessel und Laufzeit-Zustand
# noch Bus-Zugangsdaten, Mosquitto-passwd oder Zigbee2MQTT-Konfiguration; Ordner, Zustand und erstes Bundle legt der erste Start an.
[ -z "$(ls -A "$SH" 2>/dev/null)" ] || fail "var/lib/smartheat nicht leer (der Bau legt unter SHG_ROOT nichts ab)"
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
for tree in "$R/opt/smartheat"; do
  rel="${tree#"$R"}"
  if [ ! -d "$tree" ]; then fail "$rel fehlt"; continue; fi
  bad="$(find "$tree" ! -uid "$OWNER_UID" -print -quit 2>/dev/null)"
  if [ -n "$bad" ]; then fail "$rel: ${bad#"$R"} gehoert nicht root (uid $OWNER_UID erwartet)"; fi
  bad="$(find "$tree" ! -type l -perm /022 -print -quit 2>/dev/null)"
  if [ -n "$bad" ]; then fail "$rel: ${bad#"$R"} ist gruppen- oder weltbeschreibbar"; fi
done

# Hostname: kein statischer Hostname im Image (systemd gibt ihm Vorrang vor dem fluechtigen Namen aus werk.json), der Name
# des Images steht als Rueckfall in os-release (hostname_default.sh)
SHG_IMAGE_HOSTNAME=smartheat-gateway
if [ -e "$R/etc/hostname" ] || [ -L "$R/etc/hostname" ]; then
  fail "etc/hostname im Image (ein statischer Hostname verdraengt den Namen aus werk.json)"
fi
if ! grep -qx "DEFAULT_HOSTNAME=$SHG_IMAGE_HOSTNAME" "$R/usr/lib/os-release" 2>/dev/null; then
  fail "usr/lib/os-release ohne DEFAULT_HOSTNAME=$SHG_IMAGE_HOSTNAME (Rueckfallname des Images)"
fi

# Dienste
for unit in smartheat-ablage smartheat-werk smartheat-firewall smartheat-hoststatus smartheat-led smartheat-firstboot \
            smartheat-updater smartheat-waechter docker; do
  [ -L "$WANTS/$unit.service" ] || fail "$unit nicht aktiviert"
done
# Probestart-Frist (ohne Updater schaltet sie 20 min nach dem Start zurueck, GW 6.3): Timer aktiviert, Programme root-eigen
[ -L "$R/etc/systemd/system/timers.target.wants/smartheat-probestart-frist.timer" ] \
  || fail "smartheat-probestart-frist.timer nicht aktiviert"
for prog in rauc-backend probestart-frist; do
  [ -x "$R/opt/smartheat/$prog" ] || fail "opt/smartheat/$prog fehlt oder ist nicht ausfuehrbar"
done
# Kein SSH im Image (ein SSH-Zugang kommt mit AP-05b). -L statt -e: die Links zeigen absolut ins Image
# (/lib/systemd/...) und waeren vom Build-Host aus oft "kaputt".
if [ -L "$WANTS/ssh.service" ] || [ -e "$WANTS/ssh.service" ]; then fail "ssh im Image aktiviert"; fi
if [ -L "$R/etc/systemd/system/sockets.target.wants/ssh.socket" ]; then
  fail "ssh.socket im Image aktiviert"
fi
for keys in "$R"/root/.ssh/authorized_keys "$R"/home/*/.ssh/authorized_keys; do
  [ ! -s "$keys" ] || fail "authorized_keys im Image (${keys#"$R"})"
done

# Werkseinstellung (unter /opt/smartheat, nicht unter SHG_ROOT): Container-Archive fuer den Erststart ohne Pull, jedes
# nach der SHA-256 seines Inhalts benannt (der Start laedt je Name einmal), und das signierte erste Bundle mit der Vorlage
# der state.json.
WERK="$R/opt/smartheat/werkseinstellung"
if ! compgen -G "$WERK/images/*.tar" >/dev/null; then fail "keine Image-Archive fuer den Erststart"; fi
for archive in "$WERK"/images/*; do
  [ -e "$archive" ] || continue
  name="$(basename "$archive")"
  case "$name" in
    *.tar) ;;
    *) fail "opt/smartheat/werkseinstellung/images/$name ist kein Archiv (<SHA-256>.tar erwartet)"; continue ;;
  esac
  sum="$(sha256sum <"$archive")"
  [ "$name" = "${sum%% *}.tar" ] || fail "Archiv $name heisst nicht nach der SHA-256 seines Inhalts"
done
for name in docker-compose.yml mosquitto.conf manifest.json; do
  [ -s "$WERK/bundle/$name" ] || fail "Werkseinstellung: bundle/$name fehlt"
done
[ -s "$WERK/bundle/manifest.json.sig" ] || fail "Werkseinstellung: erstes Bundle nicht signiert (manifest.json.sig fehlt)"
current="$(sed -n 's/.*"current": *"\([^"]*\)".*/\1/p' "$WERK/state.json" 2>/dev/null)"
version="$(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "$WERK/bundle/manifest.json" 2>/dev/null | head -n 1)"
if [ -z "$current" ] || [ "$current" != "$version" ]; then
  fail "Werkseinstellung: state.json nennt current '${current}' statt der Version '${version}' des ersten Bundles"
fi
# Schluesselbund der Release-Roots: ohne ihn kann der Updater kein Bundle pruefen (install.sh legt ihn ab)
if [ ! -s "$R/opt/smartheat/host/release-roots.pem" ]; then
  fail "release-roots.pem fehlt oder ist leer (opt/smartheat/host)"
fi

# Pi-4-Altlasten: der Start von der SSD am USB (usbboot.sh) und die automatischen Paket-Updates (unattended-upgrades)
# gibt es nicht mehr; der Paketstand aendert sich nur mit einem neuen System-Bundle. Debian-docker.io liefert kein Docker 29.
DPKG_STATUS="$R/var/lib/dpkg/status"
pkg_version() {  # Version des installierten Pakets $1 (leer = nicht installiert)
  awk -v pkg="$1" '
    $1 == "Package:" { cur = $2; installed = 0; version = "" }
    $1 == "Status:" { installed = ($NF == "installed" && $(NF - 1) == "ok") }
    $1 == "Version:" { version = $2 }
    /^$/ { if (cur == pkg && installed) { print version; done = 1; exit } cur = ""; installed = 0 }
    END { if (!done && cur == pkg && installed) print version }' "$DPKG_STATUS" 2>/dev/null
}
[ -r "$DPKG_STATUS" ] || fail "var/lib/dpkg/status fehlt"
for old in "$R/etc/udev/rules.d/99-rpi-01-smartheat-usbboot.rules" "$R/etc/initramfs-tools/hooks/smartheat-usbboot"; do
  [ ! -e "$old" ] || fail "usbboot im Image (${old#"$R"}): der Pi 5 startet von NVMe"
done
for old in unattended-upgrades docker.io docker-compose; do
  [ -z "$(pkg_version "$old")" ] || fail "Paket $old im Image (Release-Takt, Docker aus dem Docker-Archiv)"
done
[ ! -e "$R/etc/apt/apt.conf.d/52smartheat-unattended" ] || fail "52smartheat-unattended im Image (kein unattended-upgrades)"

# A/B-System (GW 6.1, 6.3): Slot-uebergreifende Pfade, Werkspartition, Watchdog, RAUC
SHARED="$R/etc/rpi-image-gen/slot-shared.d/smartheat.conf"
if [ ! -f "$SHARED" ]; then
  fail "slot-shared.d/smartheat.conf fehlt (etc/rpi-image-gen/slot-shared.d)"
else
  grep -qx 'Version=1' "$SHARED" || fail "slot-shared.d/smartheat.conf: Version=1 fehlt"
  for path in /var/lib/smartheat /var/lib/docker /var/lib/containerd /var/lib/rauc /var/lib/systemd/timesync; do
    grep -qx "Path=$path" "$SHARED" || fail "slot-shared.d/smartheat.conf: Path=$path fehlt"
  done
fi
WERK_MOUNT="$R/etc/systemd/system/werk.mount"
if [ ! -f "$WERK_MOUNT" ]; then
  fail "werk.mount fehlt (etc/systemd/system)"
else
  grep -qx 'Where=/werk' "$WERK_MOUNT" || fail "werk.mount: Where=/werk fehlt"
  grep -qx 'What=/dev/disk/by-partlabel/werk' "$WERK_MOUNT" || fail "werk.mount: What=/dev/disk/by-partlabel/werk fehlt"
  options="$(sed -n 's/^Options=//p' "$WERK_MOUNT")"
  for opt in ro nodev nosuid noexec nofail; do
    case ",$options," in *",$opt,"*) ;; *) fail "werk.mount: Option $opt fehlt (Options=$options)" ;; esac
  done
  [ -L "$WANTS/werk.mount" ] || fail "werk.mount nicht aktiviert"
fi
[ -d "$R/werk" ] || fail "/werk fehlt (Mountpunkt im schreibgeschuetzten Root-Dateisystem)"
WATCHDOG="$R/etc/systemd/system.conf.d/smartheat-watchdog.conf"
if [ ! -f "$WATCHDOG" ]; then
  fail "Watchdog-Drop-in fehlt (etc/systemd/system.conf.d/smartheat-watchdog.conf)"
else
  grep -qx 'RuntimeWatchdogSec=10s' "$WATCHDOG" || fail "Watchdog-Drop-in: RuntimeWatchdogSec=10s fehlt"
  grep -qx 'RebootWatchdogSec=5min' "$WATCHDOG" || fail "Watchdog-Drop-in: RebootWatchdogSec=5min fehlt"
fi
if [ ! -s "$R/etc/rauc/system.conf" ]; then
  fail "/etc/rauc/system.conf fehlt oder ist leer"
elif ! grep -qx 'bootname=A' "$R/etc/rauc/system.conf" || ! grep -qx 'bootname=B' "$R/etc/rauc/system.conf"; then
  fail "/etc/rauc/system.conf: bootname=A und bootname=B erwartet"
fi
# rauc.service hat im Paket keinen [Install]-Abschnitt: ab_system.sh haengt sie per add-wants in multi-user.target
[ -L "$WANTS/rauc.service" ] || fail "rauc.service nicht aktiviert (Link in multi-user.target.wants fehlt)"
# rauc-mark-good.service bestaetigte jeden Probestart sofort: maskiert (Link auf /dev/null) oder gar nicht vorhanden
MARK_GOOD="$R/etc/systemd/system/rauc-mark-good.service"
if [ -L "$MARK_GOOD" ] && [ "$(readlink "$MARK_GOOD")" = /dev/null ]; then :
elif [ -e "$MARK_GOOD" ] || [ -L "$MARK_GOOD" ] || [ -e "$R/usr/lib/systemd/system/rauc-mark-good.service" ] \
     || [ -e "$R/lib/systemd/system/rauc-mark-good.service" ]; then
  fail "rauc-mark-good.service nicht maskiert (etc/systemd/system/rauc-mark-good.service -> /dev/null erwartet)"
fi
bad="$(find "$R/etc/systemd/system" -path '*.wants/rauc-mark-good.service' -print -quit 2>/dev/null)"
if [ -n "$bad" ]; then fail "rauc-mark-good.service aktiviert (${bad#"$R"})"; fi
# Docker exakt aus docker-apt/version (FR-34); in der Stage (--customize) liegt der Ordner nicht neben dem Skript
if [ "$CUSTOMIZE" = 0 ]; then
  DOCKER_VERSIONS="$(dirname "${BASH_SOURCE[0]}")/docker-apt/version"
  if [ ! -r "$DOCKER_VERSIONS" ]; then
    fail "docker-apt/version fehlt (neben rootfs_checks.sh)"
  else
    for pkg in docker-ce docker-ce-cli containerd.io docker-compose-plugin; do
      want="$(sed -n "s/^$pkg=//p" "$DOCKER_VERSIONS" | head -n 1)"
      have="$(pkg_version "$pkg")"
      if [ -z "$want" ]; then fail "docker-apt/version: $pkg fehlt"
      elif [ "$have" != "$want" ]; then fail "Paket $pkg: ${have:-nicht installiert} statt $want (docker-apt/version)"
      fi
    done
  fi
fi

# Diagnosewerkzeuge am Geraet: ss und ip (iproute2) in usr/bin oder usr/sbin, vcgencmd (raspi-utils) in usr/bin.
# Ein Link zaehlt (er kann auf den Build-Host-Pfaden "kaputt" zeigen), eine nicht ausfuehrbare Datei nicht.
has_tool() {  # NAME VERZEICHNIS...
  local name="$1" dir
  shift
  for dir in "$@"; do
    if [ -L "$R/$dir/$name" ] || [ -x "$R/$dir/$name" ]; then return 0; fi
  done
  return 1
}
has_tool ss usr/bin usr/sbin || fail "ss fehlt im Image (Paket iproute2)"
has_tool ip usr/bin usr/sbin || fail "ip fehlt im Image (Paket iproute2)"
has_tool vcgencmd usr/bin || fail "vcgencmd fehlt im Image (Paket raspi-utils aus dem Raspberry-Pi-Archiv)"

# Sicherheitsupdates aus dem Raspberry-Pi-Archiv moeglich
grep -rqs "archive.raspberrypi.com" "$R/etc/apt/sources.list.d" "$R/etc/apt/sources.list" \
  || fail "Raspberry-Pi-Archiv nicht eingebunden"

# WLAN aus (wlan_off.sh, nur Ethernet): iwd maskiert und von keinem Ziel gewollt, keine systemd-networkd-Konfiguration
# fuer WLAN. Den WLAN-Chip schaltet dtoverlay=disable-wifi-pi5 in der config.txt ab (hooks/finalize10-bootfs-checks).
if [ "$(readlink "$R/etc/systemd/system/iwd.service" 2>/dev/null)" != /dev/null ]; then
  fail "iwd nicht maskiert (etc/systemd/system/iwd.service -> /dev/null erwartet)"
fi
bad="$(find "$R/etc/systemd/system" \( -path '*.wants/iwd.service' -o -path '*.requires/iwd.service' \) -print -quit \
  2>/dev/null)"
if [ -n "$bad" ]; then fail "iwd aktiviert (${bad#"$R"})"; fi
bad="$(grep -lsE '^[[:space:]]*Name=.*wlan' "$R"/etc/systemd/network/*.network | head -n 1)"
if [ -e "$R/etc/systemd/network/02-wlan0.network" ]; then bad=/etc/systemd/network/02-wlan0.network; fi
if [ -n "$bad" ]; then fail "WLAN-Netzwerkkonfiguration im Image (${bad#"$R"})"; fi

# DHCPv4 auf eth0 mit der MAC als Client-ID (eth_dhcp.sh; mit der DUID kam am Router des Labors kein Angebot): im
# Drop-in gilt die letzte Zuweisung im Abschnitt [DHCPv4].
ETH_DROPIN="$R/etc/systemd/network/01-eth0.network.d/10-smartheat-client-id.conf"
client_id="$(awk '/^[[:space:]]*\[/ { sec = $0; gsub(/[[:space:]]/, "", sec); next }
                  sec == "[DHCPv4]" && /^[[:space:]]*ClientIdentifier[[:space:]]*=/ {
                    v = $0; sub(/^[^=]*=[[:space:]]*/, "", v); sub(/[[:space:]]+$/, "", v) }
                  END { print v }' "$ETH_DROPIN" 2>/dev/null || true)"
if [ "$client_id" != mac ]; then
  fail "DHCPv4-Client-ID auf eth0 nicht mac (${ETH_DROPIN#"$R"}: ${client_id:-fehlt})"
fi

# Zeitzone: /etc/localtime zeigt (absolut oder relativ) auf die Zonendatei, die im Image liegt (tzdata); ein
# vorhandenes /etc/timezone (trixie legt es nicht mehr an) muss dieselbe Zone nennen.
tz_link="$(readlink "$R/etc/localtime" 2>/dev/null || true)"
case "$tz_link" in */zoneinfo/"$SHG_IMAGE_TIMEZONE") tz_ok=1 ;; *) tz_ok=0 ;; esac
if [ "$tz_ok" = 0 ] || [ ! -f "$R/usr/share/zoneinfo/$SHG_IMAGE_TIMEZONE" ]; then
  fail "Zeitzone nicht $SHG_IMAGE_TIMEZONE (/etc/localtime -> ${tz_link:-fehlt}, Zonendatei im Image noetig)"
elif [ -e "$R/etc/timezone" ] && [ "$(tr -d '[:space:]' <"$R/etc/timezone")" != "$SHG_IMAGE_TIMEZONE" ]; then
  fail "Zeitzone: /etc/timezone nennt nicht $SHG_IMAGE_TIMEZONE"
fi

# Standard-Locale: genau LANG=<Locale> in /etc/locale.conf (trixie; /etc/default/locale ist dort ein Link darauf) bzw.
# in /etc/default/locale als eigener Datei. Nichts, was LANG ueberstimmt: kein aktives LC_ALL, kein LC_MESSAGES=C/POSIX,
# LANGUAGE nur mit der Sprache der Locale (z. B. de_DE:de; locale-base schreibt LANGUAGE=C, update-locale --reset
# kommentiert es aus). Die Locale muss erzeugt sein.
locale_problem() {
  local f target lang
  local -a files=()
  for f in etc/locale.conf etc/default/locale; do
    if [ -L "$R/$f" ]; then
      target="$(readlink "$R/$f")"
      case "$target" in ../locale.conf|/etc/locale.conf) continue ;; esac
      echo "/$f ist ein Link auf $target"; return
    fi
    if [ -e "$R/$f" ]; then files+=("$f"); fi
  done
  if [ ${#files[@]} -eq 0 ]; then echo "keine Locale-Datei (/etc/locale.conf)"; return; fi
  for f in "${files[@]}"; do
    lang="$(grep -E '^[[:space:]]*LANG=' "$R/$f")"
    if [ "$lang" != "LANG=$SHG_IMAGE_LOCALE" ]; then echo "/$f: LANG ist nicht genau $SHG_IMAGE_LOCALE"; return; fi
    if grep -qE '^[[:space:]]*LC_ALL=' "$R/$f"; then echo "/$f: LC_ALL ueberstimmt LANG"; return; fi
    if grep -qE '^[[:space:]]*LC_MESSAGES="?(C|POSIX)([.@"].*)?$' "$R/$f"; then
      echo "/$f: LC_MESSAGES=C/POSIX ueberstimmt LANG"; return
    fi
    if grep -E '^[[:space:]]*LANGUAGE=' "$R/$f" | grep -qvE "^[[:space:]]*LANGUAGE=\"?${SHG_IMAGE_LOCALE%%_*}"; then
      echo "/$f: LANGUAGE nennt nicht ${SHG_IMAGE_LOCALE%%_*}"; return
    fi
  done
  if ! grep -qE "^[[:space:]]*${SHG_IMAGE_LOCALE//./\\.}[[:space:]]+UTF-8" "$R/etc/locale.gen" 2>/dev/null; then
    echo "$SHG_IMAGE_LOCALE nicht in /etc/locale.gen (nicht erzeugt)"; return
  fi
  [ -s "$R/usr/lib/locale/locale-archive" ] || echo "keine erzeugten Locales (/usr/lib/locale/locale-archive fehlt)"
}
why="$(locale_problem)"
[ -z "$why" ] || fail "Locale nicht $SHG_IMAGE_LOCALE: $why"
# Tastatur (keyboard-configuration aus der debconf-Vorbelegung von locale-base): genau eine aktive XKBLAYOUT-Zeile.
kbd="$(grep -E '^[[:space:]]*XKBLAYOUT=' "$R/etc/default/keyboard" 2>/dev/null)"
if [ "$kbd" != "XKBLAYOUT=\"$SHG_IMAGE_KEYMAP\"" ]; then
  fail "Tastatur nicht $SHG_IMAGE_KEYMAP (/etc/default/keyboard: ${kbd:-fehlt})"
fi

# Geraete-API und Portal: eigener DNS-Name per https. Genau eine Zuweisung KEY=... am Zeilenanfang; jede andere Zeile,
# die den Schluessel nennt (Leerzeichen, export, Kommentar, doppelt), koennte je nach Leser (Docker, systemd, Shell)
# einen anderen Wert ergeben als den hier geprueften.
ENV_FILE="$R/opt/smartheat/gateway.env"
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
