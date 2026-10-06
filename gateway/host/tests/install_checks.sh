#!/bin/bash
# Laeuft IM Debian-Container (tests/test_gateway_install.sh): Installer zweimal, dann Dateien, Rechte und Syntax pruefen.
set -u
FAIL=0
fail() { echo "FAIL: $1"; FAIL=1; }
SRC=/src/gateway/host
ARGS=(--no-docker --no-activate --device-api-url https://api.example.test --root /var/lib/smartheat
      --bundle "$SRC/tests/fixtures/bundle" --pilot-ssh "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAItest test-key")
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
for unit in updater led hoststatus firewall; do
  systemd-analyze verify "/etc/systemd/system/smartheat-$unit.service" 2>&1 | grep -v "^$" | grep -vi "docker.service" \
    && fail "Unit smartheat-$unit ungueltig"
done
nft -c -f /etc/smartheat/nftables.conf || fail "nftables-Regeln ungueltig"
grep -q "tcp dport 22 accept" /etc/smartheat/nftables.conf || fail "Pilot-SSH fehlt in der Firewall"
if udevadm --help 2>&1 | grep -q verify; then udevadm verify /etc/udev/rules.d/99-smartheat-zigbee.rules || fail "udev-Regel ungueltig"; fi
grep -q "/var/lib/smartheat/host/zigbee_adapter" /etc/udev/rules.d/99-smartheat-zigbee.rules || fail "@ROOT@ nicht ersetzt"
[ "$(stat -c '%u:%g %a' /var/lib/smartheat/bus/credentials/agent)" = "1000:1000 700" ] || fail "bus/credentials/agent"
[ "$(stat -c '%u:%g' /var/lib/smartheat/data)" = "1000:1000" ] || fail "data gehoert nicht uid 1000"
grep -q '^SHG_DEVICE_API_URL=https://api.example.test$' /var/lib/smartheat/host/gateway.env || fail "gateway.env"
grep -q '"current": "0.2.0"' /var/lib/smartheat/updater/state.json || fail "erstes Bundle nicht als current"
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
command -v shellcheck >/dev/null && { shellcheck "$SRC/install.sh" "$SRC/tests/install_checks.sh" || fail "shellcheck"; }
exit $FAIL
