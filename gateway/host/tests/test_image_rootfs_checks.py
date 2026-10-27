"""rootfs_checks.sh an einem nachgebauten Root-Dateisystem: ein gutes besteht, jede einzelne
Verletzung (geteilte Identitaet, Passwort, SSH im Serien-Image, Tunnel-Token, fehlende Archive, fremde
Geraete-API-Adresse) laesst es scheitern. Dazu die Adress-Regel own_url.sh (Nutzer-Vorgabe 2026-10-07: eigener
DNS-Name per https in der eigenen Zone hartfussha.org, keine IP-Adresse, keine AWS-Adresse) direkt."""
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

IMAGE = Path(__file__).resolve().parents[2] / "image"
HOST = Path(__file__).resolve().parents[1]
SCRIPT = IMAGE / "rootfs_checks.sh"
OWN_URL = IMAGE / "own_url.sh"
WANTS = "etc/systemd/system/multi-user.target.wants"
UNITS = ("smartheat-ablage", "smartheat-werk", "smartheat-firewall", "smartheat-hoststatus", "smartheat-led",
         "smartheat-firstboot", "smartheat-updater", "smartheat-waechter", "docker")
TIMER = "etc/systemd/system/timers.target.wants/smartheat-probestart-frist.timer"
ENV = "opt/smartheat/gateway.env"  # die einzige Quelle fuer Units und Compose (kein host/gateway.env unter SHG_ROOT)
WERK = "opt/smartheat/werkseinstellung"
ARCHIV_A = "a" * 64  # der Name eines Archivs ist die SHA-256 seines Inhalts; die Fixtures tragen den Inhalt dazu
ARCHIV_INHALT = b"archiv-inhalt"
USB_RULE = "etc/udev/rules.d/99-rpi-01-smartheat-usbboot.rules"  # Altlast des Pi-4-Images (usbboot.sh, geloescht)
USB_HOOK = "etc/initramfs-tools/hooks/smartheat-usbboot"
DPKG_STATUS = "var/lib/dpkg/status"
SHARED = "etc/rpi-image-gen/slot-shared.d/smartheat.conf"
WERK_MOUNT = "etc/systemd/system/werk.mount"
WATCHDOG = "etc/systemd/system.conf.d/smartheat-watchdog.conf"
RAUC_CONF = "etc/rauc/system.conf"
MARK_GOOD = "etc/systemd/system/rauc-mark-good.service"
TOOLS = ("ss", "ip", "vcgencmd")
ETH_DROPIN = "etc/systemd/network/01-eth0.network.d/10-smartheat-client-id.conf"
GOOD_SHARED = (
    "Version=1\nPath=/var/lib/smartheat\nPath=/var/lib/docker\nPath=/var/lib/containerd\nPath=/var/lib/rauc\n"
    "Path=/var/lib/systemd/timesync\n"
)
GOOD_WERK_MOUNT = (
    "[Unit]\nDescription=Werk\n\n[Mount]\nWhat=/dev/disk/by-partlabel/werk\nWhere=/werk\nType=ext4\n"
    "Options=ro,nodev,nosuid,noexec,noatime,nofail\n\n[Install]\nWantedBy=multi-user.target\n"
)
GOOD_WATCHDOG = "[Manager]\nRuntimeWatchdogSec=10s\nRebootWatchdogSec=5min\n"
GOOD_RAUC_CONF = "[system]\ncompatible=smartheat-gateway-pi5\n\n[slot.rootfs.0]\nbootname=A\n\n[slot.rootfs.1]\nbootname=B\n"
EMERGENCY_DROPIN = "etc/systemd/system/emergency.service.d/smartheat-probestart.conf"
RESCUE_DROPIN = "etc/systemd/system/rescue.service.d/smartheat-probestart.conf"
GOOD_DROPIN = "[Service]\nExecStartPre=-/opt/smartheat/probestart-frist sofort\n"
DAEMON_JSON = "etc/docker/daemon.json"
GOOD_DAEMON_JSON = (HOST / "docker-daemon.json").read_text()  # die Datei, die install.sh ablegt
DOCKER_PACKAGES = ("docker-ce", "docker-ce-cli", "containerd.io", "docker-compose-plugin")


def _docker_versions() -> dict[str, str]:
    lines = (IMAGE / "docker-apt" / "version").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))


def _dpkg_status(packages: dict[str, str], removed: tuple[str, ...] = ()) -> str:
    """Stanzas wie /var/lib/dpkg/status: installierte Pakete mit Version, entfernte (Konfiguration blieb) ohne Wirkung."""
    stanzas = ["Package: base-files\nStatus: install ok installed\nVersion: 13.8\nArchitecture: arm64\n"]
    for name, version in packages.items():
        stanzas.append(f"Package: {name}\nStatus: install ok installed\nVersion: {version}\nArchitecture: arm64\n")
    for name in removed:
        stanzas.append(f"Package: {name}\nStatus: deinstall ok config-files\nVersion: 1.0\nArchitecture: arm64\n")
    return "\n".join(stanzas)


def _good_dpkg_status() -> str:
    return _dpkg_status({**_docker_versions_without_fingerprint(), "rauc": "1.14-1", "rauc-service": "1.14-1"})


def _docker_versions_without_fingerprint() -> dict[str, str]:
    return {pkg: _docker_versions()[pkg] for pkg in DOCKER_PACKAGES}


GOOD_URLS = [
    "https://accounts.hartfussha.org",
    "https://accounts.hartfussha.org/",
    "https://portal.hartfussha.org",
    "https://a-b.c1.hartfussha.org",
    "https://hartfussha.org",
    "https://Accounts.Hartfussha.ORG",
    "https://ACCOUNTS.HARTFUSSHA.ORG",
]
# Sonst zulaessige DNS-Namen ausserhalb der eigenen Zone (Nutzer-Vorgabe 2026-10-07, Allowlist)
FOREIGN_ZONE_URLS = [
    "https://accounts.example.test",
    "https://portal.example.test",
    "https://a-b.c1.example.org",
    "https://hartfussha.org.evil.com",
    "https://accounts.hartfussha.org.evil.com",
    "https://evilhartfussha.org",
    "https://accounts.evilhartfussha.org",
    "https://hartfussha.com",
    "https://hartfussha.org-evil.com",
]
BAD_URLS = [
    "",
    "http://accounts.hartfussha.org",
    "accounts.hartfussha.org",
    "ftp://accounts.hartfussha.org",
    "https://",
    "https://192.168.2.154",
    "https://192.168.2.154/",
    "https://192.168.2.154:8443",
    "https://127.1",
    "https://2130706433",
    "https://[::1]",
    "https://[2001:db8::1]/",
    "https://localhost",
    "https://accounts.localhost",
    "https://gateway.local",
    "https://accounts",
    "https://user@accounts.hartfussha.org",
    "https://user:pw@accounts.hartfussha.org",
    "https://accounts.hartfussha.org:443",
    "https://accounts.hartfussha.org/api",
    "https://accounts.hartfussha.org/?x=1",
    "https://accounts.hartfussha.org#x",
    "https://accounts.hartfussha.org.",
    "https://-bad.hartfussha.org",
    "https://bad-.hartfussha.org",
    "https://acc ounts.hartfussha.org",
    " https://accounts.hartfussha.org",
    "https://accounts.hartfussha.org\nSHG_ROOT=/",
    "https://abc123.execute-api.eu-central-1.amazonaws.com",
    "https://my-lb-1234.eu-central-1.elb.amazonaws.com",
    "https://ec2-3-120-1-1.eu-central-1.compute.amazonaws.com",
    "https://iot.cn-north-1.amazonaws.com.cn",
    "https://abcdef.eu-central-1.awsapprunner.com",
    "https://d1234abcd.cloudfront.net",
    "https://abc.lambda-url.eu-central-1.on.aws",
    "https://main.d1234.amplifyapp.com",
    "https://app.eu-central-1.elasticbeanstalk.com",
    "https://a1234.awsglobalaccelerator.com",
    "https://accounts.amazonaws.com.hartfussha.org",
    # weitere AWS-Namen und .aws als oberste Ebene
    "https://x.amazonaws.cn",
    "https://myapp.amazonlightsail.com",
    "https://auth.eu-central-1.amazoncognito.com",
    "https://corp.awsapps.com",
    "https://foo.bar.aws",
    "https://abc.api.aws",
    # fremde Platzhalter- und private Namen
    "https://10.0.0.5.nip.io",
    "https://nip.io",
    "https://gw.10-0-0-5.sslip.io",
    "https://gw.127.0.0.1.xip.io",
    "https://api.internal",
    "https://gw.home.arpa",
    "https://home.arpa",
    "https://gw.localdomain",
    "https://gateway.lan",
    "https://5.0.0.10.in-addr.arpa",
    "https://1.0.ip6.arpa",
]
# Nicht-ASCII: Vollbreite (IDNA normalisiert x.amazonaws.ｃｏｍ zu x.amazonaws.com), Umlaute, Vollbreiten-Punkt.
NON_ASCII_URLS = [
    "https://x.amazonaws.\uff43\uff4f\uff4d",
    "https://b\u00fccher.example",
    "https://a.b.\u00e9",
    "https://accounts\uff0ehartfussha.org",
    "https://\uff41ccounts.hartfussha.org",
]
BAD_URLS += NON_ASCII_URLS + FOREIGN_ZONE_URLS


