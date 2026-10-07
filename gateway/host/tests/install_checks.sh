#!/bin/bash
# Laeuft IM Debian-Container (tests/test_gateway_install.sh): Installer zweimal, dann Dateien, Rechte und Syntax pruefen.
set -u
FAIL=0
fail() { echo "FAIL: $1"; FAIL=1; }
# nft -c braucht einen Netlink-Socket; unter QEMU-User-Emulation (CI-Lauf linux/arm64) gibt es den nicht ("Protocol not
# supported"). Dann wird nur diese eine Pruefung uebersprungen, der amd64-Lauf prueft die Regeln. Damit das ehrlich
# bleibt, darf der Skip auf x86_64 nie greifen. Die grep-Pruefungen auf den Inhalt der nftables.conf laufen in beiden
# Faellen.
nft_syntax_check() {  # $1 = Fehlermeldung
  local out
  out="$(nft -c -f /etc/smartheat/nftables.conf 2>&1)" && return 0
  if grep -q 'Protocol not supported' <<<"$out"; then
    if [ "$(uname -m)" = x86_64 ]; then
      echo "$out"; fail "$1 (nft -c darf auf amd64 nicht uebersprungen werden)"; return 0
    fi
    echo "SKIP: nft -c (Netlink im Emulator nicht verfuegbar, amd64-Lauf prueft die Regeln)"
    return 0
  fi
  echo "$out"; fail "$1"
}
SRC=/src/gateway/host
# Release-Bundles bringen manifest.json.minisig mit, die Fixture nicht: die Laeufe mit Pilot-SSH nutzen eine Kopie mit
# Signatur, die ohne Pilot-SSH die Fixture selbst (Signatur optional).
SIGNED_BUNDLE=/tmp/bundle-signed
cp -r "$SRC/tests/fixtures/bundle" "$SIGNED_BUNDLE"
printf 'untrusted comment: signature from minisign secret key\ntest-signatur\n' >"$SIGNED_BUNDLE/manifest.json.minisig"
ARGS=(--no-docker --no-activate --device-api-url https://api.example.test --root /var/lib/smartheat
      --bundle "$SIGNED_BUNDLE" --pilot-ssh "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAItest test-key")
# Aelterer Installer-Stand: smartheat.conf (statt 00-smartheat.conf) muss beim Lauf verschwinden.
mkdir -p /etc/ssh/sshd_config.d && echo "PasswordAuthentication yes" >/etc/ssh/sshd_config.d/smartheat.conf
bash "$SRC/install.sh" "${ARGS[@]}" >/tmp/run1.log 2>&1 || { cat /tmp/run1.log; fail "erster Lauf"; }
snapshot() { find /opt/smartheat /etc/smartheat /etc/systemd/system/smartheat-* /etc/udev/rules.d/99-smartheat-zigbee.rules \
  /etc/systemd/journald.conf.d/smartheat.conf /etc/apt/apt.conf.d/52smartheat-unattended /var/lib/smartheat \
  -exec stat -c '%n %a %u:%g %s %Y' {} + 2>/dev/null | sort; }
snapshot >/tmp/before
sleep 1
bash "$SRC/install.sh" "${ARGS[@]}" >/tmp/run2.log 2>&1 || fail "zweiter Lauf"
snapshot >/tmp/after
diff /tmp/before /tmp/after >/dev/null || { diff /tmp/before /tmp/after; fail "zweiter Lauf hat etwas geaendert"; }
grep -q "Nichts zu tun" /tmp/run2.log || fail "zweiter Lauf meldet Aenderungen"
for unit in updater led hoststatus firewall firstboot; do
  # Nur die fehlende docker.service-Abhaengigkeit (Container ohne Docker) wird ausgefiltert, Syntaxfehler nicht.
  systemd-analyze verify "/etc/systemd/system/smartheat-$unit.service" 2>&1 | grep -v "^$" | grep -vi "docker.service" \
    && fail "Unit smartheat-$unit ungueltig"
done
for unit in updater led hoststatus; do
  grep -q '^Restart=always$' "/etc/systemd/system/smartheat-$unit.service" || fail "Restart= fehlt in smartheat-$unit"
