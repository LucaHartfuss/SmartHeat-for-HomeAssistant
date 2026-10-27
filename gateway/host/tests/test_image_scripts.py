"""Image-Bau-Skripte ohne Docker: prepare.sh und make_image.sh lehnen fremde Adressen (IP, AWS,
http, Port, Pfad) ab, bevor sie etwas tun; prepare.sh legt mit einer Docker-Attrappe die Stage richtig an; Layer,
Konfiguration und Installer passen zusammen (alle Pakete aus dem Layer, kein apt im chroot)."""
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from kette_helper import root, roots_pem
from test_boundaries import SHIPPED, SHIPPED_CORE, TENANT_ID

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
if [ -n "$stage" ] && [ "${*: -1}" = "/src/gateway/image/build.sh" ] && [ -n "${SHG_FAKE_RELEASE_COPY:-}" ]; then
  cp "$stage/installer/gateway/host/release-roots.pem" "$SHG_FAKE_RELEASE_COPY"  # was der Build als Schluesselbund sieht
fi
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
    compose = (f"services:\n  init:\n    image: {GATEWAY}\n  runtime:\n    image: {GATEWAY}\n"
               f"  mosquitto:\n    image: {MOSQ}\n")
    (path / "docker-compose.yml").write_text(compose)
    (path / "mosquitto.conf").write_text("listener 1883\n")
    (path / "manifest.json").write_text('{"version": "0.3.0"}\n')
    (path / "manifest.json.sig").write_text("sig\n")
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
    (bundle / "manifest.json.sig").unlink()
    result = _script("prepare.sh", tmp_path, "--bundle", str(bundle), "--stage", str(tmp_path / "s"), *GOOD)
    assert result.returncode == 2 and "manifest.json.sig" in result.stderr


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
    shipped = {str(p.relative_to(installer / "src" / "smartheat_gateway")) for p in
               (installer / "src" / "smartheat_gateway").rglob("*") if p.is_file()}
    assert shipped == set(SHIPPED)  # genau das, was install.sh ablegt: nichts fehlt, nichts kommt dazu
    core = {str(p.relative_to(installer / "src" / "smartheat_core")) for p in
            (installer / "src" / "smartheat_core").rglob("*") if p.is_file()}
    assert core == set(SHIPPED_CORE)
    assert (installer / "VERSION").read_text() == (GW / "VERSION").read_text()
    for name in ("docker-compose.yml", "mosquitto.conf", "manifest.json", "manifest.json.sig"):
        assert (stage / "bundle" / name).is_file(), name
    # jedes Image genau einmal exportiert (init und runtime teilen sich das Gateway-Image)
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
    wanted |= set(re.search(r"wanted\+=\((docker-ce[^)]*)\)", install).group(1).split())
    assert wanted - _layer_packages() == set()


def test_layer_brings_the_diagnosis_tools_but_install_sh_does_not_need_the_raspberry_pi_archive():
    """Ss und ip (iproute2) braucht install.sh ebenso wie das Image; vcgencmd (raspi-utils) gibt es nur im
    Raspberry-Pi-Archiv, das das Image einbindet: ein Debian-Host ohne dieses Archiv installiert ohne das Paket."""
    install = (GW / "host" / "install.sh").read_text()
    wanted = set(re.search(r"local wanted=\(([^)]*)\)", install).group(1).split())
    wanted |= set(re.search(r"wanted\+=\((docker-ce[^)]*)\)", install).group(1).split())
    assert "iproute2" in wanted
    assert "raspi-utils" not in install
    assert {"iproute2", "raspi-utils"} <= _layer_packages()


def test_journal_syncs_every_30_seconds_and_is_persistent():
    """Nach hartem Stromverlust fehlen hoechstens die letzten 30 s (Standard 5 min); die Groesse bleibt begrenzt."""
    text = (GW / "host" / "journald.conf.d" / "smartheat.conf").read_text()
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    assert lines == ["[Journal]", "SystemMaxUse=200M", "SyncIntervalSec=30s", "Storage=persistent"]


def test_config_layer_and_hook_fit_together():
    config = (IMAGE / "config" / "smartheat-gateway.yaml").read_text()
    layer = (IMAGE / "layer" / "smartheat-gateway.yaml").read_text()
    name = re.search(r"^# X-Env-Layer-Name: (\S+)$", layer, re.MULTILINE).group(1)
    assert re.search(rf"^  shg: {name}$", config, re.MULTILINE)
    assert re.search(r"^# X-Env-VarPrefix: shg$", layer, re.MULTILINE)
    assert "$IGconf_shg_stage/installer/gateway/image/customize.sh" in layer
    last = layer.index('- bash "$IGconf_shg_stage/installer/gateway/image/customize.sh"')
    for hook in ("locale_default.sh", "wlan_off.sh", "eth_dhcp.sh", "ab_system.sh"):
        assert (IMAGE / hook).stat().st_mode & stat.S_IXUSR, hook
        assert layer.index(f'- bash "$SRCROOT/{hook}" "$1"') < last, hook
    for gone in ("usbboot.sh", "initramfs_check.sh", "post-build.sh"):
        assert not (IMAGE / gone).exists(), gone
        assert gone not in layer


