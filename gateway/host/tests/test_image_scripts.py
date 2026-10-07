"""Image-Bau-Skripte (Plan G2b-2 Task 9) ohne Docker: prepare.sh und make_image.sh lehnen fremde Adressen (IP, AWS,
http, Port, Pfad) ab, bevor sie etwas tun; prepare.sh legt mit einer Docker-Attrappe die Stage richtig an; Layer,
Konfiguration und Installer passen zusammen (alle Pakete aus dem Layer, kein apt im chroot)."""
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from test_boundaries import SHIPPED, TENANT_ID

GW = Path(__file__).resolve().parents[2]
IMAGE = GW / "image"
REPO = GW.parent
MOSQ = "eclipse-mosquitto:2@sha256:" + "b" * 64
GATEWAY = "ghcr.io/lucahartfuss/smartheat-gateway@sha256:" + "a" * 64
GOOD = ["--device-api-url", "https://accounts.hartfussha.org", "--portal-base-url", "https://portal.hartfussha.org"]
BAD_URLS = ["https://192.168.2.154", "http://accounts.hartfussha.org",
            "https://abc.execute-api.eu-central-1.amazonaws.com", "https://x.eu-central-1.elb.amazonaws.com",
            "https://abc.awsapprunner.com", "https://[2001:db8::1]", "https://accounts.hartfussha.org:8443",
            "https://accounts.hartfussha.org/api", "https://localhost", "https://x.amazonaws.\uff43\uff4f\uff4d",
            "https://b\u00fccher.example", "https://gw.10.0.0.5.nip.io", "", "https://accounts.example.test",
            "https://hartfussha.org.evil.com", "https://evilhartfussha.org", "https://accounts.hartfussha.org."]
FAKE_DOCKER = """#!/bin/bash
# Docker-Attrappe: "version" meldet arm64; sonst protokolliert sie den Aufruf und legt beim Export (Mount :/stage)
# je Referenz ein Archiv an.
if [ "$1" = version ]; then echo arm64; exit 0; fi
all="$*"; printf '%s\\n' "${all//$'\\n'/ }" >>"$SHG_FAKE_LOG"  # ein Aufruf = eine Zeile
stage=""
for arg in "$@"; do case "$arg" in *:/stage|*:/stage:ro) stage="${arg%%:/stage*}" ;; esac; done
if [ -n "$stage" ] && [ "${*: -1}" != "/src/gateway/image/build.sh" ]; then
  n=0; seen=0
  for arg in "$@"; do
    [ "$arg" = _ ] && { seen=1; continue; }
    [ "$seen" = 1 ] || continue
    n=$((n + 1)); mkdir -p "$stage/images"; printf '%s' "$arg" >"$stage/images/$(printf '%02d' "$n").tar"
  done
fi
exit 0
"""


def _bundle(path: Path) -> Path:
    path.mkdir(parents=True)
    compose = (f"services:\n  agent:\n    image: {GATEWAY}\n  runtime:\n    image: {GATEWAY}\n"
               f"  mosquitto:\n    image: {MOSQ}\n")
    (path / "docker-compose.yml").write_text(compose)
    (path / "mosquitto.conf").write_text("listener 1883\n")
    (path / "manifest.json").write_text('{"version": "0.3.0"}\n')
    (path / "manifest.json.minisig").write_text("sig\n")
    return path


def _env(tmp_path: Path) -> dict[str, str]:
    """PATH mit Docker-Attrappe und python3 = dieses Python (prepare.sh ruft python3 fuer compose_images)."""
    fake = tmp_path / "fakebin"
    fake.mkdir(exist_ok=True)
    docker = fake / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    python = fake / "python3"
    if not python.exists():
        python.symlink_to(os.path.realpath(sys.executable))
    return {**os.environ, "PATH": f"{fake}:{os.environ['PATH']}", "SHG_FAKE_LOG": str(tmp_path / "docker.log"),
            "TMPDIR": str(tmp_path)}


def _script(name: str, tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(IMAGE / name), *args], capture_output=True, text=True, check=False,
                          env=_env(tmp_path))