def _locales() -> list[str]:
    """Gesetzte Locale-Namen, die es hier gibt (C immer); UTF-8-Locales lassen Bereiche wie [a-z] sonst Nicht-ASCII
    treffen."""
    try:
        available = subprocess.run(["locale", "-a"], capture_output=True, text=True, check=False).stdout.split()
    except OSError:
        available = []
    normal = {name.lower().replace("-", "") for name in available}
    wanted = ["C.UTF-8", "en_US.UTF-8", "de_DE.UTF-8"]
    return ["C"] + [name for name in wanted if name.lower().replace("-", "") in normal]


LOCALES = _locales()


def _problem(url: str, locale: str | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "LC_ALL": locale} if locale else None
    return subprocess.run(["bash", "-c", 'source "$1" && shg_own_url_problem "$2"', "_", str(OWN_URL), url],
                          capture_output=True, text=True, check=False, env=env)


@pytest.mark.parametrize("url", GOOD_URLS)
def test_own_url_accepts_an_own_dns_name_per_https(url):
    result = _problem(url)
    assert result.returncode == 0 and result.stdout == "", result.stdout


@pytest.mark.parametrize("url", BAD_URLS)
def test_own_url_rejects_ip_aws_and_everything_but_https_with_a_dns_name(url):
    result = _problem(url)
    assert result.returncode == 1 and result.stdout.strip(), url


@pytest.mark.parametrize("url", FOREIGN_ZONE_URLS)
def test_own_url_names_the_own_zone_for_foreign_names(url):
    result = _problem(url)
    assert result.returncode == 1 and "Zone hartfussha.org" in result.stdout, result.stdout


@pytest.mark.parametrize("url", [
    "https://accounts.hartfussha.org.",  # Punkt am Ende: weiterhin abgelehnt (kein vollstaendiger Name)
    "https://x.amazonaws.com.hartfussha.org",  # Denylist bleibt (Tiefenverteidigung), auch in der eigenen Zone
    "https://192.168.2.154",
])
def test_other_rules_still_win_before_the_zone(url):
    result = _problem(url)
    assert result.returncode == 1 and "Zone" not in result.stdout, result.stdout


def test_own_zones_are_defined_once_in_own_url_sh():
    """Eine Stelle fuer die Regel: SHG_OWN_ZONES in own_url.sh, Hinweistext fuer Meldungen aus shg_own_url_hint."""
    script = 'source "$1"; printf "%s|" "${SHG_OWN_ZONES[@]}"; echo; shg_own_url_hint'
    result = subprocess.run(["bash", "-c", script, "_", str(OWN_URL)], capture_output=True, text=True, check=False)
    zones, hint = result.stdout.splitlines()
    assert zones == "hartfussha.org|"
    assert "hartfussha.org" in hint and "https://" in hint
    for path in IMAGE.glob("*.sh"):
        if path.name != "own_url.sh":
            assert "hartfussha" not in path.read_text(), path


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("url", NON_ASCII_URLS + ["https://x.amazonaws.com", "https://192.168.2.154",
                                                  "https://accounts.example.test"])
def test_own_url_rule_does_not_depend_on_the_locale(url, locale):
    assert _problem(url, locale).returncode == 1, (url, locale)


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("url", GOOD_URLS)
def test_own_url_accepts_good_urls_in_every_locale(url, locale):
    assert _problem(url, locale).returncode == 0, (url, locale)


def test_own_url_restores_the_callers_locale():
    script = 'source "$1"; shg_own_url_problem https://accounts.hartfussha.org; printf %s "$LC_ALL"'
    result = subprocess.run(["bash", "-c", script, "_", str(OWN_URL)], capture_output=True, text=True, check=False,
                            env={**os.environ, "LC_ALL": "C.UTF-8"})
    assert result.stdout == "C.UTF-8"


def _archiv_name(inhalt: bytes) -> str:
    return hashlib.sha256(inhalt).hexdigest() + ".tar"