def _config_value(section: str, key: str) -> str | None:
    config = (IMAGE / "config" / "smartheat-gateway.yaml").read_text()
    block = re.search(rf"^{section}:\n((?:[ \t]+.*\n|[ \t]*\n|#.*\n)+)", config, re.MULTILINE)
    assert block, section
    found = re.search(rf"^  {re.escape(key)}: (.+?)\s*$", block.group(1), re.MULTILINE)
    return found.group(1) if found else None


def test_config_builds_the_pi5_nvme_image_with_image_rota():
    """GW 6.1: Pi 5 mit NVMe, A/B-Layout image-rota mit ext4 (RAUC schreibt rohe ext4-Images), feste Groessen;
    die Pi-4-Reste (SD-Umweg fuer USB, feste MBR-Signatur) gibt es nicht mehr."""
    config = (IMAGE / "config" / "smartheat-gateway.yaml").read_text()
    assert _config_value("device", "layer") == "rpi5"
    assert _config_value("device", "storage_type") == "nvme"  # nvme verlangt die zwei Traits darunter
    assert re.search(r"^trait:\n  hw:storage:nvme: true\n  hw:pcie:m2: true$", config, re.MULTILINE)
    assert _config_value("image", "layer") == "image-rota"
    assert _config_value("image", "rootfs_type") == "ext4"
    assert _config_value("image", "boot_part_size") == "256M"
    assert _config_value("image", "system_part_size") == "6G"
    assert _config_value("image", "data_part_size") == "4G"
    assert _config_value("image", "name") == "smartheat-gateway"
    assert _config_value("image", "idp_enable") == "n"  # kein IDP-Dokument: die Provisionierungs-Map kennt werk nicht
    for gone in ("root_part_size", "disksig", "rpi4", "image-rpios", "storage_type: sd", "usbboot"):
        assert gone not in config, gone


def test_image_sbom_is_cyclonedx():
    """S-10: je Artefakt eine SBOM im Format CycloneDX; rpi-image-gen schreibt sonst SPDX-JSON (layer/sbom/syft.yaml)."""
    assert _config_value("sbom", "syft_config") == "${@SRCROOT}/sbom/syft.yaml"
    syft = (IMAGE / "sbom" / "syft.yaml").read_text()
    outputs = re.search(r"^output:\n((?:  - .+\n)+)", syft, re.MULTILINE)
    assert outputs and re.findall(r"^  - (.+)$", outputs.group(1), re.MULTILINE) == ["cyclonedx-json"]
    assert not [ln for ln in syft.splitlines() if "spdx" in ln.lower() and not ln.startswith("#")]
    build = (IMAGE / "build.sh").read_text()
    assert "SPDX" not in build and "CycloneDX" in build
    assert "SPDX" not in (GW / "CHANGELOG.md").read_text().split("## 0.8.0")[0]


def test_pins_are_the_same_everywhere():
    """Basis-Image per Digest und rpi-image-gen per Commit (v2.9.0), ueberall dieselben Werte."""
    digests = set()
    for path in [*IMAGE.glob("*.sh"), REPO / "tests" / "test_gateway_firstboot.sh"]:
        code = "\n".join(line for line in path.read_text().splitlines() if not line.lstrip().startswith("#"))
        digests |= set(re.findall(r"debian:trixie@sha256:[0-9a-f]{64}", code))
        assert not re.search(r"debian:trixie(?!@sha256:[0-9a-f]{64})\b", code), path
    assert len(digests) == 1
    assert "RIG_COMMIT=d2d5705bcb37284c4b08c0ce43c039b1f1004e25\n" in (IMAGE / "build.sh").read_text()  # Tag v2.9.0
    assert "v2.9.0" in (IMAGE / "config" / "smartheat-gateway.yaml").read_text()


def test_no_tenant_ids_in_image_files():
    for path in sorted(p for p in IMAGE.rglob("*") if p.is_file()):
        assert not TENANT_ID.search(path.read_text(encoding="utf-8", errors="ignore")), path


HOOK_NAME = re.compile(r"^(preimage|postbuild|finalize|deploy)[0-9]{2}-[a-zA-Z0-9-]+$")
HOOKS = ("preimage50-werk", "postbuild50-rootfs-checks", "finalize10-bootfs-checks", "deploy50-slot-images")


def test_source_tree_hooks_are_found_and_executable():
    """rpi-image-gen v2.9.0 sucht nur <Phase>NN-Name in SRCROOT/hooks mit Namensstamm aus [a-zA-Z0-9-]; ein nicht
    ausfuehrbarer Hook wird nur mit einer Warnung uebergangen (bin/runner), v2.8.0 ignorierte post-build.sh ganz
    still. Genau diese vier Hooks, jeder passt auf das Namensmuster und ist ausfuehrbar."""
    found = sorted(p.name for p in (IMAGE / "hooks").iterdir())
    assert found == sorted(HOOKS)
    for name in found:
        assert HOOK_NAME.match(name), name
        assert (IMAGE / "hooks" / name).stat().st_mode & stat.S_IXUSR, name


