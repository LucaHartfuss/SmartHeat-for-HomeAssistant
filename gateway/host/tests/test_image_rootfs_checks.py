"""rootfs_checks.sh (Plan G2b-2 Task 9) an einem nachgebauten Root-Dateisystem: ein gutes besteht, jede einzelne
Verletzung (geteilte Identitaet, Passwort, SSH im Serien-Image, Tunnel-Token, fehlende Archive, fremde
Geraete-API-Adresse) laesst es scheitern. Dazu die Adress-Regel own_url.sh (Nutzer-Vorgabe 2026-10-07: eigener
DNS-Name per https in der eigenen Zone hartfussha.org, keine IP-Adresse, keine AWS-Adresse) direkt."""
import os
import subprocess
from pathlib import Path

import pytest

IMAGE = Path(__file__).resolve().parents[2] / "image"
SCRIPT = IMAGE / "rootfs_checks.sh"
OWN_URL = IMAGE / "own_url.sh"
WANTS = "etc/systemd/system/multi-user.target.wants"
UNITS = ("smartheat-firewall", "smartheat-hoststatus", "smartheat-led", "smartheat-firstboot", "smartheat-updater",
         "docker")
ENV = "var/lib/smartheat/host/gateway.env"
USB_RULE = "etc/udev/rules.d/99-rpi-01-smartheat-usbboot.rules"
USB_HOOK = "etc/initramfs-tools/hooks/smartheat-usbboot"
# so schreibt usbboot.sh beide Dateien (die Regel mit Fortsetzungszeile)
GOOD_USB_RULE = (
    "# SmartHeat-Gateway: Start von der SSD am USB\n"
    'SUBSYSTEM=="block", KERNEL=="sd[a-z]*[0-9]", ENV{DEVTYPE}=="partition", \\\n'
    '  ENV{ID_PART_ENTRY_UUID}=="5348470a-0[12]", ACTION=="add|change", ENV{RPI_ONBOOTDEV}="1"\n'
)
GOOD_USB_HOOK = (
    "#!/bin/sh\n. /usr/share/initramfs-tools/hook-functions\n"
    "copy_file config /etc/udev/rules.d/99-rpi-01-smartheat-usbboot.rules\n"
)

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


def _good(root: Path, pilot: bool = False) -> Path:
    files = {
        "etc/machine-id": "",
        "etc/shadow": "root:*:20000:0:99999:7:::\npi:!:20000:0:99999:7:::\n",
        "etc/passwd": "root:x:0:0:root:/root:/bin/bash\npi:x:1000:1000::/home/pi:/bin/bash\n"
                      "nobody:*:65534:65534::/nonexistent:/usr/sbin/nologin\nlocked:!x:1001:1001::/:/bin/false\n",
        "etc/apt/sources.list.d/raspi.sources": "URIs: http://archive.raspberrypi.com/debian/\n",
        ENV: "SHG_ROOT=/var/lib/smartheat\nSHG_DEVICE_API_URL=https://accounts.hartfussha.org\n"
             "SHG_PORTAL_BASE_URL=https://portal.hartfussha.org\nTZ=Europe/Berlin\n",
        "var/lib/smartheat/updater/state.json": '{"current": "0.3.0", "in_progress": null}',
        "var/lib/smartheat/bundles/0.3.0/manifest.json.minisig": "sig",
        "var/lib/smartheat/images/01.tar": "x",
        "home/pi/.ssh/authorized_keys": "",
        "opt/smartheat/installer/gateway/host/install.sh": "#!/bin/bash\n",
        "opt/smartheat/host/VERSION": "0.3.0\n",
        USB_RULE: GOOD_USB_RULE,
        USB_HOOK: GOOD_USB_HOOK,
        # Zeitzone und Locale (locale_default.sh, Layer locale-base/locale-gen): so sieht es nach dem Bau aus
        "usr/share/zoneinfo/Europe/Berlin": "TZif",
        "etc/locale.conf": "LANG=de_DE.UTF-8\n#LANGUAGE=C\n",
        "etc/locale.gen": "# en_GB.UTF-8 UTF-8\nde_DE.UTF-8 UTF-8\n",
        "usr/lib/locale/locale-archive": "archive",
    }
    if pilot:
        files["root/.ssh/authorized_keys"] = "ssh-ed25519 AAAA test-key\n"
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    (root / "var/lib/smartheat/data").mkdir(parents=True)
    (root / WANTS).mkdir(parents=True)
    (root / USB_HOOK).chmod(0o755)
    (root / "etc/localtime").symlink_to("/usr/share/zoneinfo/Europe/Berlin")
    (root / "etc/default").mkdir(parents=True, exist_ok=True)
    (root / "etc/default/locale").symlink_to("../locale.conf")
    for unit in UNITS + (("ssh",) if pilot else ()):
        (root / WANTS / f"{unit}.service").symlink_to(f"/lib/systemd/system/{unit}.service")
    for tree in ("opt/smartheat", "var/lib/smartheat/images"):  # wie im Image: nicht gruppen-/weltbeschreibbar
        for path in [root / tree, *(root / tree).rglob("*")]:
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
    pilot = _run(_good(tmp_path / "pilot", pilot=True), "--pilot")
    assert pilot.returncode == 0, pilot.stdout