done
for unit in updater hoststatus; do
  grep -q '^EnvironmentFile=/var/lib/smartheat/host/gateway.env$' "/etc/systemd/system/smartheat-$unit.service" \
    || fail "EnvironmentFile= fehlt in smartheat-$unit"
done
for unit in updater led hoststatus; do
  grep -q '^Environment=SHG_ROOT=/var/lib/smartheat$' "/etc/systemd/system/smartheat-$unit.service" || fail "SHG_ROOT fehlt in smartheat-$unit"
  grep -q '@ROOT@' "/etc/systemd/system/smartheat-$unit.service" && fail "@ROOT@ nicht ersetzt in smartheat-$unit"
done
grep -q '^DefaultDependencies=no$' /etc/systemd/system/smartheat-firewall.service || fail "Firewall-Unit ohne DefaultDependencies=no"
grep -q '^After=nftables.service$' /etc/systemd/system/smartheat-firewall.service || fail "Firewall-Unit nicht nach nftables.service"
[ -f /etc/ssh/sshd_config.d/00-smartheat.conf ] || fail "sshd-Drop-in 00-smartheat.conf fehlt"
[ ! -e /etc/ssh/sshd_config.d/smartheat.conf ] || fail "alter sshd-Drop-in smartheat.conf nicht entfernt"
# Sicherheitsupdates (Plan G2b-2 Task 6): Debian-Security und das Raspberry-Pi-Archiv (Kernel, Firmware), sonst nichts;
# kein automatischer Neustart.
patterns="$(apt-config dump | grep 'Unattended-Upgrade::Origins-Pattern::')"
# shellcheck disable=SC2016  # ${distro_codename} bleibt in der apt-Konfiguration woertlich stehen
if [ "$(printf '%s\n' "$patterns" | wc -l)" != 2 ] || ! grep -q 'codename=${distro_codename}-security,' <<<"$patterns" \
   || ! grep -q 'origin=Raspberry Pi Foundation,codename=${distro_codename},label=Raspberry Pi Foundation' <<<"$patterns"; then
  echo "$patterns"; fail "Origins-Pattern nicht genau Debian-Security und Raspberry-Pi-Archiv"
fi
apt-config dump | grep -q '^Unattended-Upgrade::Automatic-Reboot "false";$' || fail "Automatic-Reboot nicht aus"
nft_syntax_check "nftables-Regeln ungueltig"
grep -q "tcp dport 22 accept" /etc/smartheat/nftables.conf || fail "Pilot-SSH fehlt in der Firewall"
if udevadm --help 2>&1 | grep -q verify; then udevadm verify /etc/udev/rules.d/99-smartheat-zigbee.rules || fail "udev-Regel ungueltig"; fi
grep -q "/var/lib/smartheat/host/zigbee_adapter" /etc/udev/rules.d/99-smartheat-zigbee.rules || fail "@ROOT@ nicht ersetzt"
[ "$(stat -c '%u:%g %a' /var/lib/smartheat/bus/credentials/agent)" = "1000:1000 700" ] || fail "bus/credentials/agent"
[ "$(stat -c '%u:%g' /var/lib/smartheat/data)" = "1000:1000" ] || fail "data gehoert nicht uid 1000"
grep -q '^SHG_DEVICE_API_URL=https://api.example.test$' /var/lib/smartheat/host/gateway.env || fail "gateway.env"
grep -q '"current": "0.2.0"' /var/lib/smartheat/updater/state.json || fail "erstes Bundle nicht als current"
for name in docker-compose.yml mosquitto.conf manifest.json manifest.json.minisig; do
  cmp -s "$SIGNED_BUNDLE/$name" "/var/lib/smartheat/bundles/0.2.0/$name" || fail "Bundle-Ordner ohne $name (Spec 2.2)"
done
dpkg-query -W -f='${Status}' ca-certificates 2>/dev/null | grep -q "install ok installed" \
  || fail "ca-certificates nicht installiert (Updater-HTTPS, docker pull)"
