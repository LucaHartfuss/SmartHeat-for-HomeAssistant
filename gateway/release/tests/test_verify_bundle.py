import json
import subprocess
import sys
from pathlib import Path

import pytest

from build_bundle import build
from verify_bundle import BundleRejected, main, verify

GW = Path(__file__).resolve().parents[2]
COMPOSE = GW / "compose" / "docker-compose.yml"
MOSQUITTO_CONF = GW / "compose" / "mosquitto.conf"
REPO = "ghcr.io/lucahartfuss/smartheat-gateway"
IMAGE = REPO + "@sha256:" + "a" * 64
SCRIPT = Path(__file__).resolve().parents[1] / "verify_bundle.py"


@pytest.fixture
def bundle(tmp_path):
    out = tmp_path / "bundle"
    build(COMPOSE, MOSQUITTO_CONF, IMAGE, "0.2.0", out)
    return out


def _built_from(tmp_path, compose_text: str, image: str = IMAGE) -> Path:
    """Ein in sich stimmiges Bundle (Manifest passt zu den Dateien) aus einer anderen Quelle - so saehe die Ausgabe
    eines kompromittierten Build-Jobs aus."""
    source = tmp_path / "source-compose.yml"
    source.write_text(compose_text)
    out = tmp_path / "evil"
    build(source, MOSQUITTO_CONF, image, "0.2.0", out)
    return out


def test_bundle_built_from_the_tag_passes(bundle):
    verify(bundle, "0.2.0", REPO)


def test_privileged_compose_from_a_compromised_build_is_refused(tmp_path):
    text = COMPOSE.read_text().replace("    devices:", "    privileged: true\n    devices:", 1)
    assert "privileged: true" in text
    evil = _built_from(tmp_path, text)
    with pytest.raises(BundleRejected, match="docker-compose.yml weicht"):
        verify(evil, "0.2.0", REPO)


def test_foreign_third_party_digest_is_refused(tmp_path):
    text = COMPOSE.read_text()
    digest = text.split("eclipse-mosquitto:2@sha256:", 1)[1][:64]
    evil = _built_from(tmp_path, text.replace(digest, "f" * 64))
    with pytest.raises(BundleRejected, match="weicht"):
        verify(evil, "0.2.0", REPO)


@pytest.mark.parametrize("image", ["ghcr.io/evil/smartheat-gateway@sha256:" + "a" * 64,
                                   "localhost:5000/smartheat-gateway@sha256:" + "a" * 64,
                                   "ghcr.io/lucahartfuss/smartheat-gateway-x@sha256:" + "a" * 64])
def test_foreign_gateway_image_is_refused(tmp_path, image):
    evil = _built_from(tmp_path, COMPOSE.read_text(), image)
    with pytest.raises(BundleRejected, match="images.gateway"):
        verify(evil, "0.2.0", REPO)


def test_reformatted_manifest_is_refused(bundle):
    manifest = json.loads((bundle / "manifest.json").read_text())
    (bundle / "manifest.json").write_text(json.dumps(manifest))  # gleicher Inhalt, andere Bytes
    with pytest.raises(BundleRejected, match="manifest.json weicht"):
        verify(bundle, "0.2.0", REPO)


def test_file_that_does_not_match_its_manifest_hash_is_refused(bundle):
    (bundle / "mosquitto.conf").write_text("listener 1883\n")
    with pytest.raises(BundleRejected, match="SHA-256"):
        verify(bundle, "0.2.0", REPO)


def test_extra_or_missing_files_and_other_versions_are_refused(bundle):
    (bundle / "extra.sh").write_text("echo\n")
    with pytest.raises(BundleRejected, match="enthaelt"):
        verify(bundle, "0.2.0", REPO)
    (bundle / "extra.sh").unlink()
    with pytest.raises(BundleRejected, match="passt nicht zur Release-Version"):
        verify(bundle, "0.9.9", REPO)
    (bundle / "mosquitto.conf").unlink()
    with pytest.raises(BundleRejected, match="enthaelt"):
        verify(bundle, "0.2.0", REPO)


def test_unreadable_manifest_is_refused(bundle):
    (bundle / "manifest.json").write_text("{kaputt")
    with pytest.raises(BundleRejected, match="Manifest"):
        verify(bundle, "0.2.0", REPO)


def test_cli_exit_codes(bundle, capsys):
    assert main([str(bundle), "0.2.0", "--image-repo", REPO]) == 0
    assert main([str(bundle), "0.2.0", "--image-repo", "localhost:5000/smartheat-gateway"]) == 1
    assert "images.gateway" in capsys.readouterr().err


def test_script_runs_without_pythonpath(bundle):
    result = subprocess.run([sys.executable, str(SCRIPT), str(bundle), "0.2.0", "--image-repo", REPO],
                            capture_output=True, text=True, env={"PATH": ""}, check=False)
    assert result.returncode == 0, result.stderr