def _docker_calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "docker.log"
    return log.read_text().splitlines() if log.exists() else []


@pytest.mark.parametrize("option", ["--device-api-url", "--portal-base-url"])
@pytest.mark.parametrize("url", BAD_URLS)
def test_prepare_rejects_foreign_urls_before_any_work(tmp_path, option, url):
    stage = tmp_path / "stage"
    args = list(GOOD)
    args[args.index(option) + 1] = url
    result = _script("prepare.sh", tmp_path, "--bundle", str(_bundle(tmp_path / "b")), "--stage", str(stage), *args)
    assert result.returncode == 2, result.stderr
    assert f"FEHLER: {option}" in result.stderr and "eigener DNS-Name" in result.stderr
    assert "Zone hartfussha.org" in result.stderr  # die Meldung nennt die erlaubte Zone (aus own_url.sh)
    assert not stage.exists() and _docker_calls(tmp_path) == []


@pytest.mark.parametrize("option", ["--device-api-url", "--portal-base-url"])
@pytest.mark.parametrize("url", BAD_URLS)
def test_make_image_rejects_foreign_urls_before_any_work(tmp_path, option, url):
    out = tmp_path / "out"
    args = list(GOOD)
    args[args.index(option) + 1] = url
    result = _script("make_image.sh", tmp_path, "--bundle", str(_bundle(tmp_path / "b")), "--out", str(out), *args)
    assert result.returncode == 2, result.stderr
    assert f"FEHLER: {option}" in result.stderr
    assert not out.exists() and _docker_calls(tmp_path) == []
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith("tmp")] == []


@pytest.mark.parametrize("args", [
    [],
    ["--bundle"],
    ["--stage", "x", *GOOD],
    ["--bundle", "b", *GOOD],
    ["--bundle", "b", "--stage", "/", *GOOD],
    ["--bundle", "b", "--stage", "s", "--pilot-ssh", "kein schluessel", *GOOD],
    ["--bundle", "b", "--stage", "s", "--pilot-ssh", "ssh-ed25519 AAAA test\nssh-rsa BBBB", *GOOD],
    ["--bundle", "b", "--stage", "s", "--unbekannt", *GOOD],
])
def test_prepare_rejects_bad_arguments(tmp_path, args):
    _bundle(tmp_path / "b")
    args = [str(tmp_path / a) if a in ("b", "s", "x") else a for a in args]
    result = _script("prepare.sh", tmp_path, *args)
    assert result.returncode == 2, result.stderr
    assert _docker_calls(tmp_path) == []


def test_prepare_refuses_a_stage_that_is_not_empty(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "wichtig").write_text("x")
    result = _script("prepare.sh", tmp_path, "--bundle", str(_bundle(tmp_path / "b")), "--stage", str(stage), *GOOD)
    assert result.returncode == 2 and (stage / "wichtig").exists()


def test_prepare_rejects_an_incomplete_bundle(tmp_path):
    bundle = _bundle(tmp_path / "b")
    (bundle / "manifest.json.minisig").unlink()
    result = _script("prepare.sh", tmp_path, "--bundle", str(bundle), "--stage", str(tmp_path / "s"), *GOOD)
    assert result.returncode == 2 and "manifest.json.minisig" in result.stderr