def _good(root: Path) -> Path:
    files = {
        "etc/machine-id": "",
        "etc/shadow": "root:*:20000:0:99999:7:::\npi:!:20000:0:99999:7:::\n",
        "etc/passwd": "root:x:0:0:root:/root:/bin/bash\npi:x:1000:1000::/home/pi:/bin/bash\n"
                      "nobody:*:65534:65534::/nonexistent:/usr/sbin/nologin\nlocked:!x:1001:1001::/:/bin/false\n",
        "etc/apt/sources.list.d/raspi.sources": "URIs: http://archive.raspberrypi.com/debian/\n",
        ENV: "SHG_ROOT=/var/lib/smartheat\nSHG_DEVICE_API_URL=https://accounts.hartfussha.org\n"
             "SHG_PORTAL_BASE_URL=https://portal.hartfussha.org\nTZ=Europe/Berlin\n",
        # Werkseinstellung: erstes Bundle (signiert) und state.json-Vorlage; die Archive schreibt der Test unten
        f"{WERK}/state.json": '{"current": "0.3.0", "in_progress": null}',
        f"{WERK}/bundle/manifest.json": '{"version": "0.3.0"}',
        f"{WERK}/bundle/manifest.json.sig": "sig",
        f"{WERK}/bundle/docker-compose.yml": "services: {}\n",
        f"{WERK}/bundle/mosquitto.conf": "listener 1883\n",
        "usr/lib/os-release": "ID=debian\nDEFAULT_HOSTNAME=smartheat-gateway\n",  # kein etc/hostname (hostname_default.sh)
        "opt/smartheat/rauc-backend": "#!/bin/sh\n",
        "opt/smartheat/probestart-frist": "#!/bin/sh\n",
        EMERGENCY_DROPIN: GOOD_DROPIN,
        RESCUE_DROPIN: GOOD_DROPIN,
        DAEMON_JSON: GOOD_DAEMON_JSON,
        "opt/smartheat/host/release-roots.pem": "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n",
        "home/pi/.ssh/authorized_keys": "",
        "opt/smartheat/installer/gateway/host/install.sh": "#!/bin/bash\n",
        "opt/smartheat/host/VERSION": "0.3.0\n",
        DPKG_STATUS: _good_dpkg_status(),
        # A/B-System: slot-uebergreifende Pfade, Werkspartition, Watchdog, RAUC
        SHARED: GOOD_SHARED,
        WERK_MOUNT: GOOD_WERK_MOUNT,
        WATCHDOG: GOOD_WATCHDOG,
        RAUC_CONF: GOOD_RAUC_CONF,
        # Zeitzone und Locale (locale_default.sh, Layer locale-base/locale-gen): so sieht es nach dem Bau aus
        "usr/share/zoneinfo/Europe/Berlin": "TZif",
        "etc/locale.conf": "LANG=de_DE.UTF-8\n#LANGUAGE=C\n",
        "etc/locale.gen": "# en_GB.UTF-8 UTF-8\nde_DE.UTF-8 UTF-8\n",
        "usr/lib/locale/locale-archive": "archive",
        "etc/default/keyboard": 'XKBMODEL="pc105"\nXKBLAYOUT="de"\nXKBVARIANT=""\nXKBOPTIONS=""\n',
        # WLAN aus (wlan_off.sh): nur die Ethernet-Konfiguration (den Chip schaltet ein Overlay in config.txt ab)
        "etc/systemd/network/01-eth0.network": "[Match]\nName=eth0\n\n[Network]\nDHCP=yes\n",
        # DHCPv4 mit der MAC als Client-ID (eth_dhcp.sh)
        ETH_DROPIN: "# Kommentar\n[DHCPv4]\nClientIdentifier=mac\n",
        # Diagnosewerkzeuge: ss und ip aus iproute2, vcgencmd aus raspi-utils
        "usr/bin/ss": "#!/bin/sh\n",
        "usr/bin/ip": "#!/bin/sh\n",
        "usr/bin/vcgencmd": "#!/bin/sh\n",
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    (root / WERK / "images").mkdir(parents=True)
    (root / WERK / "images" / _archiv_name(ARCHIV_INHALT)).write_bytes(ARCHIV_INHALT)
    (root / "var/lib/smartheat").mkdir(parents=True)  # der Mountpunkt: leer, der Bau legt dort nichts ab
    for programm in ("rauc-backend", "probestart-frist"):
        (root / "opt/smartheat" / programm).chmod(0o755)
    (root / TIMER).parent.mkdir(parents=True)
    (root / TIMER).symlink_to("/lib/systemd/system/smartheat-probestart-frist.timer")
    (root / WANTS).mkdir(parents=True)
    (root / "werk").mkdir()
    (root / WANTS / "werk.mount").symlink_to("../werk.mount")
    (root / WANTS / "rauc.service").symlink_to("/lib/systemd/system/rauc.service")
    (root / MARK_GOOD).symlink_to("/dev/null")
    for tool in TOOLS:
        (root / "usr/bin" / tool).chmod(0o755)
    (root / "etc/systemd/system/iwd.service").symlink_to("/dev/null")
    (root / "etc/localtime").symlink_to("/usr/share/zoneinfo/Europe/Berlin")
    (root / "etc/default").mkdir(parents=True, exist_ok=True)
    (root / "etc/default/locale").symlink_to("../locale.conf")
    for unit in UNITS:
        (root / WANTS / f"{unit}.service").symlink_to(f"/lib/systemd/system/{unit}.service")
    for path in [root / "opt/smartheat", *(root / "opt/smartheat").rglob("*")]:  # wie im Image: nicht gruppen-/weltbeschreibbar
        path.chmod(path.stat().st_mode & ~0o022)
    return root


def _run(root: Path, *extra: str, owner_uid: int | None = None) -> subprocess.CompletedProcess:
    """Die Fixtures gehoeren dem Testbenutzer: SHG_ROOTFS_CHECK_UID (nur fuer Tests) setzt den erwarteten Eigentuemer
    statt root."""
    uid = os.getuid() if owner_uid is None else owner_uid
    return subprocess.run(["bash", str(SCRIPT), str(root), *extra], capture_output=True, text=True, check=False,
                          env={**os.environ, "SHG_ROOTFS_CHECK_UID": str(uid)})


def test_good_rootfs_passes(tmp_path):
    result = _run(_good(tmp_path / "serial"))
    assert result.returncode == 0, result.stdout


def test_uninitialized_machine_id_is_fine(tmp_path):
    root = _good(tmp_path)
    (root / "etc/machine-id").write_text("uninitialized\n")
    assert _run(root).returncode == 0


@pytest.mark.parametrize("breakit", [
    lambda r: (r / "etc/machine-id").write_text("0123456789abcdef0123456789abcdef\n"),
    lambda r: (r / "var/lib/dbus").mkdir(parents=True) or (r / "var/lib/dbus/machine-id").write_text("0123abcd\n"),
    lambda r: (r / "etc/ssh").mkdir() or (r / "etc/ssh/ssh_host_ed25519_key").write_text("k"),
    lambda r: (r / "var/lib/smartheat/data/device").mkdir(parents=True),
    lambda r: (r / "var/lib/smartheat/bus/credentials/runtime").mkdir(parents=True)
    or (r / "var/lib/smartheat/bus/credentials/runtime/bus.json").write_text("{}"),
    lambda r: (r / "var/lib/smartheat/zigbee2mqtt").mkdir()
    or (r / "var/lib/smartheat/zigbee2mqtt/configuration.yaml").write_text("x"),
    lambda r: (r / "var/lib/smartheat/images").mkdir(),
    lambda r: (r / "var/lib/smartheat/updater").mkdir() or (r / "var/lib/smartheat/updater/state.json").write_text("{}"),
    lambda r: (r / "var/lib/smartheat/host").mkdir() or (r / "var/lib/smartheat/host/gateway.env").write_text("x"),
    lambda r: (r / "etc/shadow").write_text("root:*:1::::::\npi::1::::::\n"),
    lambda r: (r / "etc/shadow").write_text("root:$y$j9T$abc:1::::::\n"),
    lambda r: (r / "etc/shadow").unlink(),
    lambda r: (r / WANTS / "ssh.service").symlink_to("/lib/systemd/system/ssh.service"),
    lambda r: (r / "etc/systemd/system/sockets.target.wants").mkdir(parents=True)
    or (r / "etc/systemd/system/sockets.target.wants/ssh.socket").symlink_to("/lib/systemd/system/ssh.socket"),
    lambda r: (r / "root/.ssh").mkdir(parents=True) or (r / "root/.ssh/authorized_keys").write_text("ssh-ed25519 A"),
    lambda r: (r / "home/pi/.ssh/authorized_keys").write_text("ssh-ed25519 AAAA test-key\n"),
    lambda r: (r / "etc/smartheat").mkdir() or (r / "etc/smartheat/pilot-ssh-tunnel.env").write_text("TUNNEL_TOKEN=x"),
    lambda r: (r / WERK / "images" / _archiv_name(ARCHIV_INHALT)).unlink(),
    lambda r: (r / WERK / "images" / ("0" * 64 + ".tar")).write_bytes(b"anderer inhalt"),
    lambda r: (r / WERK / "images" / "01.tar").write_bytes(b"x"),
    lambda r: (r / WERK / "images" / "README").write_text("x"),
    lambda r: (r / WERK / "bundle/manifest.json.sig").unlink(),
    lambda r: (r / WERK / "bundle/manifest.json").unlink(),
    lambda r: (r / WERK / "bundle/docker-compose.yml").unlink(),
    lambda r: (r / WERK / "bundle/mosquitto.conf").unlink(),
    lambda r: (r / WERK / "state.json").unlink(),
    lambda r: (r / WERK / "state.json").write_text('{"current": "0.9.9"}'),
    lambda r: (r / "opt/smartheat/rauc-backend").chmod(0o644),
    lambda r: (r / "opt/smartheat/probestart-frist").unlink(),
    lambda r: (r / TIMER).unlink(),
    lambda r: (r / EMERGENCY_DROPIN).unlink(),
    lambda r: (r / RESCUE_DROPIN).unlink(),
    lambda r: (r / EMERGENCY_DROPIN).write_text("[Service]\nExecStartPre=-/opt/smartheat/probestart-frist\n"),
    lambda r: (r / RESCUE_DROPIN).write_text("[Service]\n# ExecStartPre=-/opt/smartheat/probestart-frist sofort\n"),
    lambda r: (r / DAEMON_JSON).unlink(),
    lambda r: (r / DAEMON_JSON).write_text('{"features": {"containerd-snapshotter": true}}'),
    lambda r: (r / DAEMON_JSON).write_text('{"default-address-pools": [{"base": "172.30.0.0/16", "size": 24}]}'),
    lambda r: (r / DAEMON_JSON).write_text(GOOD_DAEMON_JSON.replace("172.30.0.0/16", "172.17.0.0/16")),
    lambda r: (r / "etc/hostname").write_text("smartheat-gateway\n"),
    lambda r: (r / "etc/hostname").symlink_to("/persistent/hostname"),
    lambda r: (r / "usr/lib/os-release").write_text("ID=debian\n"),
    lambda r: (r / "usr/lib/os-release").write_text("ID=debian\nDEFAULT_HOSTNAME=localhost\n"),
    lambda r: (r / "usr/lib/os-release").unlink(),
    lambda r: (r / WANTS / "smartheat-ablage.service").unlink(),
    lambda r: (r / WANTS / "smartheat-werk.service").unlink(),
    lambda r: (r / "opt/smartheat/host/release-roots.pem").unlink(),
    lambda r: (r / "opt/smartheat/host/release-roots.pem").write_text(""),
    lambda r: (r / WANTS / "smartheat-updater.service").unlink(),
    lambda r: (r / WANTS / "docker.service").unlink(),
    lambda r: (r / "etc/apt/sources.list.d/raspi.sources").unlink(),
    lambda r: (r / ENV).write_text("SHG_DEVICE_API_URL=http://x\nSHG_PORTAL_BASE_URL=https://portal.hartfussha.org\n"),
    lambda r: (r / ENV).unlink(),
    lambda r: (r / "etc/passwd").write_text("root:x:0:0::/root:/bin/bash\npi::1000:1000::/home/pi:/bin/bash\n"),
    lambda r: (r / "etc/passwd").write_text("root:$6$salt$hash:0:0::/root:/bin/bash\n"),
    lambda r: (r / "etc/passwd").unlink(),
    lambda r: (r / "var/lib/smartheat/bus/mosquitto").mkdir(parents=True)
    or (r / "var/lib/smartheat/bus/mosquitto/passwd").write_text("runtime:$7$x\n"),
    lambda r: (r / "opt/smartheat/installer/gateway/host/install.sh").chmod(0o666),
    lambda r: (r / "opt/smartheat/installer").chmod(0o775),
    lambda r: (r / WERK / "images" / _archiv_name(ARCHIV_INHALT)).chmod(0o664),
    lambda r: (r / WERK / "images").chmod(0o777),
    lambda r: __import__("shutil").rmtree(r / "opt/smartheat"),
])
def test_each_violation_fails(tmp_path, breakit):
    root = _good(tmp_path)
    breakit(root)
    result = _run(root)
    assert result.returncode != 0 and "FAIL:" in result.stdout


@pytest.mark.parametrize("breakit", [
    lambda r: (r / "opt/smartheat/host/release-roots.pem").unlink(),
    lambda r: (r / "opt/smartheat/host/release-roots.pem").write_text(""),
], ids=["fehlt", "leer"])
def test_a_missing_release_roots_bundle_is_the_only_problem(tmp_path, breakit):
    root = _good(tmp_path)
    assert _run(root).returncode == 0
    breakit(root)
    result = _run(root)
    fails = [line for line in result.stdout.splitlines() if line.startswith("FAIL:")]
    assert result.returncode != 0 and len(fails) == 1 and "release-roots.pem" in fails[0], result.stdout


def _relink(path: Path, target: str) -> None:
    path.unlink()
    path.symlink_to(target)


@pytest.mark.parametrize("breakit, problem", [
    (lambda r: _relink(r / "etc/localtime", "/usr/share/zoneinfo/Europe/London"), "Zeitzone"),
    (lambda r: _relink(r / "etc/localtime", "/usr/share/zoneinfo/Etc/UTC"), "Zeitzone"),
    (lambda r: (r / "etc/localtime").unlink(), "Zeitzone"),
    (lambda r: _relink(r / "etc/localtime", "/usr/share/zoneinfo/Europe/Berlin2"), "Zeitzone"),
    (lambda r: (r / "usr/share/zoneinfo/Europe/Berlin").unlink(), "Zeitzone"),
    (lambda r: (r / "etc/timezone").write_text("Europe/London\n"), "Zeitzone"),
    (lambda r: (r / "etc/locale.conf").write_text("LANG=C.UTF-8\nLANGUAGE=C\n"), "Locale"),
    (lambda r: (r / "etc/locale.conf").write_text("LANG=en_GB.UTF-8\n"), "Locale"),
    (lambda r: (r / "etc/locale.conf").write_text("#LANG=de_DE.UTF-8\n"), "Locale"),
    (lambda r: (r / "etc/locale.conf").write_text("LANG=de_DE.UTF-8\nLC_ALL=C\n"), "Locale"),
    (lambda r: (r / "etc/locale.conf").write_text("LANG=de_DE.UTF-8\nLANG=C.UTF-8\n"), "Locale"),
    (lambda r: (r / "etc/locale.conf").unlink() or (r / "etc/default/locale").unlink(), "Locale"),
    (lambda r: (r / "etc/default/locale").unlink() or (r / "etc/default/locale").write_text("LANG=C.UTF-8\n"),
     "Locale"),
    (lambda r: (r / "etc/locale.gen").write_text("en_GB.UTF-8 UTF-8\n# de_DE.UTF-8 UTF-8\n"), "Locale"),
    (lambda r: (r / "usr/lib/locale/locale-archive").unlink(), "Locale"),
    (lambda r: (r / "etc/locale.conf").write_text("LANG=de_DE.UTF-8\nLANGUAGE=C\n"), "LANGUAGE"),
    (lambda r: (r / "etc/locale.conf").write_text("LANG=de_DE.UTF-8\nLANGUAGE=en_GB:en\n"), "LANGUAGE"),
    (lambda r: (r / "etc/locale.conf").write_text("LANG=de_DE.UTF-8\nLANGUAGE=\n"), "LANGUAGE"),
    (lambda r: (r / "etc/locale.conf").write_text("LANG=de_DE.UTF-8\nLC_ALL=POSIX\n"), "LC_ALL"),
    (lambda r: (r / "etc/locale.conf").write_text("LANG=de_DE.UTF-8\nLC_MESSAGES=C\n"), "LC_MESSAGES"),
    (lambda r: (r / "etc/locale.conf").write_text("LANG=de_DE.UTF-8\nLC_MESSAGES=\"POSIX\"\n"), "LC_MESSAGES"),
    (lambda r: (r / "etc/default/locale").unlink()
     or (r / "etc/default/locale").write_text("LANG=de_DE.UTF-8\nLANGUAGE=C\n"), "LANGUAGE"),
    (lambda r: (r / "etc/default/keyboard").write_text('XKBMODEL="pc105"\nXKBLAYOUT="gb"\n'), "Tastatur"),
    (lambda r: (r / "etc/default/keyboard").write_text('#XKBLAYOUT="de"\nXKBLAYOUT="us"\n'), "Tastatur"),
    (lambda r: (r / "etc/default/keyboard").unlink(), "Tastatur"),
], ids=["london", "utc", "no-localtime", "berlin2", "no-zonefile", "etc-timezone-london", "lang-c", "lang-en",
        "lang-commented", "lc-all", "lang-twice", "no-locale-file", "default-locale-c", "not-generated", "no-archive",
        "language-c", "language-en", "language-empty", "lc-all-posix", "lc-messages-c", "lc-messages-posix-quoted",
        "default-locale-language-c", "keyboard-gb", "keyboard-us", "no-keyboard"])
def test_timezone_and_default_locale_are_checked(tmp_path, breakit, problem):
    """Zeitzone Europe/Berlin und Standard-Locale de_DE.UTF-8 (config/smartheat-gateway.yaml, Abschnitt locale):
    jede Abweichung allein laesst die Pruefung scheitern, mit genau einer passenden Meldung."""
    root = _good(tmp_path)
    assert _run(root).returncode == 0
    breakit(root)
    result = _run(root)
    fails = [line for line in result.stdout.splitlines() if line.startswith("FAIL:")]
    assert result.returncode != 0 and len(fails) == 1 and problem in fails[0], result.stdout


@pytest.mark.parametrize("fixit", [
    lambda r: _relink(r / "etc/localtime", "../usr/share/zoneinfo/Europe/Berlin"),
    lambda r: (r / "etc/timezone").write_text("Europe/Berlin\n"),
    lambda r: (r / "etc/default/locale").unlink(),
    lambda r: (r / "etc/default/locale").unlink() or (r / "etc/default/locale").write_text("LANG=de_DE.UTF-8\n"),
    lambda r: (r / "etc/locale.conf").write_text("# Kommentar\nLANG=de_DE.UTF-8\nLANGUAGE=de_DE:de\n"),
    lambda r: (r / "etc/locale.conf").write_text(
        "LANG=de_DE.UTF-8\n# LANGUAGE=C\n#LC_ALL=C\nLC_MESSAGES=de_DE.UTF-8\n"),
], ids=["relative-link", "etc-timezone-berlin", "no-default-locale", "default-locale-file", "language-de",
        "commented-overrides"])
def test_timezone_and_locale_variants_that_are_fine(tmp_path, fixit):
    root = _good(tmp_path)
    fixit(root)
    result = _run(root)
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize("tool", TOOLS)
def test_missing_diagnosis_tool_fails_with_its_name(tmp_path, tool):
    """ss, ip und vcgencmd muessen im fertigen Image liegen (Labor 2026-10-20): fehlt eines, genau eine
    Meldung mit dem Namen."""
    root = _good(tmp_path)
    assert _run(root).returncode == 0
    (root / "usr/bin" / tool).unlink()
    result = _run(root)
    fails = [line for line in result.stdout.splitlines() if line.startswith("FAIL:")]
    assert result.returncode != 0 and len(fails) == 1 and tool in fails[0], result.stdout


@pytest.mark.parametrize("tool", TOOLS)
def test_diagnosis_tool_that_is_not_executable_fails(tmp_path, tool):
    root = _good(tmp_path)
    (root / "usr/bin" / tool).chmod(0o644)
    result = _run(root)
    assert result.returncode != 0 and tool in result.stdout, result.stdout


@pytest.mark.parametrize("tool", ["ss", "ip"])
def test_ss_and_ip_may_live_in_usr_sbin(tmp_path, tool):
    root = _good(tmp_path)
    (root / "usr/sbin").mkdir(parents=True)
    (root / "usr/bin" / tool).rename(root / "usr/sbin" / tool)
    result = _run(root)
    assert result.returncode == 0, result.stdout


def test_vcgencmd_in_usr_sbin_does_not_count(tmp_path):
    root = _good(tmp_path)
    (root / "usr/sbin").mkdir(parents=True)
    (root / "usr/bin/vcgencmd").rename(root / "usr/sbin/vcgencmd")
    result = _run(root)
    assert result.returncode != 0 and "vcgencmd" in result.stdout, result.stdout


IWD = "etc/systemd/system/iwd.service"


@pytest.mark.parametrize("breakit, problem", [
    (lambda r: (r / IWD).unlink(), "iwd"),
    (lambda r: _relink(r / IWD, "/usr/lib/systemd/system/iwd.service"), "iwd"),
    (lambda r: (r / WANTS / "iwd.service").symlink_to("/usr/lib/systemd/system/iwd.service"), "iwd"),
    (lambda r: (r / "etc/systemd/system/network.target.wants").mkdir()
     or (r / "etc/systemd/system/network.target.wants/iwd.service").symlink_to("/usr/lib/systemd/system/iwd.service"),
     "iwd"),
    (lambda r: (r / "etc/systemd/network/02-wlan0.network").write_text("[Match]\nName=wlan0\n"), "WLAN"),
    (lambda r: (r / "etc/systemd/network/30-funk.network").write_text("[Match]\nName=wlan*\n[Network]\nDHCP=yes\n"),
     "WLAN"),
    (lambda r: (r / "etc/systemd/network/02-wlan0.network").write_text(""), "WLAN"),
], ids=["iwd-not-masked", "iwd-linked", "iwd-wanted", "iwd-wanted-other-target", "wlan0-network", "wlan-glob",
        "empty-wlan0-network"])
def test_wlan_is_off(tmp_path, breakit, problem):
    """Das Gateway laeuft nur am Ethernet (Nutzer-Vorgabe 2026-10-07): jede WLAN-Spur allein laesst die Pruefung
    scheitern, mit genau einer passenden Meldung."""
    root = _good(tmp_path)
    assert _run(root).returncode == 0
    breakit(root)
    result = _run(root)
    fails = [line for line in result.stdout.splitlines() if line.startswith("FAIL:")]
    assert result.returncode != 0 and len(fails) == 1 and problem in fails[0], result.stdout


@pytest.mark.parametrize("fixit", [
    lambda r: (r / "etc/systemd/network/10-eth1.network").write_text("[Match]\nName=eth1\n"),
], ids=["other-ethernet"])
def test_wlan_off_variants_that_are_fine(tmp_path, fixit):
    root = _good(tmp_path)
    fixit(root)
    result = _run(root)
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize("breakit", [
    lambda r: (r / ETH_DROPIN).unlink(),
    lambda r: (r / ETH_DROPIN).write_text("[DHCPv4]\nClientIdentifier=duid\n"),
    lambda r: (r / ETH_DROPIN).write_text("[DHCPv4]\n#ClientIdentifier=mac\n"),
    lambda r: (r / ETH_DROPIN).write_text("[DHCPv6]\nClientIdentifier=mac\n"),
    lambda r: (r / ETH_DROPIN).write_text("[DHCPv4]\nClientIdentifier=mac\nClientIdentifier=duid\n"),
], ids=["missing", "duid", "commented", "wrong-section", "overridden"])
def test_eth0_asks_dhcpv4_with_the_mac(tmp_path, breakit):
    """Ohne MAC als Client-ID bekam das Gateway am Router des Labors kein IPv4 (H2, 2026-10-20): jede Abweichung im
    Drop-in von eth_dhcp.sh laesst die Pruefung mit genau einer Meldung scheitern."""
    root = _good(tmp_path)
    assert _run(root).returncode == 0
    breakit(root)
    result = _run(root)
    fails = [line for line in result.stdout.splitlines() if line.startswith("FAIL:")]
    assert result.returncode != 0 and len(fails) == 1 and "Client-ID" in fails[0], result.stdout


@pytest.mark.parametrize("url", ["https://accounts.hartfussha.org", "https://accounts.hartfussha.org/"])
def test_own_hostname_as_device_api_passes(tmp_path, url):
    root = _good(tmp_path)
    (root / ENV).write_text(f"SHG_DEVICE_API_URL={url}\nSHG_PORTAL_BASE_URL=https://portal.hartfussha.org\n")
    assert _run(root).returncode == 0


@pytest.mark.parametrize("url", [
    "https://192.168.2.154", "https://192.168.2.154:8443", "https://[2001:db8::1]", "https://localhost",
    "https://abc123.execute-api.eu-central-1.amazonaws.com", "https://my-lb-1.eu-central-1.elb.amazonaws.com",
    "https://abc.eu-central-1.awsapprunner.com", "https://accounts.hartfussha.org/api", "https://accounts",
    "https://user@accounts.hartfussha.org", "", "https://accounts.example.test", "https://hartfussha.org.evil.com",
])
def test_device_api_must_be_an_own_dns_name(tmp_path, url):
    root = _good(tmp_path)
    (root / ENV).write_text(f"SHG_DEVICE_API_URL={url}\nSHG_PORTAL_BASE_URL=https://portal.hartfussha.org\n")
    result = _run(root)
    assert result.returncode != 0 and "FAIL: Geraete-API" in result.stdout


@pytest.mark.parametrize("url", ["https://10.0.0.5", "https://portal.cloudfront.net", "", "https://portal.example.test",
                                 "https://evilhartfussha.org"])
def test_portal_must_be_an_own_dns_name(tmp_path, url):
    root = _good(tmp_path)
    (root / ENV).write_text(f"SHG_DEVICE_API_URL=https://accounts.hartfussha.org\nSHG_PORTAL_BASE_URL={url}\n")
    result = _run(root)
    assert result.returncode != 0 and "FAIL: Portal" in result.stdout


@pytest.mark.parametrize("extra", [
    "SHG_DEVICE_API_URL=https://accounts.hartfussha.org\n",  # doppelt, auch mit gleichem Wert
    "SHG_PORTAL_BASE_URL=https://10.0.0.5\n",
    " SHG_DEVICE_API_URL=https://10.0.0.5\n",
    "export SHG_DEVICE_API_URL=https://10.0.0.5\n",
    "SHG_PORTAL_BASE_URL =https://portal.hartfussha.org\n",
    "#SHG_DEVICE_API_URL=https://10.0.0.5\n",
])
def test_gateway_env_needs_exactly_one_plain_assignment_per_address(tmp_path, extra):
    """Doppelte oder anders geschriebene Zuweisungen (Leerzeichen, export, auskommentiert) koennten je nach Leser
    (Docker, systemd, Shell) einen anderen Wert ergeben als den geprueften."""
    root = _good(tmp_path)
    (root / ENV).write_text((root / ENV).read_text() + extra)
    result = _run(root)
    assert result.returncode != 0 and "gateway.env" in result.stdout, result.stdout


def test_owner_other_than_root_fails(tmp_path):
    """Gegenprobe ohne root: erwarteter Eigentuemer != Eigentuemer der Fixtures (im Image: uid 0, Testbenutzer = uid
    1000 = pi auf dem Geraet)."""
    root = _good(tmp_path)
    result = _run(root, owner_uid=os.getuid() + 1)
    assert result.returncode != 0
    assert "/opt/smartheat" in result.stdout


def test_default_expected_owner_is_root(tmp_path):
    if os.getuid() == 0:
        pytest.skip("laeuft als root: die Fixtures gehoeren dann root")
    root = _good(tmp_path)
    env = {k: v for k, v in os.environ.items() if k != "SHG_ROOTFS_CHECK_UID"}
    result = subprocess.run(["bash", str(SCRIPT), str(root)], capture_output=True, text=True, check=False, env=env)
    assert result.returncode != 0 and "nicht root" in result.stdout


def test_invalid_expected_owner_is_rejected(tmp_path):
    root = _good(tmp_path)
    result = subprocess.run(["bash", str(SCRIPT), str(root)], capture_output=True, text=True, check=False,
                            env={**os.environ, "SHG_ROOTFS_CHECK_UID": "root"})
    assert result.returncode == 2


def test_pilot_option_is_gone(tmp_path):
    """Das Pilot-Image (SSH-Schluessel im Image) gibt es bis AP-05b nicht: --pilot ist keine Option mehr."""
    assert _run(_good(tmp_path), "--pilot").returncode == 2


def test_customize_mode_leaves_only_the_machine_ids_to_the_final_check(tmp_path):
    """Im customize-Hook bestehen die machine-ids von systemd und dbus noch; zurueckgesetzt bzw. geloescht werden sie
    erst beim Aufraeumen von mmdebstrap (geprueft im postbuild-Hook). Alles andere prueft auch der customize-Lauf."""
    root = _good(tmp_path)
    (root / "etc/machine-id").write_text("0123456789abcdef0123456789abcdef\n")
    assert _run(root, "--customize").returncode == 0
    assert _run(root).returncode != 0
    (root / "etc/machine-id").write_text("uninitialized\n")
    (root / "var/lib/dbus").mkdir(parents=True)
    (root / "var/lib/dbus/machine-id").write_text("0123456789abcdef0123456789abcdef\n")
    assert _run(root, "--customize").returncode == 0
    assert "dbus-machine-id" in _run(root).stdout
    (root / "etc/ssh").mkdir()
    (root / "etc/ssh/ssh_host_ed25519_key").write_text("k")
    assert _run(root, "--customize").returncode != 0


def test_unknown_option_is_rejected(tmp_path):
    assert _run(_good(tmp_path), "--pilto").returncode == 2


def _postbuild_hook(root: Path, stage: Path, **extra: str) -> subprocess.CompletedProcess:
    """Wie der Runner: der Hook laeuft aus seinem Ordner, Umgebung mit der Stage des Layers."""
    return subprocess.run(["./postbuild50-rootfs-checks", str(root)], capture_output=True, text=True, check=False,
                          cwd=IMAGE / "hooks", env={**os.environ, "IGconf_shg_stage": str(stage), **extra})


def test_postbuild_hook_runs_the_full_check_and_logs_its_lines(tmp_path):
    """Gegenprobe gegen stilles Ignorieren (v2.8.0 uebersah post-build.sh): der Hook schreibt Anfang und Ende in den
    Baulog. Ein gutes Rootfs besteht, ein schlechtes bricht ab (Exit != 0, FAIL-Zeilen im Log, kein 'bestanden')."""
    if os.getuid() == 0:
        pytest.skip("laeuft als root: die Fixtures gehoeren dann root, die Eigentuemer-Probe unten wuerde bestehen")
    stage = tmp_path / "stage"
    stage.mkdir()
    root = _good(tmp_path / "root")
    result = _postbuild_hook(root, stage)
    # die Fixtures gehoeren dem Testbenutzer, der Hook prueft auf root (ohne SHG_ROOTFS_CHECK_UID): genau das ist der Fehler
    assert result.returncode != 0 and "postbuild50-rootfs-checks: pruefe" in result.stdout
    assert "nicht root" in result.stdout and "bestanden" not in result.stdout


def test_postbuild_hook_ignores_the_test_only_owner_override(tmp_path):
    """Ein gesetzter SHG_ROOTFS_CHECK_UID in der Bau-Umgebung schwaecht die Eigentuemer-Pruefung nicht ab."""
    if os.getuid() == 0:
        pytest.skip("laeuft als root: die Fixtures gehoeren dann root")
    stage = tmp_path / "stage"
    stage.mkdir()
    result = _postbuild_hook(_good(tmp_path / "root"), stage, SHG_ROOTFS_CHECK_UID=str(os.getuid()))
    assert result.returncode != 0 and "nicht root" in result.stdout
    assert "env -u SHG_ROOTFS_CHECK_UID" in (IMAGE / "customize.sh").read_text()
    assert "env -u SHG_ROOTFS_CHECK_UID" in (IMAGE / "hooks" / "postbuild50-rootfs-checks").read_text()


def test_postbuild_hook_passes_for_a_good_rootfs_owned_by_root(tmp_path):
    """Der Hook im Erfolgsfall: nur als root pruefbar (ohne den Test-Schalter erwartet die Pruefung uid 0); als
    Testbenutzer ersetzt eine Kopie des Hooks den Aufruf von rootfs_checks.sh durch den Schalter."""
    if os.getuid() == 0:
        root = _good(tmp_path / "root")
        stage = tmp_path / "stage"
        stage.mkdir()
        result = _postbuild_hook(root, stage)
    else:
        copy = tmp_path / "srcroot"
        (copy / "hooks").mkdir(parents=True)
        shutil.copytree(IMAGE / "docker-apt", copy / "docker-apt")
        shutil.copy(IMAGE / "own_url.sh", copy / "own_url.sh")
        script = (IMAGE / "rootfs_checks.sh").read_text().replace('OWNER_UID="${SHG_ROOTFS_CHECK_UID:-0}"',
                                                                  f'OWNER_UID="{os.getuid()}"')
        (copy / "rootfs_checks.sh").write_text(script)
        shutil.copy(IMAGE / "hooks" / "postbuild50-rootfs-checks", copy / "hooks" / "postbuild50-rootfs-checks")
        stage = tmp_path / "stage"
        stage.mkdir()
        result = subprocess.run(["./postbuild50-rootfs-checks", str(_good(tmp_path / "root"))], capture_output=True,
                                text=True, check=False, cwd=copy / "hooks",
                                env={**os.environ, "IGconf_shg_stage": str(stage)})
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines()[0].startswith("postbuild50-rootfs-checks: pruefe ")
    assert result.stdout.rstrip().endswith("postbuild50-rootfs-checks: bestanden")


def test_postbuild_hook_needs_the_stage_variable(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "IGconf_shg_stage"}
    result = subprocess.run(["./postbuild50-rootfs-checks", str(tmp_path)], capture_output=True, text=True,
                            check=False, cwd=IMAGE / "hooks", env=env)
    assert result.returncode != 0 and "IGconf_shg_stage" in result.stderr


# Neue Pruefungen des A/B-Images
def _fail_lines(result: subprocess.CompletedProcess) -> list[str]:
    return [line for line in result.stdout.splitlines() if line.startswith("FAIL:")]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _replace(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, (path, old)
    path.write_text(text.replace(old, new))


@pytest.mark.parametrize("breakit, problem", [
    # Slot-uebergreifende Pfade
    (lambda r: (r / SHARED).unlink(), "slot-shared.d/smartheat.conf fehlt"),
    (lambda r: _replace(r / SHARED, "Version=1\n", ""), "Version=1"),
    (lambda r: _replace(r / SHARED, "Path=/var/lib/smartheat\n", ""), "Path=/var/lib/smartheat fehlt"),
    (lambda r: _replace(r / SHARED, "Path=/var/lib/docker\n", ""), "Path=/var/lib/docker fehlt"),
    (lambda r: _replace(r / SHARED, "Path=/var/lib/containerd\n", ""), "Path=/var/lib/containerd fehlt"),
    (lambda r: _replace(r / SHARED, "Path=/var/lib/rauc\n", ""), "Path=/var/lib/rauc fehlt"),
    (lambda r: _replace(r / SHARED, "Path=/var/lib/systemd/timesync\n", ""), "Path=/var/lib/systemd/timesync fehlt"),
    # Werkspartition
    (lambda r: (r / WERK_MOUNT).unlink(), "werk.mount fehlt"),
    (lambda r: _replace(r / WERK_MOUNT, "Where=/werk", "Where=/mnt"), "Where=/werk"),
    (lambda r: _replace(r / WERK_MOUNT, "by-partlabel/werk", "by-partlabel/daten"), "by-partlabel/werk"),
    (lambda r: _replace(r / WERK_MOUNT, "ro,", ""), "Option ro fehlt"),
    (lambda r: _replace(r / WERK_MOUNT, "nodev,", ""), "Option nodev fehlt"),
    (lambda r: _replace(r / WERK_MOUNT, "nosuid,", ""), "Option nosuid fehlt"),
    (lambda r: _replace(r / WERK_MOUNT, "noexec,", ""), "Option noexec fehlt"),
    (lambda r: _replace(r / WERK_MOUNT, ",nofail", ""), "Option nofail fehlt"),
    (lambda r: (r / WANTS / "werk.mount").unlink(), "werk.mount nicht aktiviert"),
    (lambda r: (r / "werk").rmdir(), "/werk fehlt"),
    # Watchdog
    (lambda r: (r / WATCHDOG).unlink(), "Watchdog-Drop-in fehlt"),
    (lambda r: _replace(r / WATCHDOG, "RuntimeWatchdogSec=10s", "RuntimeWatchdogSec=30s"), "RuntimeWatchdogSec=10s"),
    (lambda r: _replace(r / WATCHDOG, "RebootWatchdogSec=5min", "RebootWatchdogSec=10min"), "RebootWatchdogSec=5min"),
    (lambda r: _replace(r / WATCHDOG, "RebootWatchdogSec=5min\n", ""), "RebootWatchdogSec=5min"),
    # RAUC
    (lambda r: (r / RAUC_CONF).unlink(), "system.conf fehlt"),
    (lambda r: (r / RAUC_CONF).write_text(""), "system.conf fehlt oder ist leer"),
    (lambda r: _replace(r / RAUC_CONF, "bootname=B", "bootname=C"), "bootname=A und bootname=B"),
    (lambda r: (r / WANTS / "rauc.service").unlink(), "rauc.service nicht aktiviert"),
    (lambda r: _relink(r / MARK_GOOD, "/lib/systemd/system/rauc-mark-good.service"), "rauc-mark-good.service nicht maskiert"),
    (lambda r: (r / WANTS / "rauc-mark-good.service").symlink_to("/lib/systemd/system/rauc-mark-good.service"),
     "rauc-mark-good.service aktiviert"),
    # Altlasten des Pi-4-Images
    (lambda r: _write(r / USB_RULE, "# usbboot\n"), "usbboot im Image"),
    (lambda r: _write(r / USB_HOOK, "#!/bin/sh\n"), "usbboot im Image"),
    (lambda r: (r / DPKG_STATUS).write_text(
        (r / DPKG_STATUS).read_text() + "\n" + _dpkg_status({"unattended-upgrades": "2.12"})),
     "Paket unattended-upgrades"),
    (lambda r: (r / DPKG_STATUS).write_text((r / DPKG_STATUS).read_text() + "\n" + _dpkg_status({"docker.io": "26.1.5"})),
     "Paket docker.io"),
    (lambda r: (r / DPKG_STATUS).write_text(
        (r / DPKG_STATUS).read_text() + "\n" + _dpkg_status({"docker-compose": "1.29.2"})), "Paket docker-compose"),
    (lambda r: (r / "etc/apt/apt.conf.d").mkdir(parents=True)
     or (r / "etc/apt/apt.conf.d/52smartheat-unattended").write_text("APT::Periodic::Unattended-Upgrade \"1\";\n"),
     "52smartheat-unattended"),
], ids=["no-shared", "shared-no-version", "shared-no-smartheat", "shared-no-docker", "shared-no-containerd",
        "shared-no-rauc", "shared-no-timesync", "no-werk-mount", "werk-where", "werk-what", "werk-not-ro",
        "werk-no-nodev", "werk-no-nosuid", "werk-no-noexec", "werk-no-nofail", "werk-not-enabled", "no-werk-dir", "no-watchdog",
        "watchdog-30s", "reboot-watchdog-10min", "reboot-watchdog-missing", "no-rauc-conf", "empty-rauc-conf",
        "rauc-bootname", "rauc-not-enabled", "mark-good-linked", "mark-good-enabled",
        "usbboot-rule", "usbboot-hook", "unattended-upgrades", "docker-io", "docker-compose-v1", "apt-conf"])
def test_ab_system_is_checked(tmp_path, breakit, problem):
    """GW 6.1/6.3: jede einzelne Abweichung am A/B-System (oder eine Pi-4-Altlast) laesst die Pruefung mit genau
    einer passenden Meldung scheitern."""
    root = _good(tmp_path)
    assert _run(root).returncode == 0
    breakit(root)
    result = _run(root)
    fails = _fail_lines(result)
    assert result.returncode != 0 and len(fails) == 1 and problem in fails[0], result.stdout


@pytest.mark.parametrize("unit", UNITS)
def test_every_service_of_the_image_must_be_enabled(tmp_path, unit):
    """Jede Unit der Liste (auch der Waechter, GA-09) muss in multi-user.target.wants verlinkt sein; fehlt der Link, scheitert die
    Pruefung mit genau dieser Unit in der Meldung."""
    root = _good(tmp_path)
    assert _run(root).returncode == 0
    (root / WANTS / f"{unit}.service").unlink()
    fails = _fail_lines(_run(root))
    assert fails == [f"FAIL: {unit} nicht aktiviert"], fails


def test_missing_dpkg_status_fails_without_hiding_the_cause(tmp_path):
    """Ohne var/lib/dpkg/status laesst sich kein Paketstand pruefen: die erste Meldung nennt die Datei."""
    root = _good(tmp_path)
    (root / DPKG_STATUS).unlink()
    fails = _fail_lines(_run(root))
    assert fails and "var/lib/dpkg/status fehlt" in fails[0]
    assert all(f.startswith("FAIL: Paket ") or "dpkg/status" in f for f in fails), fails


def test_mark_good_without_mask_fails_when_the_package_ships_the_unit(tmp_path):
    """rauc-mark-good.service fehlt ganz: in Ordnung (Paket ohne die Unit). Liegt die Unit im Paket und die Maske
    fehlt, bestaetigte sie jeden Probestart sofort: Fehler."""
    root = _good(tmp_path)
    (root / MARK_GOOD).unlink()
    assert _run(root).returncode == 0  # Unit nirgends vorhanden
    unit = root / "usr/lib/systemd/system/rauc-mark-good.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\nExecStart=/usr/bin/rauc status mark-good\n")
    result = _run(root)
    fails = _fail_lines(result)
    assert result.returncode != 0 and len(fails) == 1 and "rauc-mark-good.service nicht maskiert" in fails[0]
    (root / "lib/systemd/system").mkdir(parents=True)
    unit.unlink()
    (root / "lib/systemd/system/rauc-mark-good.service").write_text("[Service]\n")
    assert _fail_lines(_run(root))[0].startswith("FAIL: rauc-mark-good.service nicht maskiert")


def test_removed_but_not_purged_packages_do_not_count(tmp_path):
    """Status 'deinstall ok config-files' ist kein installiertes Paket."""
    root = _good(tmp_path)
    (root / DPKG_STATUS).write_text((root / DPKG_STATUS).read_text() + "\n"
                                    + _dpkg_status({}, removed=("unattended-upgrades", "docker.io")))
    assert _run(root).returncode == 0


@pytest.mark.parametrize("package", DOCKER_PACKAGES)
def test_docker_packages_must_match_docker_apt_version(tmp_path, package):
    root = _good(tmp_path)
    versions = _docker_versions_without_fingerprint()
    versions[package] = "5:28.5.2-1~debian.13~trixie"
    (root / DPKG_STATUS).write_text(_dpkg_status({**versions, "rauc": "1.14-1", "rauc-service": "1.14-1"}))
    fails = _fail_lines(_run(root))
    assert len(fails) == 1 and f"Paket {package}: 5:28.5.2-1~debian.13~trixie statt {_docker_versions()[package]}" in fails[0]


@pytest.mark.parametrize("package", DOCKER_PACKAGES)
def test_missing_docker_package_fails(tmp_path, package):
    root = _good(tmp_path)
    versions = _docker_versions_without_fingerprint()
    del versions[package]
    (root / DPKG_STATUS).write_text(_dpkg_status({**versions, "rauc": "1.14-1", "rauc-service": "1.14-1"}))
    fails = _fail_lines(_run(root))
    assert len(fails) == 1 and f"Paket {package}: nicht installiert" in fails[0]


def test_docker_versions_are_not_checked_in_the_customize_run(tmp_path):
    """In der Stage (customize.sh) liegt docker-apt/ nicht neben dem Skript; der volle Lauf im postbuild-Hook prueft
    die Versionen."""
    root = _good(tmp_path)
    (root / DPKG_STATUS).write_text(_dpkg_status({"rauc": "1.14-1"}))
    assert _run(root, "--customize").returncode == 0
    assert _fail_lines(_run(root))  # voller Lauf: alle vier Docker-Pakete fehlen


def test_docker_version_file_must_sit_next_to_the_script(tmp_path):
    copy = tmp_path / "image"
    copy.mkdir()
    shutil.copy(IMAGE / "rootfs_checks.sh", copy / "rootfs_checks.sh")
    shutil.copy(IMAGE / "own_url.sh", copy / "own_url.sh")
    root = _good(tmp_path / "root")
    result = subprocess.run(["bash", str(copy / "rootfs_checks.sh"), str(root)], capture_output=True, text=True,
                            check=False, env={**os.environ, "SHG_ROOTFS_CHECK_UID": str(os.getuid())})
    fails = _fail_lines(result)
    assert result.returncode != 0 and len(fails) == 1 and "docker-apt/version fehlt" in fails[0]


def _finalize(target: Path, **extra: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "IGconf_target_path"}
    env.update(extra)
    if "IGconf_target_path" not in extra:
        env["IGconf_target_path"] = str(target)
    return subprocess.run(["./finalize10-bootfs-checks"], capture_output=True, text=True, check=False,
                          cwd=IMAGE / "hooks", env=env)


GOOD_CMDLINE = "console=serial0,115200 console=tty1 root=/dev/disk/by-slot/active/system fsck.repair=yes rootwait panic=10\n"
# so setzt rpi-image-gen die config.txt aus den Fragmenten zusammen (Kopf, Fragmente, [all] nach einem offenen Filter)
GOOD_CONFIG = (
    "# Assembled by rpi-image-gen from layer fragments.\n# See http://rptl.io/configtxt\n"
    "arm_boost=1\n[pi5]\nBOOT_UART=1\n[all]\n"
    "# SmartHeat-Gateway: nur Ethernet, WLAN- und Bluetooth-Chip des Pi 5 abgeschaltet (Nutzer-Vorgabe 2026-10-07).\n"
    "[pi5]\ndtoverlay=disable-wifi-pi5\ndtoverlay=disable-bt-pi5\ndtoverlay=\n[all]\n"
)


def _boot_tree(tmp_path: Path) -> Path:
    target = tmp_path / "rootfs"
    firmware = target / "boot/firmware"
    (firmware / "overlays").mkdir(parents=True)
    (firmware / "cmdline.txt").write_text(GOOD_CMDLINE)
    (firmware / "config.txt").write_text(GOOD_CONFIG)
    for overlay in ("disable-wifi-pi5", "disable-bt-pi5"):
        (firmware / "overlays" / f"{overlay}.dtbo").write_bytes(b"dtbo")
    return target


def test_finalize_hook_passes_for_the_assembled_boot_files_and_logs_its_lines(tmp_path):
    result = _finalize(_boot_tree(tmp_path))
    assert result.returncode == 0, result.stdout
    lines = result.stdout.splitlines()
    assert lines[0].startswith("finalize10-bootfs-checks: pruefe ") and lines[-1] == "finalize10-bootfs-checks: bestanden"


def _cmdline(text: str):
    return lambda f: (f / "cmdline.txt").write_text(text)


def _config(text: str):
    return lambda f: (f / "config.txt").write_text(text)


WIFI, BT = "dtoverlay=disable-wifi-pi5", "dtoverlay=disable-bt-pi5"


@pytest.mark.parametrize("breakit, problems", [
    (lambda f: (f / "cmdline.txt").unlink(), ("cmdline.txt fehlt",)),
    (_cmdline(GOOD_CMDLINE.replace(" panic=10", "")), ("panic=10 fehlt",)),
    (_cmdline(GOOD_CMDLINE.replace("panic=10", "panic=0")), ("panic=0 (panic=10 erwartet)",)),
    (_cmdline(GOOD_CMDLINE.replace("panic=10", "panic=100")), ("panic=100 (panic=10 erwartet)",)),
    (_cmdline(GOOD_CMDLINE.replace("panic=10", "xpanic=10")), ("panic=10 fehlt",)),
    (_cmdline(GOOD_CMDLINE.replace("by-slot/active/system", "mmcblk0p2")),
     ("root=/dev/disk/by-slot/active/system fehlt",)),
    (lambda f: (f / "config.txt").unlink(), ("config.txt fehlt",)),
    (_config(GOOD_CONFIG.replace(WIFI, "#" + WIFI)), (f"{WIFI} fehlt",)),
    (_config(GOOD_CONFIG.replace(BT, "dtoverlay=disable-bt")), (f"{BT} fehlt",)),
    (_config(GOOD_CONFIG.replace("[pi5]\ndtoverlay", "[pi4]\ndtoverlay")), (f"{WIFI} fehlt", f"{BT} fehlt")),
    (_config("arm_boost=1\n"), (f"{WIFI} fehlt", f"{BT} fehlt")),
    (lambda f: (f / "overlays" / "disable-wifi-pi5.dtbo").unlink(), ("overlays/disable-wifi-pi5.dtbo fehlt",)),
    (lambda f: (f / "overlays" / "disable-bt-pi5.dtbo").unlink(), ("overlays/disable-bt-pi5.dtbo fehlt",)),
], ids=["no-cmdline", "no-panic", "panic-0", "panic-100", "xpanic", "wrong-root", "no-config", "wifi-commented",
        "bt-old-name", "overlays-in-pi4", "no-overlay", "no-wifi-dtbo", "no-bt-dtbo"])
def test_finalize_hook_checks_cmdline_and_config(tmp_path, breakit, problems):
    target = _boot_tree(tmp_path)
    breakit(target / "boot/firmware")
    result = _finalize(target)
    fails = _fail_lines(result)
    assert result.returncode != 0 and len(fails) == len(problems), result.stdout
    assert all(problem in fail for problem, fail in zip(problems, fails, strict=True)), result.stdout
    assert "bestanden" not in result.stdout and result.stdout.rstrip().endswith("FEHLER")


@pytest.mark.parametrize("config", [
    "dtoverlay=disable-wifi-pi5\ndtoverlay=disable-bt-pi5\n",  # vor jedem Abschnitt
    "[all]\n  dtoverlay=disable-wifi-pi5  \ndtoverlay=disable-bt-pi5\n",
])
def test_finalize_hook_accepts_the_overlays_before_sections_and_in_all(tmp_path, config):
    target = _boot_tree(tmp_path)
    (target / "boot/firmware/config.txt").write_text(config)
    assert _finalize(target).returncode == 0


def test_finalize_hook_needs_the_target_path(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "IGconf_target_path"}
    result = subprocess.run(["./finalize10-bootfs-checks"], capture_output=True, text=True, check=False,
                            cwd=IMAGE / "hooks", env=env)
    assert result.returncode != 0 and "IGconf_target_path" in result.stderr