def test_uninitialized_machine_id_is_fine(tmp_path):
    root = _good(tmp_path)
    (root / "etc/machine-id").write_text("uninitialized\n")
    assert _run(root).returncode == 0


@pytest.mark.parametrize("breakit", [
    lambda r: (r / "etc/machine-id").write_text("0123456789abcdef0123456789abcdef\n"),
    lambda r: (r / "var/lib/dbus").mkdir(parents=True) or (r / "var/lib/dbus/machine-id").write_text("0123abcd\n"),
    lambda r: (r / "etc/ssh").mkdir() or (r / "etc/ssh/ssh_host_ed25519_key").write_text("k"),
    lambda r: (r / "var/lib/smartheat/data/device").mkdir(),
    lambda r: (r / "var/lib/smartheat/bus/credentials/agent").mkdir(parents=True)
    or (r / "var/lib/smartheat/bus/credentials/agent/bus.json").write_text("{}"),
    lambda r: (r / "var/lib/smartheat/zigbee2mqtt").mkdir()
    or (r / "var/lib/smartheat/zigbee2mqtt/configuration.yaml").write_text("x"),
    lambda r: (r / "etc/shadow").write_text("root:*:1::::::\npi::1::::::\n"),
    lambda r: (r / "etc/shadow").write_text("root:$y$j9T$abc:1::::::\n"),
    lambda r: (r / "etc/shadow").unlink(),
    lambda r: (r / WANTS / "ssh.service").symlink_to("/lib/systemd/system/ssh.service"),
    lambda r: (r / "etc/systemd/system/sockets.target.wants").mkdir(parents=True)
    or (r / "etc/systemd/system/sockets.target.wants/ssh.socket").symlink_to("/lib/systemd/system/ssh.socket"),
    lambda r: (r / "root/.ssh").mkdir(parents=True) or (r / "root/.ssh/authorized_keys").write_text("ssh-ed25519 A"),
    lambda r: (r / "home/pi/.ssh/authorized_keys").write_text("ssh-ed25519 AAAA test-key\n"),
    lambda r: (r / "etc/smartheat").mkdir() or (r / "etc/smartheat/pilot-ssh-tunnel.env").write_text("TUNNEL_TOKEN=x"),
    lambda r: (r / "var/lib/smartheat/images/01.tar").unlink(),
    lambda r: (r / "var/lib/smartheat/bundles/0.3.0/manifest.json.minisig").unlink(),
    lambda r: (r / "var/lib/smartheat/updater/state.json").write_text('{"current": null}'),
    lambda r: (r / WANTS / "smartheat-updater.service").unlink(),
    lambda r: (r / WANTS / "docker.service").unlink(),
    lambda r: (r / "etc/apt/sources.list.d/raspi.sources").unlink(),
    lambda r: (r / ENV).write_text("SHG_DEVICE_API_URL=http://x\nSHG_PORTAL_BASE_URL=https://portal.hartfussha.org\n"),
    lambda r: (r / ENV).unlink(),
    lambda r: (r / "etc/passwd").write_text("root:x:0:0::/root:/bin/bash\npi::1000:1000::/home/pi:/bin/bash\n"),
    lambda r: (r / "etc/passwd").write_text("root:$6$salt$hash:0:0::/root:/bin/bash\n"),
    lambda r: (r / "etc/passwd").unlink(),
    lambda r: (r / "var/lib/smartheat/bus/mosquitto").mkdir(parents=True)
    or (r / "var/lib/smartheat/bus/mosquitto/passwd").write_text("agent:$7$x\n"),
    lambda r: (r / "opt/smartheat/installer/gateway/host/install.sh").chmod(0o666),
    lambda r: (r / "opt/smartheat/installer").chmod(0o775),
    lambda r: (r / "var/lib/smartheat/images/01.tar").chmod(0o664),
    lambda r: (r / "var/lib/smartheat/images").chmod(0o777),
    lambda r: __import__("shutil").rmtree(r / "opt/smartheat"),
    lambda r: (r / USB_RULE).unlink(),
    lambda r: (r / USB_RULE).write_text('SUBSYSTEM=="block", ENV{ID_PART_ENTRY_UUID}=="random-0[12]"\n'),
    lambda r: (r / USB_HOOK).chmod(0o644),
])
def test_each_violation_fails(tmp_path, breakit):
    root = _good(tmp_path)
    breakit(root)
    result = _run(root)
    assert result.returncode != 0 and "FAIL:" in result.stdout