@pytest.mark.parametrize("pilot", [False, True])
def test_prepare_builds_the_stage(tmp_path, pilot):
    stage = tmp_path / "stage"
    extra = ["--pilot-ssh", "ssh-ed25519 AAAA test-key"] if pilot else []
    result = _script("prepare.sh", tmp_path, "--bundle", str(_bundle(tmp_path / "b")), "--stage", str(stage),
                     *GOOD, *extra)
    assert result.returncode == 0, result.stderr
    installer = stage / "installer" / "gateway"
    assert (installer / "host" / "install.sh").is_file() and not (installer / "host" / "tests").exists()
    assert (installer / "host" / "pilot_ssh_tunnel.sh").is_file()
    for name in ("rootfs_checks.sh", "own_url.sh", "customize.sh"):
        assert (installer / "image" / name).is_file(), name
    for rel in SHIPPED:
        assert (installer / "src" / "smartheat_gateway" / rel).is_file(), rel
    assert (installer / "VERSION").read_text() == (GW / "VERSION").read_text()
    for name in ("docker-compose.yml", "mosquitto.conf", "manifest.json", "manifest.json.minisig"):
        assert (stage / "bundle" / name).is_file(), name
    # jedes Image genau einmal exportiert (agent und runtime teilen sich das Gateway-Image)
    assert sorted(p.read_text() for p in (stage / "images").glob("*.tar")) == sorted([GATEWAY, MOSQ])
    lines = (stage / "install.args").read_text().splitlines()
    assert lines[:4] == GOOD
    assert lines[4:] == (["--pilot-ssh", "ssh-ed25519 AAAA test-key"] if pilot else [])
    assert (stage / "pilot").exists() == pilot
    [call] = _docker_calls(tmp_path)
    assert call.startswith("run --rm") and "--privileged" not in call and "linux/arm64" in call


def test_make_image_runs_prepare_then_the_arm64_build(tmp_path):
    out = tmp_path / "out"
    result = _script("make_image.sh", tmp_path, "--bundle", str(_bundle(tmp_path / "b")), "--out", str(out), *GOOD)
    assert result.returncode == 0, result.stderr
    export, build = _docker_calls(tmp_path)
    assert "export_images.sh" in export
    assert "--platform linux/arm64" in build and "--privileged" in build
    assert build.endswith("/src/gateway/image/build.sh")
    assert f"{out}:/out" in build and ":/stage:ro" in build
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith("tmp")] == []  # Stage aufgeraeumt


@pytest.mark.parametrize("script, target", [("prepare.sh", "--stage"), ("make_image.sh", "--out")])
def test_upper_case_own_zone_is_accepted(tmp_path, script, target):
    """Gross-/Kleinschreibung zaehlt bei DNS-Namen nicht: ACCOUNTS.HARTFUSSHA.ORG ist die eigene Zone."""
    args = ["--device-api-url", "https://ACCOUNTS.HARTFUSSHA.ORG", "--portal-base-url", "https://Portal.Hartfussha.Org"]
    result = _script(script, tmp_path, "--bundle", str(_bundle(tmp_path / "b")), target, str(tmp_path / "t"), *args)
    assert result.returncode == 0, result.stderr


def test_make_image_needs_out(tmp_path):
    result = _script("make_image.sh", tmp_path, "--bundle", str(_bundle(tmp_path / "b")), *GOOD)
    assert result.returncode == 2 and _docker_calls(tmp_path) == []


def _layer_packages() -> set[str]:
    text = (IMAGE / "layer" / "smartheat-gateway.yaml").read_text()
    block = text.split("packages:", 1)[1].split("customize-hooks:", 1)[0]
    return set(re.findall(r"^\s*-\s*(\S+)\s*$", block, re.MULTILINE))


def test_layer_brings_every_package_install_sh_wants():
    """install.sh --image laeuft im chroot ohne apt: alle Pakete muessen aus dem Layer kommen (openssh-server bringt
    trixie-minbase mit)."""
    install = (GW / "host" / "install.sh").read_text()
    wanted = set(re.search(r"local wanted=\(([^)]*)\)", install).group(1).split())
    wanted |= set(re.search(r"wanted\+=\((docker\.io[^)]*)\)", install).group(1).split())
    assert wanted - _layer_packages() == set()