grep -q "WARNUNG: ohne --pilot-ssh" /tmp/run1.log && fail "SSH-Warnung trotz --pilot-ssh"
[ "$(stat -c '%a' /root/.ssh/authorized_keys)" = "600" ] || fail "authorized_keys"
PYTHONPATH=/opt/smartheat/host python3 -c "import smartheat_host.updater, smartheat_host.led, smartheat_host.hoststatus, smartheat_host.minisign" \
  || fail "Host-Dienste mit dem System-Python nicht importierbar"
PYTHONPATH=/opt/smartheat/host python3 -c "
from pathlib import Path
from smartheat_host import updater
state = updater.State.load(Path('/var/lib/smartheat/updater/state.json'))
assert state.current == '0.2.0' and state.previous is None and state.rejected == [] and state.in_progress is None, state" \
  || fail "state.json laesst sich nicht mit updater.State.load lesen"
PYTHONPATH=/opt/smartheat/host python3 -c "
from smartheat_host import minisign
try:
    minisign.load_public_key(open('/opt/smartheat/host/release.pub').read())
except minisign.SignatureError as error:
    assert 'Platzhalter' in str(error)
else:
    raise SystemExit('Platzhalter nicht erkannt')" || fail "release.pub-Platzhalter"
# Ohne --pilot-ssh: Drop-in weg, kein Port 22 in der Firewall; danach ist ein weiterer Lauf wieder ein No-op.
ARGS_NOPILOT=(--no-docker --no-activate --device-api-url https://api.example.test --root /var/lib/smartheat
              --bundle "$SRC/tests/fixtures/bundle")
bash "$SRC/install.sh" "${ARGS_NOPILOT[@]}" >/tmp/run3.log 2>&1 || { cat /tmp/run3.log; fail "Lauf ohne Pilot-SSH"; }
[ ! -e /etc/ssh/sshd_config.d/00-smartheat.conf ] || fail "Drop-in ohne --pilot-ssh nicht entfernt"
grep -q "WARNUNG: ohne --pilot-ssh wird ssh deaktiviert" /tmp/run3.log || { cat /tmp/run3.log; fail "keine SSH-Warnung ohne --pilot-ssh"; }
grep -q 'dport 22' /etc/smartheat/nftables.conf && fail "Port 22 ohne --pilot-ssh in der Firewall"
grep -q '@PILOT_SSH@' /etc/smartheat/nftables.conf && fail "@PILOT_SSH@ nicht ersetzt"
nft_syntax_check "nftables-Regeln ohne Pilot ungueltig"
bash "$SRC/install.sh" "${ARGS_NOPILOT[@]}" >/tmp/run4.log 2>&1 || fail "Wiederholung ohne Pilot-SSH"
grep -q "Nichts zu tun" /tmp/run4.log || fail "Wiederholung ohne Pilot-SSH meldet Aenderungen"
# Symlinks unter ROOT (dort schreibt uid 1000): Installer bricht ab und fasst das Ziel nicht an.
SYM_TARGET=/tmp/symlink-target
mkdir -p "$SYM_TARGET" && chmod 0755 "$SYM_TARGET" && echo keep >"$SYM_TARGET/file" && chmod 0644 "$SYM_TARGET/file"
for plant in bus/credentials/agent bus data host/gateway.env; do
  victim=/var/lib/smartheat/$plant
  mv "$victim" "$victim.orig"
  if [ "$plant" = host/gateway.env ]; then ln -s "$SYM_TARGET/file" "$victim"; else ln -s "$SYM_TARGET" "$victim"; fi
  if bash "$SRC/install.sh" "${ARGS_NOPILOT[@]}" >/tmp/sym.log 2>&1; then fail "Symlink $plant: Installer lief durch"; fi
  grep -q "Symlink" /tmp/sym.log || { cat /tmp/sym.log; fail "Symlink $plant: keine klare Fehlermeldung"; }
  [ "$(stat -c '%a %u:%g' "$SYM_TARGET")" = "755 0:0" ] || fail "Symlink $plant: Ziel veraendert"
  if [ "$(stat -c '%a %u:%g' "$SYM_TARGET/file")" != "644 0:0" ] || [ "$(cat "$SYM_TARGET/file")" != keep ]; then
    fail "Symlink $plant: Zieldatei veraendert"
  fi
  rm "$victim"; mv "$victim.orig" "$victim"
