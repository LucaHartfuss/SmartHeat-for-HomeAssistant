#!/bin/bash
# Laeuft IM Debian-Container (tests/test_gateway_install.sh) nach install_checks.sh: Pilot-SSH-Tunnel (Plan G2b-2 Task 8)
# mit einem Attrappen-.deb (eigene Pin-Datei in einer Kopie des Installer-Baums) und Schutz vor dem Aussperren.
set -u
FAIL=0
fail() { echo "FAIL: $1"; FAIL=1; }
COPY=/tmp/gw-copy
rm -rf "$COPY" && cp -r /src/gateway "$COPY"
# Attrappe: Paket cloudflared mit /usr/bin/cloudflared, Version wie in der Pin-Datei
version="$(sed -n 's/^VERSION=//p' "$COPY/host/cloudflared.pin")"
arch="$(dpkg --print-architecture)"
mkdir -p /tmp/deb/DEBIAN /tmp/deb/usr/bin
printf 'Package: cloudflared\nVersion: %s\nArchitecture: %s\nMaintainer: test\nDescription: Attrappe\n' "$version" "$arch" \
  >/tmp/deb/DEBIAN/control
printf '#!/bin/sh\necho attrappe\n' >/tmp/deb/usr/bin/cloudflared && chmod 755 /tmp/deb/usr/bin/cloudflared
dpkg-deb --build /tmp/deb /tmp/cloudflared-test.deb >/dev/null
sed -i "s/^SHA256_$arch=.*/SHA256_$arch=$(sha256sum /tmp/cloudflared-test.deb | cut -d' ' -f1)/" "$COPY/host/cloudflared.pin"
printf 'test-token-AAAAAAAAAAAAAAAAAAAAAAAAAAAA\n' >/tmp/token
TUNNEL=("$COPY/host/pilot_ssh_tunnel.sh")

# 0. Die Pin-Datei im Repo hat fuer beide Architekturen einen SHA-256 und dieselbe Version wie das Gateway-Image
for a in arm64 amd64; do
  grep -Eq "^SHA256_$a=[0-9a-f]{64}$" /src/gateway/host/cloudflared.pin || fail "Pin SHA256_$a fehlt oder ist kein SHA-256"
done
grep -q "cloudflare/cloudflared:$(sed -n 's/^VERSION=//p' /src/gateway/host/cloudflared.pin)@" /src/gateway/Dockerfile \
  || fail "cloudflared-Version in der Pin-Datei weicht vom Gateway-Image ab"
# 1. Falsches Paket (Pin passt nicht) wird abgelehnt
cp /tmp/cloudflared-test.deb /tmp/falsch.deb && printf 'x' >>/tmp/falsch.deb
bash "${TUNNEL[@]}" install --token-file /tmp/token --deb /tmp/falsch.deb --no-activate >/tmp/t0.log 2>&1 \
  && fail "Paket mit falschem SHA-256 angenommen"
[ -e /usr/bin/cloudflared ] && fail "falsches Paket trotz Ablehnung installiert"
[ -e /etc/smartheat/pilot-ssh-tunnel.env ] && fail "Token-Datei trotz abgelehntem Paket geschrieben"
# 2. Einrichten
bash "${TUNNEL[@]}" install --token-file /tmp/token --deb /tmp/cloudflared-test.deb --no-activate >/tmp/t1.log 2>&1 \
  || { cat /tmp/t1.log; fail "Tunnel einrichten"; }
[ "$(stat -c '%a %u:%g' /etc/smartheat/pilot-ssh-tunnel.env)" = "600 0:0" ] || fail "Token-Datei nicht 0600 root"
grep -qx 'TUNNEL_TOKEN=test-token-AAAAAAAAAAAAAAAAAAAAAAAAAAAA' /etc/smartheat/pilot-ssh-tunnel.env || fail "Token-Datei"
grep -q 'test-token' /tmp/t1.log && fail "Token im Log"
[ -x /usr/bin/cloudflared ] || fail "cloudflared nicht installiert"
systemd-analyze verify /etc/systemd/system/smartheat-pilot-ssh.service 2>&1 | grep -v "^$" && fail "Tunnel-Unit ungueltig"
grep -q '^ExecStart=/usr/bin/cloudflared --no-autoupdate tunnel run$' /etc/systemd/system/smartheat-pilot-ssh.service \
  || fail "Tunnel-Unit startet nicht cloudflared tunnel run"