def test_config_layer_and_hook_fit_together():
    config = (IMAGE / "config" / "smartheat-gateway.yaml").read_text()
    layer = (IMAGE / "layer" / "smartheat-gateway.yaml").read_text()
    name = re.search(r"^# X-Env-Layer-Name: (\S+)$", layer, re.MULTILINE).group(1)
    assert re.search(rf"^  shg: {name}$", config, re.MULTILINE)
    assert re.search(r"^# X-Env-VarPrefix: shg$", layer, re.MULTILINE)
    assert "$IGconf_shg_stage/installer/gateway/image/customize.sh" in layer
    assert re.search(r"^  layer: rpi4$", config, re.MULTILINE)
    # v2.8.0: das IDP-Schema (post-image) kennt kein usb; der USB-Start kommt aus usbboot.sh (CI-Spike 2026-10-07)
    assert re.search(r"^  storage_type: sd$", config, re.MULTILINE)
    assert re.search(r'^  disksig: "0x[0-9a-f]{8}"$', config, re.MULTILINE)
    assert layer.index('- bash "$SRCROOT/usbboot.sh"') < layer.index(
        '- bash "$IGconf_shg_stage/installer/gateway/image/customize.sh"')
    post_build = IMAGE / "post-build.sh"
    assert post_build.stat().st_mode & stat.S_IXUSR, "rpi-image-gen fuehrt nur ausfuehrbare Hooks aus"


def test_pins_are_the_same_everywhere():
    """Basis-Image per Digest und rpi-image-gen per Commit (v2.8.0), ueberall dieselben Werte."""
    digests = set()
    for path in [*IMAGE.glob("*.sh"), REPO / "tests" / "test_gateway_firstboot.sh"]:
        code = "\n".join(line for line in path.read_text().splitlines() if not line.lstrip().startswith("#"))
        digests |= set(re.findall(r"debian:trixie@sha256:[0-9a-f]{64}", code))
        assert not re.search(r"debian:trixie(?!@sha256:[0-9a-f]{64})\b", code), path
    assert len(digests) == 1
    assert "RIG_COMMIT=262d4df5a9f9d4133370465399a7958a7c22cdc7" in (IMAGE / "build.sh").read_text()


def test_no_tenant_ids_in_image_files():
    for path in sorted(p for p in IMAGE.rglob("*") if p.is_file()):
        assert not TENANT_ID.search(path.read_text(encoding="utf-8", errors="ignore")), path


def _usbboot(root: Path, sig: str | None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "IGconf_image_disksig"}
    if sig is not None:
        env["IGconf_image_disksig"] = sig
    return subprocess.run(["bash", str(IMAGE / "usbboot.sh"), str(root)], capture_output=True, text=True,
                          check=False, env=env)


def test_usbboot_writes_the_rule_for_the_configured_disk_signature(tmp_path):
    """rpi-image-gen v2.8.0 legt /dev/disk/by-slot/* nur fuer SD/eMMC und NVMe an (CI-Spike 2026-10-07): usbboot.sh
    markiert die Partitionen des Images am USB ueber die feste Disk-Signatur aus der Konfiguration, auch in der
    initramfs, und rootfs_checks.sh erkennt die Regel."""
    config = (IMAGE / "config" / "smartheat-gateway.yaml").read_text()
    found = re.search(r'^  disksig: "(0x[0-9a-f]{8})"$', config, re.MULTILINE)
    assert found
    sig = found.group(1)
    result = _usbboot(tmp_path, sig.upper().replace("0X", "0x"))
    assert result.returncode == 0, result.stderr
    # Der Dateiname kommt aus dem, was usbboot.sh wirklich schreibt (nicht aus einem Literal im Test)
    written = sorted(p.name for p in (tmp_path / "etc/udev/rules.d").iterdir())
    assert len(written) == 1, written
    name = written[0]
    rule = _logical_rule_lines(tmp_path / "etc/udev/rules.d" / name)
    assert len(rule) == 1, rule  # eine Regel, auch mit Fortsetzungszeilen
    rule = rule[0]
    assert f'ENV{{ID_PART_ENTRY_UUID}}=="{sig[2:]}-0[12]"' in rule and 'ENV{RPI_ONBOOTDEV}="1"' in rule
    assert 'KERNEL=="sd[a-z]*[0-9]"' in rule
    hook = tmp_path / "etc/initramfs-tools/hooks/smartheat-usbboot"
    assert hook.stat().st_mode & stat.S_IXUSR
    assert f"copy_file config /etc/udev/rules.d/{name}" in hook.read_text()
    # udev liest die Regeldateien in lexikalischer Reihenfolge: nach 99-rpi-00-bootdev (rpi-storage-binder, setzt
    # RPI_ONBOOTDEV fuer SD/NVMe) und vor 99-rpi-05-image (legt die by-slot-Links an)
    assert "99-rpi-00-bootdev.rules" < name < "99-rpi-05-image.rules"