# Vorlage genimage.cfg.in.ext4 von rpi-image-gen v2.9.0 (Commit d2d5705), unveraendert; die Platzhalter <...> bleiben
# stehen, der Hook ersetzt nur Text.
GENIMAGE_V290 = Path(__file__).parent / "fixtures" / "genimage.cfg.in.ext4"


def _run_hook(name: str, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Wie der Runner: der Hook laeuft aus seinem eigenen Ordner."""
    return subprocess.run(["./" + name, *args], capture_output=True, text=True, check=False, cwd=IMAGE / "hooks",
                          env={**os.environ, **(env or {})})


def _partitions(cfg: str) -> list[str]:
    return re.findall(r"^\s*partition (\w+) \{", cfg, re.MULTILINE)


def test_preimage_werk_inserts_the_partition_before_persistent(tmp_path):
    cfg = tmp_path / "genimage.cfg"
    cfg.write_text(GENIMAGE_V290.read_text())
    assert _partitions(cfg.read_text()) == ["bootconfig", "boot_a", "boot_b", "system_a", "system_b", "persistent"]
    result = _run_hook("preimage50-werk", str(tmp_path / "rootfs"), str(tmp_path))
    assert result.returncode == 0, result.stderr
    text = cfg.read_text()
    assert _partitions(text) == ["bootconfig", "boot_a", "boot_b", "system_a", "system_b", "werk", "persistent"]
    # genau das Eingefuegte kommt dazu: die Partition vor persistent, das Image am Ende, sonst bleibt die Vorlage
    original = [ln for ln in GENIMAGE_V290.read_text().splitlines() if ln.strip()]
    at = original.index("   partition persistent {")
    partition = ["   partition werk {", "      in-partition-table = true", "      image = werk.ext4",
                 "      partition-type-uuid = L", "   }"]
    image = ["image werk.ext4 {", "   ext4 {", "      use-mke2fs = true", '      label = "werk"',
             '      extraargs = "-b 4096"', "   }", "   size = 16M", "   empty = true", "}"]  # leer, 4-K-Bloecke
    assert [ln for ln in text.splitlines() if ln.strip()] == original[:at] + partition + original[at:] + image


@pytest.mark.parametrize("breakit, why", [
    (lambda t: t.replace("   partition persistent {", "   partition daten {"), "Anker fehlt"),
    (lambda t: t + "\n   partition persistent {\n}\n", "Anker doppelt"),
    (lambda t: t.replace("   partition persistent {", "      partition persistent {"), "Anker eingerueckt"),
    (lambda t: t.replace("   partition boot_b {\n      in-partition-table = true\n      image = boot.vfat\n"
                         "      partition-type-uuid = F\n      bootable = true\n   }\n\n", ""), "Partition fehlt"),
    (lambda t: t + "\nimage werk.ext4 {\n}\n", "werk schon da"),
])
def test_preimage_werk_fails_loudly_when_the_template_changed(tmp_path, breakit, why):
    """Ein Upstream-Wechsel der Vorlage faellt im Bau auf: der Hook aendert nichts und scheitert mit Meldung."""
    original = breakit(GENIMAGE_V290.read_text())
    assert original != GENIMAGE_V290.read_text(), why
    cfg = tmp_path / "genimage.cfg"
    cfg.write_text(original)
    result = _run_hook("preimage50-werk", str(tmp_path / "rootfs"), str(tmp_path))
    assert result.returncode != 0 and "preimage50-werk:" in result.stderr, (why, result.stderr)
    assert cfg.read_text() == original and not (tmp_path / "genimage.cfg.werk").exists()


def test_preimage_werk_fails_without_genimage_cfg(tmp_path):
    result = _run_hook("preimage50-werk", str(tmp_path / "rootfs"), str(tmp_path))
    assert result.returncode != 0 and "genimage.cfg fehlt" in result.stderr


def test_genimage_template_fixture_is_the_one_the_layout_assumes():
    """Aenderte rpi-image-gen die Vorlage (Reihenfolge, Nummern), muessen autoboot.txt (Partition 2 und 3), der Hook und
    die RAUC-Slots (system.conf.in) mit; die Fixture haelt den Stand, auf den sich das alles bezieht."""
    names = _partitions(GENIMAGE_V290.read_text())
    assert names[:3] == ["bootconfig", "boot_a", "boot_b"] and names[-1] == "persistent"
    system_conf = (GW / "host" / "rauc" / "system.conf.in").read_text()
    for label in ("system_a", "system_b", "boot_a", "boot_b"):
        assert f"device=/dev/disk/by-partlabel/{label}" in system_conf and label in names


def _deploy_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    out = tmp_path / "image-out"
    out.mkdir(exist_ok=True)
    return {"IGconf_image_outputdir": str(out), "IGconf_artefact_version": "0.9.0", "IGconf_image_rootfs_type": "ext4",
            **extra}


def test_deploy_slot_images_copies_the_raw_slot_images(tmp_path):
    env = _deploy_env(tmp_path)
    out = Path(env["IGconf_image_outputdir"])
    (out / "system.ext4").write_bytes(b"ext4-roh")
    (out / "boot.vfat").write_bytes(b"vfat-roh")
    (out / "system.sparse").write_bytes(b"sparse-nicht")
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    result = _run_hook("deploy50-slot-images", str(deploy), env=env)
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in deploy.iterdir()) == ["boot-0.9.0.vfat", "system-0.9.0.ext4"]
    assert (deploy / "system-0.9.0.ext4").read_bytes() == b"ext4-roh"
    assert (deploy / "boot-0.9.0.vfat").read_bytes() == b"vfat-roh"


@pytest.mark.parametrize("missing", ["system.ext4", "boot.vfat"])
def test_deploy_slot_images_fails_when_a_slot_image_is_missing_or_empty(tmp_path, missing):
    env = _deploy_env(tmp_path)
    out = Path(env["IGconf_image_outputdir"])
    for name in ("system.ext4", "boot.vfat"):
        (out / name).write_bytes(b"" if name == missing else b"x")
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    result = _run_hook("deploy50-slot-images", str(deploy), env=env)
    assert result.returncode != 0 and missing in result.stderr


def test_deploy_slot_images_wants_ext4_and_the_environment(tmp_path):
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    env = _deploy_env(tmp_path, IGconf_image_rootfs_type="erofs")
    assert _run_hook("deploy50-slot-images", str(deploy), env=env).returncode != 0
    assert _run_hook("deploy50-slot-images", str(deploy), env={"IGconf_image_outputdir": ""}).returncode != 0


def test_make_image_refuses_pilot_ssh_until_ap05b(tmp_path):
    """Das Pilot-Image (SSH-Schluessel im Image) gibt es bis AP-05b nicht: der Aufruf bricht ab, bevor irgendetwas
    angelegt oder gestartet wird."""
    out = tmp_path / "out"
    result = _script("make_image.sh", tmp_path, "--bundle", str(_bundle(tmp_path / "b")), "--out", str(out), *GOOD,
                     "--pilot-ssh", "ssh-ed25519 AAAA test-key")
    assert result.returncode != 0 and "--pilot-ssh" in result.stderr and "AP-05b" in result.stderr, result.stderr
    assert not out.exists() and _docker_calls(tmp_path) == []


DOCKER_APT = IMAGE / "docker-apt"
DOCKER_PACKAGES = ("docker-ce", "docker-ce-cli", "containerd.io", "docker-compose-plugin")


def _docker_versions() -> dict[str, str]:
    entries = [line for line in (DOCKER_APT / "version").read_text().splitlines() if line and not line.startswith("#")]
    pairs = [line.split("=", 1) for line in entries]
    assert len(pairs) == len({name for name, _ in pairs}), "doppelter Eintrag in docker-apt/version"
    return dict(pairs)


def test_docker_comes_from_the_docker_archive_pinned_to_29():
    """FR-34: Docker >= 28 (Weiterleitung auf Loopback-Ports, GW-Weiterleitung) gibt es nicht im Debian-Archiv; vier
    Pakete exakt gepinnt, keine Platzhalter, docker-ce und -cli gleich."""
    versions = _docker_versions()
    assert set(versions) == {"fingerprint", *DOCKER_PACKAGES}
    assert versions["fingerprint"] == "9DC858229FC7DD38854AE2D88D81803C0EBFCD88"
    assert re.fullmatch(r"5:29\.\d+\.\d+-1~debian\.13~trixie", versions["docker-ce"])
    assert versions["docker-ce-cli"] == versions["docker-ce"]
    assert re.fullmatch(r"\d+\.\d+\.\d+-\d+~debian\.13~trixie", versions["containerd.io"])
    assert re.fullmatch(r"\d+\.\d+\.\d+-\d+~debian\.13~trixie", versions["docker-compose-plugin"])
    sources = (DOCKER_APT / "docker.sources").read_text()
    assert "URIs: https://download.docker.com/linux/debian\n" in sources and "Suites: trixie\n" in sources
    assert "X-IG-Signed-By: /usr/share/keyrings/docker.asc" in sources  # wird zu Signed-By (cleanup10-deb822-signedby)
    layer = (IMAGE / "layer" / "smartheat-gateway.yaml").read_text()
    assert "- ${SRCROOT}/docker-apt/docker.sources" in layer
    assert '- bash "$SRCROOT/docker_apt.sh" "$1"' in layer
    assert set(DOCKER_PACKAGES) <= _layer_packages()


def test_layer_packages_have_rauc_and_neither_docker_io_nor_unattended_upgrades():
    packages = _layer_packages()
    assert {"rauc", "rauc-service"} <= packages
    assert packages.isdisjoint({"docker.io", "docker-cli", "docker-compose", "unattended-upgrades"})
    assert not (GW / "host" / "apt").exists()
    install = (GW / "host" / "install.sh").read_text()
    assert "unattended" not in install and "apt.conf.d" not in install


@pytest.fixture
def need_gpg():
    assert shutil.which("gpg"), "gpg fehlt: docker_apt.sh und dieser Test brauchen es (Paket gnupg)"


def _docker_apt_copy(tmp_path: Path) -> Path:
    """docker_apt.sh samt docker-apt/ in einen eigenen Ordner (das Skript liest neben sich), zum Verfaelschen."""
    scripts = tmp_path / "scripts"
    shutil.copytree(DOCKER_APT, scripts / "docker-apt")
    shutil.copy(IMAGE / "docker_apt.sh", scripts / "docker_apt.sh")
    return scripts


def _docker_apt(scripts: Path, root: Path, keydir: Path | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "IGconf_sys_apt_keydir"}
    if keydir is not None:
        env["IGconf_sys_apt_keydir"] = str(keydir)
    return subprocess.run(["bash", str(scripts / "docker_apt.sh"), str(root)], capture_output=True, text=True,
                          check=False, env=env)


def test_docker_apt_trusts_the_key_and_pins_the_four_packages(tmp_path, need_gpg):
    root, keydir = tmp_path / "root", tmp_path / "keydir"
    result = _docker_apt(_docker_apt_copy(tmp_path), root, keydir)
    assert result.returncode == 0, result.stderr
    key = (DOCKER_APT / "docker.asc").read_bytes()
    assert (root / "usr/share/keyrings/docker.asc").read_bytes() == key
    assert (keydir / "docker.asc").read_bytes() == key
    pref = (root / "etc/apt/preferences.d/smartheat-docker.pref").read_text()
    versions = _docker_versions()
    stanzas = {m.group(1): (m.group(2), m.group(3)) for m in
               re.finditer(r"Package: (\S+)\nPin: version (\S+)\nPin-Priority: (\d+)\n", pref)}
    assert stanzas == {pkg: (versions[pkg], "1001") for pkg in DOCKER_PACKAGES}


def test_docker_apt_needs_no_key_directory(tmp_path, need_gpg):
    assert _docker_apt(_docker_apt_copy(tmp_path), tmp_path / "root").returncode == 0


def test_docker_apt_refuses_a_key_with_another_fingerprint(tmp_path, need_gpg):
    scripts = _docker_apt_copy(tmp_path)
    home = tmp_path / "gnupg"
    home.mkdir(mode=0o700)
    gpg = ["gpg", "--homedir", str(home), "--batch", "--quiet"]
    try:
        subprocess.run([*gpg, "--passphrase", "", "--quick-generate-key", "Fremd <fremd@example.test>", "ed25519", "sign",
                        "never"], check=True, capture_output=True)
        other = subprocess.run([*gpg, "--armor", "--export"], check=True, capture_output=True).stdout
    finally:  # der gpg-Agent des Testschluesselbunds bliebe sonst als Hintergrundprozess stehen
        subprocess.run(["gpgconf", "--homedir", str(home), "--kill", "all"], check=False, capture_output=True)
    (scripts / "docker-apt" / "docker.asc").write_bytes(other)
    root = tmp_path / "root"
    result = _docker_apt(scripts, root)
    assert result.returncode != 0 and "Fingerabdruck" in result.stderr
    assert not (root / "usr").exists() and not (root / "etc").exists()  # nichts vertraut, nichts gepinnt


def test_docker_apt_refuses_a_file_that_is_no_key(tmp_path, need_gpg):
    scripts = _docker_apt_copy(tmp_path)
    (scripts / "docker-apt" / "docker.asc").write_text("kein Schluessel\n")
    result = _docker_apt(scripts, tmp_path / "root")
    assert result.returncode != 0 and not (tmp_path / "root" / "usr").exists()


@pytest.mark.parametrize("edit, message", [
    (lambda t: t.replace("docker-ce-cli=", "docker-ce-cli-x="), "docker-ce-cli fehlt"),
    (lambda t: t.replace("fingerprint=9DC8", "fingerprint=9dc8"), "kein Fingerabdruck"),
    (lambda t: t + "docker-ce=5:29.0.0-1~debian.13~trixie\n", "doppelt"),
    (lambda t: t + "docker-ce 5:29\n", "unlesbare Zeile"),
])
def test_docker_apt_refuses_a_broken_version_file(tmp_path, need_gpg, edit, message):
    scripts = _docker_apt_copy(tmp_path)
    version = scripts / "docker-apt" / "version"
    version.write_text(edit(version.read_text()))
    result = _docker_apt(scripts, tmp_path / "root")
    assert result.returncode != 0 and message in result.stderr, result.stderr
    assert not (tmp_path / "root" / "etc").exists()


def test_overlay_declares_the_shared_paths_the_watchdog_and_the_werk_mount():
    overlay = IMAGE / "layer" / "smartheat-gateway.d" / "customize.overlay" / "etc"
    shared = [ln for ln in (overlay / "rpi-image-gen/slot-shared.d/smartheat.conf").read_text().splitlines()
              if ln and not ln.startswith("#")]
    assert shared == ["Version=1", "Path=/var/lib/smartheat", "Path=/var/lib/docker", "Path=/var/lib/containerd",
                      "Path=/var/lib/rauc", "Path=/var/lib/systemd/timesync"]
    watchdog = [ln for ln in (overlay / "systemd/system.conf.d/smartheat-watchdog.conf").read_text().splitlines()
                if ln and not ln.startswith("#")]
    # GW 6.3 (21): Watchdog 10 s; Neustart-Watchdog 5 min (Rueckfall hoechstens 20 min Frist + 5 min Shutdown, R9)
    assert watchdog == ["[Manager]", "RuntimeWatchdogSec=10s", "RebootWatchdogSec=5min"]
    mount = [ln for ln in (overlay / "systemd/system/werk.mount").read_text().splitlines()
             if ln and not ln.startswith("#")]
    assert "What=/dev/disk/by-partlabel/werk" in mount and "Where=/werk" in mount and "Type=ext4" in mount
    assert "Options=ro,nodev,nosuid,noexec,noatime,nofail" in mount  # nofail: keine 90 s Wartezeit ohne werk
    assert "WantedBy=multi-user.target" in mount  # nicht local-fs.target: eine fehlende Partition haelt den Start nicht auf
    assert not any(ln.startswith(("Requires", "Before=local-fs")) for ln in mount)


def test_kernel_and_firmware_fragments_set_panic_and_switch_off_wifi_and_bluetooth():
    fragments = IMAGE / "layer" / "smartheat-gateway.d" / "for"
    assert (fragments / "linux-base/cmdline.d/80-smartheat-panic").read_text() == "panic=10\n"
    config = [ln for ln in (fragments / "rpi-boot-firmware/config.txt.d/80-smartheat-funk").read_text().splitlines()
              if ln and not ln.startswith("#")]
    assert config == ["[pi5]", "dtoverlay=disable-wifi-pi5", "dtoverlay=disable-bt-pi5", "dtoverlay="]


def test_system_conf_is_laid_down_by_customize():
    """R1: /etc/rauc/system.conf entsteht beim Bau aus host/rauc/system.conf.in (rootfs_checks.sh verlangt sie)."""
    customize = (IMAGE / "customize.sh").read_text()
    assert 'host/rauc/system.conf.in" "$R/etc/rauc/system.conf"' in customize
    assert (GW / "host" / "rauc" / "system.conf.in").is_file()
    lines = (GW / "host" / "rauc" / "system.conf.in").read_text().splitlines()
    assert "bootname=A" in lines and "bootname=B" in lines


def test_ab_system_hook_enables_werk_adds_rauc_to_the_boot_target_and_masks_rauc_mark_good(tmp_path):
    env, log = _fake_chroot(tmp_path)
    root = tmp_path / "root"
    (root / "etc/systemd/system").mkdir(parents=True)
    (root / "etc/systemd/system/werk.mount").write_text("[Mount]\n")
    result = subprocess.run(["bash", str(IMAGE / "ab_system.sh"), str(root)], capture_output=True, text=True,
                            check=False, env=env)
    assert result.returncode == 0, result.stderr
    assert (root / "werk").is_dir()
    assert log.read_text().splitlines() == [f"{root}|systemctl enable werk.mount",
                                            f"{root}|systemctl add-wants multi-user.target rauc.service",
                                            f"{root}|systemctl mask rauc-mark-good.service"]


def test_ab_system_hook_fails_without_the_werk_unit_or_when_systemctl_fails(tmp_path):
    env, log = _fake_chroot(tmp_path)
    root = tmp_path / "root"
    root.mkdir()
    result = subprocess.run(["bash", str(IMAGE / "ab_system.sh"), str(root)], capture_output=True, text=True,
                            check=False, env=env)
    assert result.returncode != 0 and "werk.mount fehlt" in result.stderr and not log.exists()
    (root / "etc/systemd/system").mkdir(parents=True)
    (root / "etc/systemd/system/werk.mount").write_text("[Mount]\n")
    env["SHG_FAKE_CHROOT_RC"] = "1"
    result = subprocess.run(["bash", str(IMAGE / "ab_system.sh"), str(root)], capture_output=True, text=True,
                            check=False, env=env)
    assert result.returncode != 0


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
    """Stand nach den Layern von v2.9.0: iwd aktiviert (enable-units), 01-eth0/02-wlan0.network (rpi-device-base).
    config.txt gibt es in der Phase customize nicht (entsteht erst in preimage)."""
    root = tmp_path / "root"
    wants = root / "etc/systemd/system/multi-user.target.wants"
    wants.mkdir(parents=True)
    (wants / "iwd.service").symlink_to("/usr/lib/systemd/system/iwd.service")
    (wants / "ssh.service").symlink_to("/usr/lib/systemd/system/ssh.service")
    net = root / "etc/systemd/network"
    net.mkdir(parents=True)
    (net / "01-eth0.network").write_text("[Match]\nName=eth0\n")
    (net / "02-wlan0.network").write_text("[Match]\nName=wlan0\n")
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


def test_wlan_off_masks_iwd_and_drops_the_wlan_network(tmp_path):
    root = _wlan_root(tmp_path)
    result = _wlan_off(tmp_path, root)
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "wlan.log").read_text().splitlines()
    assert calls == ["systemctl disable iwd.service", "systemctl mask iwd.service"]
    assert os.readlink(root / "etc/systemd/system/iwd.service") == "/dev/null"
    assert not (root / "etc/systemd/system/multi-user.target.wants/iwd.service").is_symlink()
    assert (root / "etc/systemd/system/multi-user.target.wants/ssh.service").is_symlink()
    assert sorted(p.name for p in (root / "etc/systemd/network").iterdir()) == ["01-eth0.network"]
    # config.txt gibt es hier nicht und der Hook legt keine an: den Chip schaltet das Fragment 80-smartheat-funk ab
    assert not (root / "boot").exists()
    assert _wlan_off(tmp_path, root).returncode == 0  # ein zweiter Lauf ist harmlos


def test_wlan_off_fails_when_systemctl_fails(tmp_path):
    assert _wlan_off(tmp_path, _wlan_root(tmp_path), rc=1).returncode != 0


def _eth_dhcp(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(IMAGE / "eth_dhcp.sh"), str(root)], capture_output=True, text=True, check=False)


def test_eth_dhcp_asks_for_ipv4_with_the_mac_as_client_id(tmp_path):
    """Labor 2026-10-20 (H2): mit dem Standard von systemd-networkd (Client-ID aus der DUID) bekam das Gateway am
    Router des Labors nie ein DHCPv4-Angebot, mit der MAC als Client-ID sofort. Die Docker-Netze sind nur IPv4, ohne
    IPv4-Adresse erreicht die Laufzeit den Server nicht."""
    root = _wlan_root(tmp_path)
    result = _eth_dhcp(root)
    assert result.returncode == 0, result.stderr
    dropin = root / "etc/systemd/network/01-eth0.network.d/10-smartheat-client-id.conf"
    lines = [line for line in dropin.read_text().splitlines() if line and not line.startswith("#")]
    assert lines == ["[DHCPv4]", "ClientIdentifier=mac"]
    assert (root / "etc/systemd/network/01-eth0.network").read_text() == "[Match]\nName=eth0\n"  # unveraendert
    text = dropin.read_text()
    assert _eth_dhcp(root).returncode == 0 and dropin.read_text() == text  # ein zweiter Lauf aendert nichts


def test_eth_dhcp_fails_without_the_eth0_network(tmp_path):
    root = _wlan_root(tmp_path)
    (root / "etc/systemd/network/01-eth0.network").unlink()
    result = _eth_dhcp(root)
    assert result.returncode != 0 and "01-eth0.network" in result.stderr
    assert not (root / "etc/systemd/network/01-eth0.network.d").exists()


def test_layer_runs_the_eth_dhcp_hook_before_customize():
    layer = (IMAGE / "layer" / "smartheat-gateway.yaml").read_text()
    assert '- bash "$SRCROOT/eth_dhcp.sh" "$1"' in layer
    assert layer.index('- bash "$SRCROOT/eth_dhcp.sh"') < layer.index(
        '- bash "$IGconf_shg_stage/installer/gateway/image/customize.sh"')


def test_layer_runs_the_wlan_hook_before_customize():
    layer = (IMAGE / "layer" / "smartheat-gateway.yaml").read_text()
    assert '- bash "$SRCROOT/wlan_off.sh" "$1"' in layer
    assert layer.index('- bash "$SRCROOT/wlan_off.sh"') < layer.index(
        '- bash "$IGconf_shg_stage/installer/gateway/image/customize.sh"')


def _roots_pem(path: Path, *namen: str) -> Path:
    """Schluesselbund (PEM) mit je einer selbstsignierten Root je Name (Standard: eine)."""
    path.write_text(roots_pem(*(root(cn=name)[1] for name in namen or ("Labor Root",))))
    return path


def _tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


_RELEASE_ROOTS = Path("installer/gateway/host/release-roots.pem")


def test_release_roots_defaults_to_the_shipped_bundle(tmp_path):
    stage = tmp_path / "stage"
    result = _script("prepare.sh", tmp_path, "--bundle", str(_bundle(tmp_path / "b")), "--stage", str(stage), *GOOD)
    assert result.returncode == 0, result.stderr
    assert (stage / _RELEASE_ROOTS).read_bytes() == (GW / "host" / "release-roots.pem").read_bytes()


def test_release_roots_option_replaces_only_that_file(tmp_path):
    mine = _roots_pem(tmp_path / "labor.pem")
    bundle = _bundle(tmp_path / "b")
    plain, labor = tmp_path / "plain", tmp_path / "labor"
    assert _script("prepare.sh", tmp_path, "--bundle", str(bundle), "--stage", str(plain), *GOOD).returncode == 0
    result = _script("prepare.sh", tmp_path, "--bundle", str(bundle), "--stage", str(labor), *GOOD,
                     "--release-roots", str(mine))
    assert result.returncode == 0, result.stderr
    assert (labor / _RELEASE_ROOTS).read_bytes() == mine.read_bytes() != (plain / _RELEASE_ROOTS).read_bytes()
    before, after = _tree(plain), _tree(labor)
    before.pop(str(_RELEASE_ROOTS)), after.pop(str(_RELEASE_ROOTS))
    assert after == before  # nichts sonst aendert sich (Installer, Bundle, Images, install.args)


def test_release_roots_option_takes_a_bundle_of_several_roots(tmp_path):
    mine = _roots_pem(tmp_path / "zwei.pem", "Erste Root", "Zweite Root")
    result = _script("prepare.sh", tmp_path, "--bundle", str(_bundle(tmp_path / "b")), "--stage",
                     str(tmp_path / "stage"), *GOOD, "--release-roots", str(mine))
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "stage" / _RELEASE_ROOTS).read_bytes() == mine.read_bytes()


def _bad_release_roots(tmp_path: Path) -> dict[str, Path]:
    good = _roots_pem(tmp_path / "gut.pem").read_text()
    body = good.splitlines()[1]
    cases = {
        "fehlt": tmp_path / "gibt-es-nicht.pem",
        "verzeichnis": tmp_path,
        "leer": "",
        "nur-leerzeilen": "\n\n",
        "platzhalter": (GW / "host" / "release-roots.pem"),
        "minisign-schluessel": "untrusted comment: minisign public key\nRWQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\n",
        "privater-schluessel": good.replace("CERTIFICATE", "PRIVATE " + "KEY"),  # Armor eines Schluessels statt Zertifikat
        "begin-ohne-end": good.replace("-----END CERTIFICATE-----\n", ""),
        "end-ohne-begin": good.replace("-----BEGIN CERTIFICATE-----\n", ""),
        "leerer-koerper": "-----BEGIN CERTIFICATE-----\n-----END CERTIFICATE-----\n",
        "kein-base64": good.replace(body, body[:-3] + "!!!"),
        "text-davor": "Root von Hand\n" + good,
        "text-danach": good + "Ende\n",
        "verschachtelt": good.replace("-----END CERTIFICATE-----\n", "-----BEGIN CERTIFICATE-----\n-----END CERTIFICATE-----\n"),
        "zweites-ohne-ende": good + good.splitlines()[0] + "\n" + body + "\n",
    }
    out = {}
    for name, value in cases.items():
        if isinstance(value, Path):
            out[name] = value
        else:
            out[name] = tmp_path / f"{name}.pem"
            out[name].write_text(value)
    return out


@pytest.mark.parametrize("name", ["fehlt", "verzeichnis", "leer", "nur-leerzeilen", "platzhalter",
                                  "minisign-schluessel", "privater-schluessel", "begin-ohne-end", "end-ohne-begin",
                                  "leerer-koerper", "kein-base64", "text-davor", "text-danach", "verschachtelt",
                                  "zweites-ohne-ende"])
def test_release_roots_rejects_files_that_are_no_certificate_bundle(tmp_path, name):
    bad = _bad_release_roots(tmp_path)[name]
    stage, out = tmp_path / "stage", tmp_path / "out"
    result = _script("prepare.sh", tmp_path, "--bundle", str(_bundle(tmp_path / "b")), "--stage", str(stage), *GOOD,
                     "--release-roots", str(bad))
    assert result.returncode == 2 and "FEHLER: --release-roots:" in result.stderr, result.stderr
    assert not stage.exists() and _docker_calls(tmp_path) == []
    result = _script("make_image.sh", tmp_path, "--bundle", str(tmp_path / "b"), "--out", str(out), *GOOD,
                     "--release-roots", str(bad))
    assert result.returncode == 2 and "FEHLER: --release-roots:" in result.stderr, result.stderr
    assert not out.exists() and _docker_calls(tmp_path) == []


@pytest.mark.parametrize("script, args", [("prepare.sh", ["--stage", "s"]), ("make_image.sh", ["--out", "o"])])
def test_release_roots_needs_a_value(tmp_path, script, args):
    result = _script(script, tmp_path, "--bundle", str(_bundle(tmp_path / "b")), *args, *GOOD, "--release-roots")
    assert result.returncode == 2 and "--release-roots braucht einen Wert" in result.stderr
    assert _docker_calls(tmp_path) == []


def test_make_image_passes_release_roots_to_prepare(tmp_path):
    mine = _roots_pem(tmp_path / "labor.pem")
    seen = tmp_path / "seen.pem"
    env_before = _env(tmp_path)
    env_before["SHG_FAKE_RELEASE_COPY"] = str(seen)
    result = subprocess.run(["bash", str(IMAGE / "make_image.sh"), "--bundle", str(_bundle(tmp_path / "b")), "--out",
                             str(tmp_path / "out"), *GOOD, "--release-roots", str(mine)],
                            capture_output=True, text=True, check=False, env=env_before)
    assert result.returncode == 0, result.stderr
    assert seen.read_bytes() == mine.read_bytes()


def test_make_image_without_release_roots_ships_the_default_bundle(tmp_path):
    seen = tmp_path / "seen.pem"
    env = _env(tmp_path)
    env["SHG_FAKE_RELEASE_COPY"] = str(seen)
    result = subprocess.run(["bash", str(IMAGE / "make_image.sh"), "--bundle", str(_bundle(tmp_path / "b")), "--out",
                             str(tmp_path / "out"), *GOOD], capture_output=True, text=True, check=False, env=env)
    assert result.returncode == 0, result.stderr
    assert seen.read_bytes() == (GW / "host" / "release-roots.pem").read_bytes()


def test_no_workflow_uses_release_roots():
    """Waechter: --release-roots ist ein Schalter fuer Labor-Images von Hand; ein Workflow darf damit keinen
    Schluesselbund fuer ein ausgeliefertes Image tauschen."""
    workflows = sorted((REPO / ".github" / "workflows").glob("*.y*ml"))
    assert workflows
    assert [w.name for w in workflows if "--release-roots" in w.read_text()] == []
