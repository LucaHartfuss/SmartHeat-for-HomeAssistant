import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from build_bundle import GATEWAY_SERVICES, MIN_UPDATER_VERSION, build, main, render_compose
from smartheat_host import bundles

GW = Path(__file__).resolve().parents[2]
COMPOSE = GW / "compose" / "docker-compose.yml"
MOSQUITTO_CONF = GW / "compose" / "mosquitto.conf"
IMAGE = "ghcr.io/lucahartfuss/smartheat-gateway@sha256:" + "a" * 64


def test_render_replaces_build_by_the_digest_for_every_gateway_service():
    text = render_compose(COMPOSE.read_text(), IMAGE)
    services = yaml.safe_load(text)["services"]
    for name in GATEWAY_SERVICES:
        assert services[name]["image"] == IMAGE and "build" not in services[name], name
    assert services["mosquitto"]["image"].startswith("eclipse-mosquitto:2@sha256:")
    assert services["zigbee2mqtt"]["image"].startswith("koenkk/zigbee2mqtt:2.14.2@sha256:")
    bundles.check_compose(text)  # das Geraet akzeptiert genau diese Datei


def test_render_keeps_variables_tmpfs_masks_and_string_values():
    text = render_compose(COMPOSE.read_text(), IMAGE)
    assert "${SHG_ROOT:-/var/lib/smartheat}" in text
    assert "${SHG_DEVICE_API_URL:?SHG_DEVICE_API_URL fehlt}" in text
    services = yaml.safe_load(text)["services"]
    assert services["init"]["restart"] == "no"  # String, kein YAML-1.1-Bool
    assert isinstance(services["init"]["restart"], str)
    mask = {"type": "tmpfs", "target": "/data/device", "read_only": True, "tmpfs": {"size": 4096}}
    assert mask in services["tunnel"]["volumes"] and mask in services["runtime"]["volumes"]
    assert services["init"]["network_mode"] == "none"
    assert services["runtime"]["network_mode"] == "service:tunnel"
    assert services["mosquitto"]["user"] == "1000:1000"


def test_render_has_no_yaml_anchors_or_extension_keys():
    text = render_compose(COMPOSE.read_text(), IMAGE)
    assert not any(marker in text for marker in ("&id", "*id", "x-hardening", "x-gateway", "x-after-init"))
    assert [key for key in yaml.safe_load(text) if key.startswith("x-")] == []


def test_render_keeps_hardening_and_dependencies_of_the_source():
    source = yaml.safe_load(COMPOSE.read_text())["services"]
    rendered = yaml.safe_load(render_compose(COMPOSE.read_text(), IMAGE))["services"]
    for name, service in source.items():
        for key, value in service.items():
            if key not in ("build", "image"):
                assert rendered[name][key] == value, (name, key)


def test_build_writes_files_and_a_manifest_the_device_accepts(tmp_path):
    manifest = build(COMPOSE, MOSQUITTO_CONF, IMAGE, "0.2.0", tmp_path)
    parsed = bundles.parse_manifest((tmp_path / "manifest.json").read_bytes())
    assert parsed.version == "0.2.0" and parsed.min_updater_version == MIN_UPDATER_VERSION
    assert sorted(p.name for p in tmp_path.iterdir()) == ["docker-compose.yml", "manifest.json", "mosquitto.conf"]
    assert manifest["images"]["gateway"] == IMAGE
    assert set(manifest["images"]) == {"gateway", "eclipse-mosquitto", "zigbee2mqtt"}
    assert parsed.images == manifest["images"]


def test_manifest_hashes_match_the_written_files(tmp_path):
    import hashlib

    build(COMPOSE, MOSQUITTO_CONF, IMAGE, "0.2.0", tmp_path)
    parsed = bundles.parse_manifest((tmp_path / "manifest.json").read_bytes())
    for name in bundles.MANIFEST_FILES:
        assert parsed.files[name] == hashlib.sha256((tmp_path / name).read_bytes()).hexdigest(), name
    assert (tmp_path / "mosquitto.conf").read_bytes() == MOSQUITTO_CONF.read_bytes()
    bundles.check_compose((tmp_path / "docker-compose.yml").read_text())


def test_build_is_reproducible(tmp_path):
    build(COMPOSE, MOSQUITTO_CONF, IMAGE, "0.2.0", tmp_path / "a")
    build(COMPOSE, MOSQUITTO_CONF, IMAGE, "0.2.0", tmp_path / "b")
    for name in ("docker-compose.yml", "manifest.json", "mosquitto.conf"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes(), name


@pytest.mark.parametrize("image", ["ghcr.io/x/gw:latest", "ghcr.io/x/gw@sha256:abc", "gw@sha256:" + "A" * 64])
def test_image_without_valid_digest_is_refused(tmp_path, image):
    with pytest.raises(SystemExit):
        build(COMPOSE, MOSQUITTO_CONF, image, "0.2.0", tmp_path)
    assert not (tmp_path / "manifest.json").exists()


def test_invalid_version_is_refused(tmp_path):
    with pytest.raises(bundles.ManifestError):
        build(COMPOSE, MOSQUITTO_CONF, IMAGE, "v0.2", tmp_path)
    assert not (tmp_path / "manifest.json").exists()


def test_cli_writes_the_bundle(tmp_path, capsys):
    code = main(["--compose", str(COMPOSE), "--mosquitto-conf", str(MOSQUITTO_CONF), "--image", IMAGE,
                 "--version", "1.2.3", "--out", str(tmp_path)])
    assert code == 0
    assert set(json.loads(capsys.readouterr().out)) == set(bundles.MANIFEST_FILES)
    assert bundles.parse_manifest((tmp_path / "manifest.json").read_bytes()).version == "1.2.3"


def test_script_runs_without_pythonpath(tmp_path):
    script = Path(__file__).resolve().parents[1] / "build_bundle.py"
    result = subprocess.run(
        [sys.executable, str(script), "--compose", str(COMPOSE), "--mosquitto-conf", str(MOSQUITTO_CONF),
         "--image", IMAGE, "--version", "0.2.0", "--out", str(tmp_path)],
        capture_output=True, text=True, env={"PATH": ""}, check=False)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "manifest.json").is_file()
