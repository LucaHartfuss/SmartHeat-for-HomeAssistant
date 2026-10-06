import base64
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from build_bundle import build

GW = Path(__file__).resolve().parents[2]
IMAGE = "localhost:5000/smartheat-gateway@sha256:" + "a" * 64
SCRIPT = Path(__file__).resolve().parents[1] / "sign_bundle.sh"
pytestmark = pytest.mark.skipif(shutil.which("minisign") is None, reason="minisign nicht installiert (CI-Job test)")


def _run(bundle: Path, pub: Path, tmp: Path, *args: str, **env: str):
    environment = {**os.environ, "PYTHON": sys.executable, "TMPDIR": str(tmp), **env}
    if "MINISIGN_SECRET_KEY" not in env:
        environment.pop("MINISIGN_SECRET_KEY", None)
        environment.pop("MINISIGN_PASSWORD", None)
    return subprocess.run(["bash", str(SCRIPT), str(bundle), "0.2.0", str(pub), *args], env=environment,
                          capture_output=True, text=True, check=False)


@pytest.fixture
def bundle(tmp_path):
    out = tmp_path / "bundle"
    build(GW / "compose" / "docker-compose.yml", GW / "compose" / "mosquitto.conf", IMAGE, "0.2.0", out)
    (tmp_path / "work").mkdir()
    return out


def test_dryrun_signs_with_a_throwaway_key_and_verifies(bundle, tmp_path):
    pub = tmp_path / "test.pub"
    result = _run(bundle, pub, tmp_path / "work", "--dryrun")
    assert result.returncode == 0, result.stderr
    assert "OK: smartheat-gateway 0.2.0" in result.stdout
    assert (bundle / "manifest.json.minisig").is_file() and pub.is_file()
    assert list((tmp_path / "work").iterdir()) == []  # Schluessel und Arbeitsordner sind weg


def test_release_path_signs_with_the_secret_from_the_environment(bundle, tmp_path):
    key, pub = tmp_path / "k.key", tmp_path / "k.pub"
    generate = subprocess.run(["minisign", "-G", "-p", str(pub), "-s", str(key)], input="test-pw\ntest-pw\n",
                              capture_output=True, text=True, check=False)
    assert generate.returncode == 0, generate.stderr
    secret = base64.b64encode(key.read_bytes()).decode()
    key.unlink()
    result = _run(bundle, pub, tmp_path / "work", MINISIGN_SECRET_KEY=secret, MINISIGN_PASSWORD="test-pw")
    assert result.returncode == 0, result.stderr
    assert "OK: smartheat-gateway 0.2.0" in result.stdout
    assert "test-pw" not in result.stdout + result.stderr and secret not in result.stdout + result.stderr
    assert list((tmp_path / "work").iterdir()) == []


def test_wrong_password_fails_and_leaves_no_key_behind(bundle, tmp_path):
    key, pub = tmp_path / "k.key", tmp_path / "k.pub"
    subprocess.run(["minisign", "-G", "-p", str(pub), "-s", str(key)], input="test-pw\ntest-pw\n",
                   capture_output=True, text=True, check=True)
    secret = base64.b64encode(key.read_bytes()).decode()
    result = _run(bundle, pub, tmp_path / "work", MINISIGN_SECRET_KEY=secret, MINISIGN_PASSWORD="falsch")
    assert result.returncode != 0
    assert not (bundle / "manifest.json.minisig").exists()
    assert list((tmp_path / "work").iterdir()) == []


def test_missing_secrets_fail_with_an_error_annotation(bundle, tmp_path):
    result = _run(bundle, tmp_path / "x.pub", tmp_path / "work")
    assert result.returncode == 1
    assert "::error::MINISIGN_SECRET_KEY/MINISIGN_PASSWORD fehlen" in result.stdout



def test_tampered_or_foreign_bundles_are_not_signed(bundle, tmp_path):
    (bundle / "mosquitto.conf").write_text("listener 1883\n")  # passt nicht mehr zum SHA-256 im Manifest
    result = _run(bundle, tmp_path / "t.pub", tmp_path / "work", "--dryrun")
    assert result.returncode != 0 and "SHA-256" in result.stderr
    assert not (bundle / "manifest.json.minisig").exists()


def test_extra_files_and_other_versions_are_not_signed(bundle, tmp_path):
    (bundle / "extra.sh").write_text("echo\n")
    assert _run(bundle, tmp_path / "t.pub", tmp_path / "work", "--dryrun").returncode != 0
    (bundle / "extra.sh").unlink()
    wrong = subprocess.run(["bash", str(SCRIPT), str(bundle), "0.9.9", str(tmp_path / "t.pub"), "--dryrun"],
                           env={**os.environ, "PYTHON": sys.executable, "TMPDIR": str(tmp_path / "work")},
                           capture_output=True, text=True, check=False)
    assert wrong.returncode != 0 and "passt nicht zur Release-Version" in wrong.stderr
