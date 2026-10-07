"""rootfs_checks.sh (Plan G2b-2 Task 9) an einem nachgebauten Root-Dateisystem: ein gutes besteht, jede einzelne
Verletzung (geteilte Identitaet, Passwort, SSH im Serien-Image, Tunnel-Token, fehlende Archive, fremde
Geraete-API-Adresse) laesst es scheitern. Dazu die Adress-Regel own_url.sh (Nutzer-Vorgabe 2026-10-07: eigener
DNS-Name per https, keine IP-Adresse, keine AWS-Adresse) direkt."""
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

GOOD_URLS = [
    "https://accounts.hartfussha.org",
    "https://accounts.hartfussha.org/",
    "https://portal.hartfussha.org",
    "https://accounts.example.test",
    "https://portal.example.test",
    "https://a-b.c1.example.org",
    "https://Accounts.Hartfussha.ORG",
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
    "https://-bad.example.org",
    "https://bad-.example.org",
    "https://acc ounts.example.org",
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
]


def _problem(url: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", 'source "$1" && shg_own_url_problem "$2"', "_", str(OWN_URL), url],
                          capture_output=True, text=True, check=False)


@pytest.mark.parametrize("url", GOOD_URLS)
def test_own_url_accepts_an_own_dns_name_per_https(url):
    result = _problem(url)
    assert result.returncode == 0 and result.stdout == "", result.stdout


@pytest.mark.parametrize("url", BAD_URLS)
def test_own_url_rejects_ip_aws_and_everything_but_https_with_a_dns_name(url):
    result = _problem(url)
    assert result.returncode == 1 and result.stdout.strip(), url


def _good(root: Path, pilot: bool = False) -> Path:
    files = {
        "etc/machine-id": "",
        "etc/shadow": "root:*:20000:0:99999:7:::\npi:!:20000:0:99999:7:::\n",
        "etc/apt/sources.list.d/raspi.sources": "URIs: http://archive.raspberrypi.com/debian/\n",
        ENV: "SHG_ROOT=/var/lib/smartheat\nSHG_DEVICE_API_URL=https://accounts.example.test\n"
             "SHG_PORTAL_BASE_URL=https://portal.example.test\nTZ=Europe/Berlin\n",
        "var/lib/smartheat/updater/state.json": '{"current": "0.3.0", "in_progress": null}',
        "var/lib/smartheat/bundles/0.3.0/manifest.json.minisig": "sig",
        "var/lib/smartheat/images/01.tar": "x",
        "home/pi/.ssh/authorized_keys": "",
    }
    if pilot:
        files["root/.ssh/authorized_keys"] = "ssh-ed25519 AAAA test-key\n"
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    (root / "var/lib/smartheat/data").mkdir(parents=True)
    (root / WANTS).mkdir(parents=True)
    for unit in UNITS + (("ssh",) if pilot else ()):
        (root / WANTS / f"{unit}.service").symlink_to(f"/lib/systemd/system/{unit}.service")
    return root


def _run(root: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT), str(root), *extra], capture_output=True, text=True, check=False)


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
    lambda r: (r / ENV).write_text("SHG_DEVICE_API_URL=http://x\nSHG_PORTAL_BASE_URL=https://portal.example.test\n"),
    lambda r: (r / ENV).unlink(),
])
def test_each_violation_fails(tmp_path, breakit):
    root = _good(tmp_path)
    breakit(root)
    result = _run(root)
    assert result.returncode != 0 and "FAIL:" in result.stdout


@pytest.mark.parametrize("url", ["https://accounts.hartfussha.org", "https://accounts.hartfussha.org/"])
def test_own_hostname_as_device_api_passes(tmp_path, url):
    root = _good(tmp_path)
    (root / ENV).write_text(f"SHG_DEVICE_API_URL={url}\nSHG_PORTAL_BASE_URL=https://portal.hartfussha.org\n")
    assert _run(root).returncode == 0


@pytest.mark.parametrize("url", [
    "https://192.168.2.154", "https://192.168.2.154:8443", "https://[2001:db8::1]", "https://localhost",
    "https://abc123.execute-api.eu-central-1.amazonaws.com", "https://my-lb-1.eu-central-1.elb.amazonaws.com",
    "https://abc.eu-central-1.awsapprunner.com", "https://accounts.hartfussha.org/api", "https://accounts",
    "https://user@accounts.hartfussha.org", "",
])
def test_device_api_must_be_an_own_dns_name(tmp_path, url):
    root = _good(tmp_path)
    (root / ENV).write_text(f"SHG_DEVICE_API_URL={url}\nSHG_PORTAL_BASE_URL=https://portal.example.test\n")
    result = _run(root)
    assert result.returncode != 0 and "FAIL: Geraete-API" in result.stdout


@pytest.mark.parametrize("url", ["https://10.0.0.5", "https://portal.cloudfront.net", ""])
def test_portal_must_be_an_own_dns_name(tmp_path, url):
    root = _good(tmp_path)
    (root / ENV).write_text(f"SHG_DEVICE_API_URL=https://accounts.example.test\nSHG_PORTAL_BASE_URL={url}\n")
    result = _run(root)
    assert result.returncode != 0 and "FAIL: Portal" in result.stdout


def test_last_assignment_counts(tmp_path):
    """Docker und systemd nehmen bei doppeltem Schluessel den letzten Wert: der zaehlt auch hier."""
    root = _good(tmp_path)
    env = (root / ENV).read_text()
    (root / ENV).write_text(env + "SHG_DEVICE_API_URL=https://10.0.0.5\n")
    assert _run(root).returncode != 0


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


def test_customize_mode_leaves_only_the_machine_id_to_the_final_check(tmp_path):
    """Im customize-Hook hat systemd die machine-id schon angelegt; zurueckgesetzt wird sie erst beim Aufraeumen von
    mmdebstrap (geprueft in post-build.sh). Alles andere prueft auch der customize-Lauf."""
    root = _good(tmp_path)
    (root / "etc/machine-id").write_text("0123456789abcdef0123456789abcdef\n")
    assert _run(root, "--customize").returncode == 0
    assert _run(root).returncode != 0
    (root / "etc/ssh").mkdir()
    (root / "etc/ssh/ssh_host_ed25519_key").write_text("k")
    assert _run(root, "--customize").returncode != 0


def test_unknown_option_is_rejected(tmp_path):
    assert _run(_good(tmp_path), "--pilto").returncode == 2