@pytest.mark.parametrize("breakit, problem", [
    # jede Zeile isoliert genau einen Mangel an sonst bestehendem Rootfs
    (lambda r: (r / USB_RULE).write_text(GOOD_USB_RULE.replace('ENV{RPI_ONBOOTDEV}="1"', 'ENV{OTHER}="1"')),
     "USB-Startregel"),
    (lambda r: (r / USB_RULE).write_text(GOOD_USB_RULE.replace('ENV{RPI_ONBOOTDEV}="1"', 'ENV{RPI_ONBOOTDEV}="0"')),
     "USB-Startregel"),
    (lambda r: (r / USB_RULE).write_text(
        "".join(f"# {line}\n" for line in GOOD_USB_RULE.replace("\\\n", "").splitlines()[1:])),
     "USB-Startregel"),
    (lambda r: (r / USB_RULE).write_text(GOOD_USB_RULE.replace('KERNEL=="sd[a-z]*[0-9]", ', "")),
     "USB-Startregel"),
    (lambda r: (r / USB_HOOK).write_text(""), "initramfs-Hook"),
    (lambda r: (r / USB_HOOK).write_text(GOOD_USB_HOOK.replace("copy_file", "# copy_file")), "initramfs-Hook"),
], ids=["no-onbootdev", "onbootdev-0", "rule-in-comment", "no-kernel-sd", "empty-hook", "commented-copy_file"])
def test_usb_boot_rule_and_hook_are_checked_strictly(tmp_path, breakit, problem):
    root = _good(tmp_path)
    assert _run(root).returncode == 0
    breakit(root)
    (root / USB_HOOK).chmod(0o755)  # der Hook bleibt ausfuehrbar: nur sein Inhalt bzw. die Regel ist der Mangel
    result = _run(root)
    assert result.returncode != 0
    fails = [line for line in result.stdout.splitlines() if line.startswith("FAIL:")]
    assert len(fails) == 1 and problem in fails[0], result.stdout


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
], ids=["london", "utc", "no-localtime", "berlin2", "no-zonefile", "etc-timezone-london", "lang-c", "lang-en",
        "lang-commented", "lc-all", "lang-twice", "no-locale-file", "default-locale-c", "not-generated", "no-archive"])
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
], ids=["relative-link", "etc-timezone-berlin", "no-default-locale", "default-locale-file", "language-de"])
def test_timezone_and_locale_variants_that_are_fine(tmp_path, fixit):
    root = _good(tmp_path)
    fixit(root)
    result = _run(root)
    assert result.returncode == 0, result.stdout


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
    assert "/opt/smartheat" in result.stdout and "/var/lib/smartheat/images" in result.stdout


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