grep -q 'docker' /etc/systemd/system/smartheat-pilot-ssh.service && fail "Tunnel-Unit haengt an Docker"
# 3. Zweiter Lauf idempotent
bash "${TUNNEL[@]}" install --token-file /tmp/token --deb /tmp/cloudflared-test.deb --no-activate >/tmp/t2.log 2>&1 \
  || fail "zweiter Lauf"
grep -q "Nichts zu tun" /tmp/t2.log || fail "zweiter Lauf meldet Aenderungen"
# 4. Kaputtes Token wird abgelehnt (mehrzeilig, zu kurz, Sonderzeichen), die bestehende Token-Datei bleibt unveraendert
printf 'zwei\nzeilen\n' >/tmp/token-kaputt
bash "${TUNNEL[@]}" install --token-file /tmp/token-kaputt --deb /tmp/cloudflared-test.deb --no-activate >/dev/null 2>&1 \
  && fail "mehrzeiliges Token angenommen"
printf 'kurz\n' >/tmp/token-kaputt
bash "${TUNNEL[@]}" install --token-file /tmp/token-kaputt --deb /tmp/cloudflared-test.deb --no-activate >/dev/null 2>&1 \
  && fail "zu kurzes Token angenommen"
printf 'test-token-AAAAAAAAAAAAAAAAAAAA;rm -rf\n' >/tmp/token-kaputt
bash "${TUNNEL[@]}" install --token-file /tmp/token-kaputt --deb /tmp/cloudflared-test.deb --no-activate >/dev/null 2>&1 \
  && fail "Token mit Sonderzeichen angenommen"
grep -qx 'TUNNEL_TOKEN=test-token-AAAAAAAAAAAAAAAAAAAAAAAAAAAA' /etc/smartheat/pilot-ssh-tunnel.env \
  || fail "Token-Datei nach abgelehntem Token veraendert"
# 5. Schutz vor dem Aussperren: install.sh ohne --pilot-ssh bricht ab, solange der Tunnel eingerichtet ist.
# Zuerst den Pilot-Zustand herstellen (sshd-Drop-in und Schluessel), der nach dem Abbruch unveraendert bleiben muss.
PILOT_ARGS=(--no-docker --no-activate --device-api-url https://api.example.test --pilot-ssh "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAItest test-key")
bash /src/gateway/host/install.sh "${PILOT_ARGS[@]}" >/tmp/t3a.log 2>&1 || { cat /tmp/t3a.log; fail "Pilot-Zustand herstellen"; }
bash /src/gateway/host/install.sh --no-docker --no-activate --device-api-url https://api.example.test >/tmp/t3.log 2>&1 \
  && fail "install.sh ohne --pilot-ssh trotz Tunnel durchgelaufen"
grep -q "Pilot-SSH-Tunnel ist eingerichtet" /tmp/t3.log || fail "Abbruchtext fehlt"
[ -f /etc/ssh/sshd_config.d/00-smartheat.conf ] || fail "sshd-Drop-in trotz Abbruch entfernt"
grep -q 'tcp dport 22 accept' /etc/smartheat/nftables.conf || fail "Port 22 trotz Abbruch aus der Firewall entfernt"
# mit --pilot-ssh laeuft derselbe Installer weiter durch
bash /src/gateway/host/install.sh "${PILOT_ARGS[@]}" >/tmp/t3b.log 2>&1 || { cat /tmp/t3b.log; fail "install.sh mit --pilot-ssh trotz Tunnel"; }
# 6. Entfernen
bash "${TUNNEL[@]}" remove --no-activate >/tmp/t4.log 2>&1 || fail "Tunnel entfernen"
if [ -e /etc/smartheat/pilot-ssh-tunnel.env ] || [ -e /etc/systemd/system/smartheat-pilot-ssh.service ]; then
  fail "Tunnel nicht entfernt"
fi
bash "${TUNNEL[@]}" remove --no-activate >/tmp/t5.log 2>&1 || fail "zweites Entfernen"
grep -q "Nichts zu tun" /tmp/t5.log || fail "zweites Entfernen meldet Aenderungen"
# nach dem Entfernen darf install.sh wieder ohne --pilot-ssh laufen
bash /src/gateway/host/install.sh --no-docker --no-activate --device-api-url https://api.example.test >/tmp/t6.log 2>&1 \
  || { cat /tmp/t6.log; fail "install.sh ohne --pilot-ssh nach dem Entfernen"; }
command -v shellcheck >/dev/null && {
  shellcheck /src/gateway/host/pilot_ssh_tunnel.sh /src/gateway/host/tests/pilot_ssh_checks.sh || fail "shellcheck"; }
exit $FAIL