done
# Reihenfolgezyklus beim Boot: Units wie "enable" nach multi-user.target.wants verlinken. systemd ordnet ein Target nach
# allen Units, die es per Wants= zieht; "systemd-analyze verify" (ohne gebootetes systemd) bildet das nicht nach, darum
# steht diese implizite Regel hier als Drop-in explizit. Ein Gegenbeispiel (After=multi-user.target bei
# WantedBy=multi-user.target) muss den Test ausloesen, sonst taugt er nichts.
mkdir -p /etc/systemd/system/multi-user.target.wants /etc/systemd/system/multi-user.target.d
for unit in updater led hoststatus firewall firstboot; do
  ln -sf "/etc/systemd/system/smartheat-$unit.service" "/etc/systemd/system/multi-user.target.wants/smartheat-$unit.service"
done
cycle_check() {  # Ausgabe von verify fuer multi-user.target mit der impliziten Reihenfolge fuer die Units $*
  printf '[Unit]\nAfter=%s\n' "$*" >/etc/systemd/system/multi-user.target.d/smartheat-test.conf
  systemd-analyze verify --man=no multi-user.target 2>&1
}
cycle_check smartheat-updater.service smartheat-led.service smartheat-hoststatus.service smartheat-firewall.service \
  smartheat-firstboot.service >/tmp/cycle.log
if grep -qi 'ordering cycle' /tmp/cycle.log; then grep -i cycle /tmp/cycle.log; fail "Ordnungszyklus in den Gateway-Units"; fi
sed 's|^After=local-fs.target|After=multi-user.target|' /etc/systemd/system/smartheat-led.service \
  >/etc/systemd/system/smartheat-cycle.service
ln -sf /etc/systemd/system/smartheat-cycle.service /etc/systemd/system/multi-user.target.wants/smartheat-cycle.service
cycle_check smartheat-cycle.service >/tmp/cycle2.log
grep -qi 'ordering cycle' /tmp/cycle2.log || { cat /tmp/cycle2.log; fail "Zyklus-Test erkennt das Gegenbeispiel nicht"; }
# Aktivierung mit Attrappen fuer systemctl, udevadm, docker und dpkg-query (der Container hat weder systemd noch Docker):
# das laufende Bundle startet nur ohne offenen Updater-Auftrag und mit allen Geraeten aus devices: (Final-Review FW-2,
# FW-10); fehlt eines, sagt der Installer das klar, statt an `docker compose up` abzubrechen.
STUBS=/tmp/stubs
mkdir -p "$STUBS"
for cmd in systemctl udevadm docker; do
  printf '#!/bin/sh\necho "%s $*" >>/tmp/stub.log\n' "$cmd" >"$STUBS/$cmd"