def test_pilot_image_needs_its_key_and_ssh(tmp_path):
    root = _good(tmp_path, pilot=True)
    (root / "root/.ssh/authorized_keys").write_text("")
    assert _run(root, "--pilot").returncode != 0
    root = _good(tmp_path / "nossh", pilot=True)
    (root / WANTS / "ssh.service").unlink()
    assert _run(root, "--pilot").returncode != 0


def test_pilot_image_has_no_host_keys_either(tmp_path):
    root = _good(tmp_path, pilot=True)
    (root / "etc/ssh").mkdir()
    (root / "etc/ssh/ssh_host_rsa_key").write_text("k")
    assert _run(root, "--pilot").returncode != 0


def test_customize_mode_leaves_only_the_machine_ids_to_the_final_check(tmp_path):
    """Im customize-Hook bestehen die machine-ids von systemd und dbus noch; zurueckgesetzt bzw. geloescht werden sie
    erst beim Aufraeumen von mmdebstrap (geprueft in post-build.sh). Alles andere prueft auch der customize-Lauf."""
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


def test_build_hooks_ignore_the_test_only_owner_override(tmp_path):
    """post-build.sh (und customize.sh) rufen rootfs_checks ohne SHG_ROOTFS_CHECK_UID auf: ein gesetzter Wert in der
    Bau-Umgebung schwaecht die Eigentuemer-Pruefung nicht ab."""
    if os.getuid() == 0:
        pytest.skip("laeuft als root: die Fixtures gehoeren dann root")
    root = _good(tmp_path / "root")
    stage = tmp_path / "stage"
    stage.mkdir()
    env = {**os.environ, "SHG_ROOTFS_CHECK_UID": str(os.getuid()), "IGconf_shg_stage": str(stage)}
    result = subprocess.run(["bash", str(IMAGE / "post-build.sh"), str(root)], capture_output=True, text=True,
                            check=False, env=env)
    assert result.returncode != 0 and "nicht root" in result.stdout
    assert "env -u SHG_ROOTFS_CHECK_UID" in (IMAGE / "customize.sh").read_text()


# initramfs_check.sh (nur post-build): lsinitramfs laeuft per chroot im Root-Dateisystem. Die Tests ersetzen chroot und
# lsinitramfs durch Attrappen im PATH: chroot ruft den Befehl ohne Wurzelwechsel auf (relativ zum Root-Dateisystem
# aufgeloest), lsinitramfs gibt den Inhalt der "initramfs" (Textdatei mit einem Eintrag je Zeile) aus.
INITRAMFS_CHECK = IMAGE / "initramfs_check.sh"
USB_RULE_ENTRY = "etc/udev/rules.d/99-rpi-01-smartheat-usbboot.rules"


def _fake_chroot_path(tmp_path: Path) -> str:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "chroot").write_text('#!/bin/bash\nroot="$1"; shift\nSHG_FAKE_ROOT="$root" exec "$@"\n')
    (bindir / "lsinitramfs").write_text(
        '#!/bin/bash\nf="$SHG_FAKE_ROOT$1"\n[ -s "$f" ] || { echo "lsinitramfs: $1: nicht lesbar" >&2; exit 1; }\n'
        'cat "$f"\n')
    for tool in ("chroot", "lsinitramfs"):
        (bindir / tool).chmod(0o755)
    return f"{bindir}{os.pathsep}{os.environ['PATH']}"