def _logical_rule_lines(path: Path) -> list[str]:
    """Regelzeilen einer udev-Datei ohne Kommentare, Zeilen mit Backslash am Ende zu einer verbunden."""
    lines, current = [], ""
    for raw in path.read_text().splitlines():
        if not current and raw.lstrip().startswith("#"):
            continue
        if raw.endswith("\\"):
            current += raw[:-1]
            continue
        current += raw
        if current.strip():
            lines.append(current.strip())
        current = ""
    return lines


@pytest.mark.parametrize("sig", [None, "", "random", "0x123", "5348470a", "0x5348470g"])
def test_usbboot_needs_a_fixed_disk_signature(tmp_path, sig):
    result = _usbboot(tmp_path, sig)
    assert result.returncode != 0
    assert not (tmp_path / "etc").exists()


def _config_locale() -> dict[str, str]:
    config = (IMAGE / "config" / "smartheat-gateway.yaml").read_text()
    block = re.search(r"^locale:\n((?:  .*\n)+)", config, re.MULTILINE)
    assert block, "Abschnitt locale fehlt in config/smartheat-gateway.yaml"
    return dict(re.findall(r"^  (\w+): (.+?)\s*$", block.group(1), re.MULTILINE))


def test_config_sets_german_locale_keyboard_and_berlin_time():
    """Nutzer-Vorgabe 2026-10-07: Layer locale-config von rpi-image-gen v2.8.0 (Standard en_GB/gb/Europe/London)."""
    assert _config_locale() == {"default": "de_DE.UTF-8", "keyboard_keymap": "de", "keyboard_layout": "German",
                                "timezone": "Europe/Berlin"}


def test_rootfs_checks_and_install_expect_the_configured_timezone_and_locale():
    """Eine Quelle fuer den Bau (Konfiguration); rootfs_checks.sh und der Standard von install.sh (TZ der Container)
    muessen dazu passen."""
    wanted = _config_locale()
    checks = (IMAGE / "rootfs_checks.sh").read_text()
    assert re.search(rf"^SHG_IMAGE_TIMEZONE={re.escape(wanted['timezone'])}$", checks, re.MULTILINE)
    assert re.search(rf"^SHG_IMAGE_LOCALE={re.escape(wanted['default'])}$", checks, re.MULTILINE)
    assert re.search(rf"^SHG_IMAGE_KEYMAP={re.escape(wanted['keyboard_keymap'])}$", checks, re.MULTILINE)
    assert f'TZ_NAME="{wanted["timezone"]}"' in (GW / "host" / "install.sh").read_text()