done
printf '#!/bin/sh\necho "install ok installed"\n' >"$STUBS/dpkg-query"  # alle Pakete gelten als installiert
chmod +x "$STUBS"/*
STICK=/tmp/fake-zigbee
ACT_BUNDLE=/tmp/bundle-act
cp -r "$SRC/tests/fixtures/bundle" "$ACT_BUNDLE"
printf '  zigbee2mqtt:\n    image: koenkk/zigbee2mqtt@sha256:%s\n    devices:\n    - %s:/dev/zigbee\n' \
  "$(printf '%064d' 0 | tr 0 b)" "$STICK" >>"$ACT_BUNDLE/docker-compose.yml"
ACT_ROOT=/var/lib/smartheat-act
activate_run() {
  : >/tmp/stub.log
  PATH="$STUBS:$PATH" bash "$SRC/install.sh" --device-api-url https://api.example.test --root "$ACT_ROOT" \
    --bundle "$ACT_BUNDLE" >/tmp/act.log 2>&1 || { cat /tmp/act.log; fail "Aktivierung: $1 bricht ab"; }
}
compose_started() { grep -q "^docker compose -p smartheat .*/bundles/0.2.0/docker-compose.yml up -d" /tmp/stub.log; }
activate_run "ohne Stick"
grep -q "Geraet fehlt: $STICK" /tmp/act.log || { cat /tmp/act.log; fail "Aktivierung ohne Stick: keine klare Meldung"; }
compose_started && fail "Aktivierung ohne Stick: compose up trotzdem aufgerufen"
grep -q "^systemctl enable --now docker.service smartheat-updater.service" /tmp/stub.log || fail "Updater nicht aktiviert"
cmp -s "$SRC/docker-daemon.json" /etc/docker/daemon.json || fail "docker daemon.json (Speicherweg fuer den Erststart) fehlt"
touch "$STICK"
activate_run "mit Stick"
compose_started || { cat /tmp/act.log /tmp/stub.log; fail "Aktivierung mit Stick startet das Bundle nicht"; }
python3 -c '
import json, sys
path = sys.argv[1]
state = json.load(open(path))
state["in_progress"] = {"version": "0.3.0", "previous": "0.2.0", "since": 1.0}
json.dump(state, open(path, "w"))' "$ACT_ROOT/updater/state.json"
activate_run "mit offenem Updater-Auftrag"
grep -q "Updater-Auftrag fuer 0.3.0 offen" /tmp/act.log || { cat /tmp/act.log; fail "offener Auftrag: keine Meldung"; }
compose_started && fail "offener Updater-Auftrag: compose up trotzdem aufgerufen"
# Image-Bau (Plan G2b-2 Task 7): --image aktiviert offline, startet nichts.
# Vorher die Symlinks des Zyklustests (oben) entfernen, sonst wuerde ein "aktiviert" von dort stammen.
rm -f /etc/systemd/system/multi-user.target.wants/smartheat-*.service /etc/systemd/system/multi-user.target.d/*.conf \
  /etc/systemd/system/smartheat-cycle.service
IMAGE_ARGS=("${ARGS[@]/--no-activate/--image}")
bash "$SRC/install.sh" "${IMAGE_ARGS[@]}" >/tmp/run-image.log 2>&1 || { cat /tmp/run-image.log; fail "Lauf mit --image"; }
for unit in smartheat-firewall smartheat-hoststatus smartheat-led ssh; do
  [ -L "/etc/systemd/system/multi-user.target.wants/$unit.service" ] || fail "$unit mit --image nicht aktiviert"
done
# Ohne Docker (--no-docker) bleibt smartheat-firstboot aus: die Unit hat Requires=docker.service.
[ -L /etc/systemd/system/multi-user.target.wants/smartheat-firstboot.service ] \
  && fail "firstboot mit --image --no-docker aktiviert"
grep -q '^ConditionDirectoryNotEmpty=/var/lib/smartheat/images$' /etc/systemd/system/smartheat-firstboot.service \
  || fail "Firstboot-Bedingung fehlt oder @ROOT@ nicht ersetzt"
# Docker-Speicherweg (Plan G2b-2 Task 7): daemon.json liegt VOR der Installation von docker.io, eine abweichende
# bestehende Datei bricht ab und bleibt unberuehrt, identischer Inhalt ist ein No-op.
ORD_STUBS=/tmp/stubs-order
mkdir -p "$ORD_STUBS"
cat >"$ORD_STUBS/dpkg-query" <<'STUB'
#!/bin/sh
case "$*" in *docker.io*|*docker-cli*|*docker-compose*) exit 1 ;; esac
echo "install ok installed"
STUB
cat >"$ORD_STUBS/apt-get" <<'STUB'
#!/bin/sh
if [ -f /etc/docker/daemon.json ]; then state=vorhanden; else state=fehlt; fi
echo "apt-get $* daemon.json=$state" >>/tmp/apt.log
STUB
chmod +x "$ORD_STUBS"/*
ORD_ARGS=(--no-activate --device-api-url https://api.example.test --root /var/lib/smartheat-ord)
rm -rf /etc/docker; : >/tmp/apt.log
PATH="$ORD_STUBS:$STUBS:$PATH" bash "$SRC/install.sh" "${ORD_ARGS[@]}" >/tmp/ord.log 2>&1 || { cat /tmp/ord.log; fail "Docker-Lauf"; }
grep -q 'apt-get install .*docker.io.* daemon.json=vorhanden$' /tmp/apt.log \
  || { cat /tmp/apt.log; fail "daemon.json liegt nicht vor der Installation von docker.io"; }
cmp -s "$SRC/docker-daemon.json" /etc/docker/daemon.json || fail "daemon.json Inhalt"
: >/tmp/apt.log
PATH="$STUBS:$PATH" bash "$SRC/install.sh" "${ORD_ARGS[@]}" >/tmp/ord2.log 2>&1 || fail "zweiter Docker-Lauf"
grep -q "geschrieben: /etc/docker/daemon.json" /tmp/ord2.log && fail "identische daemon.json neu geschrieben"
echo '{"fremd": true}' >/etc/docker/daemon.json
: >/tmp/apt.log
if PATH="$ORD_STUBS:$STUBS:$PATH" bash "$SRC/install.sh" "${ORD_ARGS[@]}" >/tmp/ord3.log 2>&1; then
  fail "abweichende daemon.json: Installer lief durch"
fi
grep -q "bestehende /etc/docker/daemon.json weicht ab" /tmp/ord3.log || { cat /tmp/ord3.log; fail "keine klare Meldung"; }
[ "$(cat /etc/docker/daemon.json)" = '{"fremd": true}' ] || fail "abweichende daemon.json veraendert"
[ -s /tmp/apt.log ] && fail "Pakete trotz abweichender daemon.json installiert"
# Mit --no-docker ignoriert der Installer daemon.json ganz.
PATH="$STUBS:$PATH" bash "$SRC/install.sh" --no-docker "${ORD_ARGS[@]}" >/tmp/ord4.log 2>&1 \
  || { cat /tmp/ord4.log; fail "--no-docker mit fremder daemon.json"; }
rm -rf /etc/docker
# smartheat-firstboot nur mit Docker (Requires=docker.service): --image mit Docker aktiviert es zusammen mit
# docker.service, die Aktivierung ohne Docker kommt ohne die Unit aus, selbst wenn systemctl daran scheitern wuerde.
FAILFB_STUBS=/tmp/stubs-failfb
mkdir -p "$FAILFB_STUBS"
printf '#!/bin/sh\necho "systemctl $*" >>/tmp/stub.log\ncase "$*" in *smartheat-firstboot*) exit 1 ;; esac\n' \
  >"$FAILFB_STUBS/systemctl"
chmod +x "$FAILFB_STUBS/systemctl"
: >/tmp/stub.log
PATH="$STUBS:$PATH" bash "$SRC/install.sh" --image "${ORD_ARGS[@]}" --pilot-ssh "ssh-ed25519 AAAAtest k" \
  >/tmp/img-docker.log 2>&1 || { cat /tmp/img-docker.log; fail "--image mit Docker"; }
grep -q "^systemctl enable docker.service smartheat-updater.service smartheat-firstboot.service" /tmp/stub.log \
  || { cat /tmp/stub.log; fail "--image mit Docker aktiviert firstboot nicht"; }
grep -q "^systemctl enable --now" /tmp/stub.log && fail "--image startet Dienste"
rm -rf /etc/docker
: >/tmp/stub.log
PATH="$FAILFB_STUBS:$STUBS:$PATH" bash "$SRC/install.sh" --no-docker --device-api-url https://api.example.test \
  --root /var/lib/smartheat-nd >/tmp/nodocker.log 2>&1 || { cat /tmp/nodocker.log; fail "Aktivierung mit --no-docker bricht ab"; }
grep -q "firstboot" /tmp/stub.log && fail "--no-docker beruehrt smartheat-firstboot"
grep -q "^systemctl disable --now ssh.service" /tmp/stub.log || fail "--no-docker: ssh-Schritt nach der Aktivierung fehlt"
command -v shellcheck >/dev/null && { shellcheck "$SRC/install.sh" "$SRC/tests/install_checks.sh" || fail "shellcheck"; }
exit $FAIL