def _initramfs_run(tmp_path: Path, root: Path, *, path_env: str | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "PATH": path_env or _fake_chroot_path(tmp_path)}
    return subprocess.run(["bash", str(INITRAMFS_CHECK), str(root)], capture_output=True, text=True, check=False,
                          env=env)


def _initramfs(root: Path, rel: str, entries: list[str]) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text("\n".join(entries) + "\n")


def test_initramfs_check_passes_with_the_rule_inside(tmp_path):
    root = tmp_path / "root"
    _initramfs(root, "boot/firmware/initramfs8", ["etc/udev/rules.d/60-persistent-storage.rules", USB_RULE_ENTRY])
    _initramfs(root, "boot/initrd.img-6.18.50+rpt-rpi-v8", [USB_RULE_ENTRY])
    result = _initramfs_run(tmp_path, root)
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize("where", ["boot/firmware/initramfs8", "boot/initrd.img-6.18.50+rpt-rpi-v8"])
def test_initramfs_check_fails_when_the_rule_is_missing_in_any_initramfs(tmp_path, where):
    root = tmp_path / "root"
    for rel in ("boot/firmware/initramfs8", "boot/initrd.img-6.18.50+rpt-rpi-v8"):
        _initramfs(root, rel, ["etc/udev/rules.d/99-rpi-00-bootdev.rules"] if rel == where else [USB_RULE_ENTRY])
    result = _initramfs_run(tmp_path, root)
    assert result.returncode != 0
    assert f"FAIL: USB-Startregel fehlt in der initramfs /{where}" in result.stdout


def test_initramfs_check_fails_clearly_without_any_initramfs(tmp_path):
    root = tmp_path / "root"
    (root / "boot/firmware").mkdir(parents=True)
    result = _initramfs_run(tmp_path, root)
    assert result.returncode != 0 and "FAIL: keine initramfs im Image gefunden" in result.stdout


def test_initramfs_check_fails_when_lsinitramfs_cannot_run(tmp_path):
    root = tmp_path / "root"
    _initramfs(root, "boot/firmware/initramfs8", [USB_RULE_ENTRY])
    (root / "boot/firmware/initramfs8").write_text("")  # die Attrappe meldet eine nicht lesbare Datei (Exit 1)
    result = _initramfs_run(tmp_path, root)
    assert result.returncode != 0 and "FAIL: lsinitramfs /boot/firmware/initramfs8 fehlgeschlagen" in result.stdout


def test_post_build_runs_the_initramfs_check_too(tmp_path):
    """post-build.sh bricht den Bau auch bei scheiternder initramfs-Pruefung ab und fuehrt beide Pruefungen aus."""
    root = _good(tmp_path / "root")
    stage = tmp_path / "stage"
    stage.mkdir()
    _initramfs(root, "boot/firmware/initramfs8", ["etc/udev/rules.d/99-rpi-00-bootdev.rules"])
    env = {**os.environ, "PATH": _fake_chroot_path(tmp_path), "IGconf_shg_stage": str(stage)}
    result = subprocess.run(["bash", str(IMAGE / "post-build.sh"), str(root)], capture_output=True, text=True,
                            check=False, env=env)
    assert result.returncode != 0
    assert "FAIL: USB-Startregel fehlt in der initramfs /boot/firmware/initramfs8" in result.stdout
    if os.getuid() != 0:  # die Fixtures gehoeren dem Testbenutzer: rootfs_checks scheitert dann ebenfalls (Eigentuemer)
        assert "nicht root" in result.stdout
    assert os.access(INITRAMFS_CHECK, os.X_OK)