def _fake_chroot(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """chroot-Attrappe: protokolliert den Befehl im Root-Dateisystem (eine Zeile je Aufruf) statt ihn auszufuehren."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "chroot.log"
    (bindir / "chroot").write_text('#!/bin/bash\nroot="$1"; shift\necho "$root|$*" >>"$SHG_FAKE_CHROOT_LOG"\n'
                                   'exit "${SHG_FAKE_CHROOT_RC:-0}"\n')
    (bindir / "chroot").chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("IGconf_")}
    env.update({"PATH": f"{bindir}:{os.environ['PATH']}", "SHG_FAKE_CHROOT_LOG": str(log)})
    return env, log


def test_locale_hook_sets_the_configured_default_locale(tmp_path):
    """locale-base (v2.8.0) schreibt LANG=C.UTF-8 nach /etc/locale.conf, das Paket locales aendert die Datei beim
    Einrichten nicht (nur die Erzeugung folgt der Konfiguration): locale_default.sh setzt LANG per update-locale im
    chroot, das die erzeugte Locale prueft."""
    env, log = _fake_chroot(tmp_path)
    env["IGconf_locale_default"] = _config_locale()["default"]
    root = tmp_path / "root"
    root.mkdir()
    result = subprocess.run(["bash", str(IMAGE / "locale_default.sh"), str(root)], capture_output=True, text=True,
                            check=False, env=env)
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == [f"{root}|update-locale --reset LANG=de_DE.UTF-8"]


@pytest.mark.parametrize("value", [None, "", "de_DE", "de_DE.UTF-8 UTF-8", "LANG=x", "de_DE.UTF-8\nLC_ALL=C"])
def test_locale_hook_rejects_a_missing_or_odd_locale(tmp_path, value):
    env, log = _fake_chroot(tmp_path)
    if value is not None:
        env["IGconf_locale_default"] = value
    root = tmp_path / "root"
    root.mkdir()
    result = subprocess.run(["bash", str(IMAGE / "locale_default.sh"), str(root)], capture_output=True, text=True,
                            check=False, env=env)
    assert result.returncode != 0 and not log.exists()


def test_locale_hook_fails_when_update_locale_fails(tmp_path):
    env, _ = _fake_chroot(tmp_path)
    env.update({"IGconf_locale_default": "de_DE.UTF-8", "SHG_FAKE_CHROOT_RC": "1"})
    root = tmp_path / "root"
    root.mkdir()
    result = subprocess.run(["bash", str(IMAGE / "locale_default.sh"), str(root)], capture_output=True, text=True,
                            check=False, env=env)
    assert result.returncode != 0


def test_layer_installs_locales_and_tzdata():
    """trixie-minbase (v2.8.0) laedt locale-base, aber nicht locale-gen: ohne das Paket locales gaebe es weder die
    Locale noch update-locale; tzdata wertet die Vorbelegung der Zeitzone aus."""
    assert {"locales", "tzdata"} <= _layer_packages()


def test_layer_runs_the_locale_hook_before_customize():
    layer = (IMAGE / "layer" / "smartheat-gateway.yaml").read_text()
    assert '- bash "$SRCROOT/locale_default.sh" "$1"' in layer
    assert layer.index('- bash "$SRCROOT/locale_default.sh"') < layer.index(
        '- bash "$IGconf_shg_stage/installer/gateway/image/customize.sh"')


FAKE_SYSTEMCTL_CHROOT = """#!/bin/bash
# chroot-Attrappe fuer wlan_off.sh: protokolliert den Befehl und spielt systemctl disable/mask im Root-Dateisystem nach.
root="$1"; shift
echo "$*" >>"$SHG_FAKE_CHROOT_LOG"
[ "${SHG_FAKE_CHROOT_RC:-0}" = 0 ] || exit "$SHG_FAKE_CHROOT_RC"
[ "$1" = systemctl ] || exit 0
case "$2" in
  disable) rm -f "$root"/etc/systemd/system/*.wants/"$3" ;;
  mask) ln -sf /dev/null "$root/etc/systemd/system/$3" ;;
esac
"""


def _wlan_root(tmp_path: Path) -> Path:
    """Stand nach den Layern von v2.8.0: iwd aktiviert (enable-units), 01-eth0/02-wlan0.network (rpi-device-base),
    config.txt aus der Vorlage."""
    root = tmp_path / "root"
    wants = root / "etc/systemd/system/multi-user.target.wants"
    wants.mkdir(parents=True)
    (wants / "iwd.service").symlink_to("/usr/lib/systemd/system/iwd.service")
    (wants / "ssh.service").symlink_to("/usr/lib/systemd/system/ssh.service")
    net = root / "etc/systemd/network"
    net.mkdir(parents=True)
    (net / "01-eth0.network").write_text("[Match]\nName=eth0\n")
    (net / "02-wlan0.network").write_text("[Match]\nName=wlan0\n")
    (root / "boot/firmware").mkdir(parents=True)
    (root / "boot/firmware/config.txt").write_text("dtparam=audio=on\n[pi4]\nenable_uart=1\n[all]\nuart_2ndstage=1\n")
    return root


def _wlan_off(tmp_path: Path, root: Path, rc: int = 0) -> subprocess.CompletedProcess:
    bindir = tmp_path / "wlanbin"
    bindir.mkdir(exist_ok=True)
    (bindir / "chroot").write_text(FAKE_SYSTEMCTL_CHROOT)
    (bindir / "chroot").chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "SHG_FAKE_CHROOT_LOG": str(tmp_path / "wlan.log"),
           "SHG_FAKE_CHROOT_RC": str(rc)}
    return subprocess.run(["bash", str(IMAGE / "wlan_off.sh"), str(root)], capture_output=True, text=True,
                          check=False, env=env)


def test_wlan_off_masks_iwd_drops_the_wlan_network_and_disables_the_chip(tmp_path):
    root = _wlan_root(tmp_path)
    result = _wlan_off(tmp_path, root)
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "wlan.log").read_text().splitlines()
    assert calls == ["systemctl disable iwd.service", "systemctl mask iwd.service"]
    assert os.readlink(root / "etc/systemd/system/iwd.service") == "/dev/null"
    assert not (root / "etc/systemd/system/multi-user.target.wants/iwd.service").is_symlink()
    assert (root / "etc/systemd/system/multi-user.target.wants/ssh.service").is_symlink()
    assert sorted(p.name for p in (root / "etc/systemd/network").iterdir()) == ["01-eth0.network"]
    config = (root / "boot/firmware/config.txt").read_text()
    assert config.startswith("dtparam=audio=on\n[pi4]\nenable_uart=1\n[all]\nuart_2ndstage=1\n")
    assert config.rstrip().endswith("[all]\ndtoverlay=disable-wifi")
    assert "disable-bt" not in config  # Bluetooth bleibt unberuehrt (kein Bluetooth-Dienst im Image)
    # ein zweiter Lauf haengt nichts doppelt an
    assert _wlan_off(tmp_path, root).returncode == 0
    assert (root / "boot/firmware/config.txt").read_text() == config


def test_wlan_off_appends_its_own_block_even_if_the_overlay_appears_elsewhere(tmp_path):
    """Der Waechter gegen doppeltes Anhaengen erkennt nur den eigenen Block (Markierung, [all], Overlay): ein
    dtoverlay=disable-wifi in einem anderen Abschnitt (hier [pi5]) wirkt auf dem Pi 4 nicht und zaehlt nicht."""
    root = _wlan_root(tmp_path)
    config = root / "boot/firmware/config.txt"
    config.write_text("[pi5]\ndtoverlay=disable-wifi\n[all]\nuart_2ndstage=1\n")
    assert _wlan_off(tmp_path, root).returncode == 0
    text = config.read_text()
    assert text.startswith("[pi5]\ndtoverlay=disable-wifi\n[all]\nuart_2ndstage=1\n")
    marker = "# SmartHeat-Gateway: nur Ethernet, WLAN-Chip abgeschaltet (gateway/image/wlan_off.sh)"
    assert text.rstrip().endswith(f"{marker}\n[all]\ndtoverlay=disable-wifi")
    assert _wlan_off(tmp_path, root).returncode == 0
    assert config.read_text() == text and text.count(marker) == 1


def test_wlan_off_fails_without_config_txt(tmp_path):
    root = _wlan_root(tmp_path)
    (root / "boot/firmware/config.txt").unlink()
    assert _wlan_off(tmp_path, root).returncode != 0


def test_wlan_off_fails_when_systemctl_fails(tmp_path):
    assert _wlan_off(tmp_path, _wlan_root(tmp_path), rc=1).returncode != 0


def test_layer_runs_the_wlan_hook_before_customize():
    layer = (IMAGE / "layer" / "smartheat-gateway.yaml").read_text()
    assert '- bash "$SRCROOT/wlan_off.sh" "$1"' in layer
    assert layer.index('- bash "$SRCROOT/wlan_off.sh"') < layer.index(
        '- bash "$IGconf_shg_stage/installer/gateway/image/customize.sh"')
