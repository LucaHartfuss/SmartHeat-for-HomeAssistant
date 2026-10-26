import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from oci_helper import make_oci_archive

from build_bundle import build
from smartheat_host import bundles
from verify_bundle import BundleRejected, main, verify

GW = Path(__file__).resolve().parents[2]
COMPOSE = GW / "compose" / "docker-compose.yml"
MOSQUITTO_CONF = GW / "compose" / "mosquitto.conf"
SCRIPT = Path(__file__).resolve().parents[1] / "verify_bundle.py"


@pytest.fixture
def archive(tmp_path):
    path = tmp_path / "image.oci.tar"
    make_oci_archive(path)
    return path


@pytest.fixture
def bundle(tmp_path, archive):
    out = tmp_path / "bundle"
    build(COMPOSE, MOSQUITTO_CONF, archive, "0.2.0", out)
    return out


def _built_from(tmp_path, compose_text: str, archive: Path) -> Path:
    """Ein in sich stimmiges Bundle (Manifest passt zu den Dateien) aus einer anderen Quelle - so saehe die Ausgabe
    eines kompromittierten Bau-Rechners aus."""
    source = tmp_path / "source-compose.yml"
    source.write_text(compose_text)
    out = tmp_path / "evil"
    build(source, MOSQUITTO_CONF, archive, "0.2.0", out)
    return out


def _rewrite(bundle: Path, **changes) -> None:
    """Aendert das Manifest (Schluessel -> neuer Wert) und zieht seine Datei-Pruefsummen und Bytes sauber nach."""
    manifest = json.loads((bundle / "manifest.json").read_text())
    manifest.update(changes)
    (bundle / "manifest.json").write_bytes(json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n")


def test_bundle_built_from_the_tag_passes(bundle):
    verify(bundle, "0.2.0")


def test_privileged_compose_from_a_compromised_build_is_refused(tmp_path, archive):
    text = COMPOSE.read_text().replace("    devices:", "    privileged: true\n    devices:", 1)
    assert "privileged: true" in text
    evil = _built_from(tmp_path, text, archive)
    with pytest.raises(BundleRejected, match="docker-compose.yml weicht"):
        verify(evil, "0.2.0")


def test_foreign_third_party_digest_is_refused(tmp_path, archive):
    text = COMPOSE.read_text()
    digest = text.split("eclipse-mosquitto:2@sha256:", 1)[1][:64]
    evil = _built_from(tmp_path, text.replace(digest, "f" * 64), archive)
    with pytest.raises(BundleRejected, match="weicht"):
        verify(evil, "0.2.0")


def test_verify_lehnt_fremden_archivdigest_ab(bundle):
    """Manifest und Compose-Datei nennen einen Digest, den das Archiv nicht hat (in sich stimmig, Pruefsummen nachgezogen):
    das Image, das signiert wuerde, waere nicht das im Archiv."""
    fremd = "sha256:" + "9" * 64
    manifest = json.loads((bundle / "manifest.json").read_text())
    compose = (bundle / "docker-compose.yml").read_text().replace(manifest["archiv"]["digest"], fremd)
    (bundle / "docker-compose.yml").write_text(compose)
    manifest["files"]["docker-compose.yml"] = hashlib.sha256(compose.encode()).hexdigest()
    manifest["images"]["gateway"] = "smartheat-gateway@" + fremd
    _rewrite(bundle, files=manifest["files"], images=manifest["images"], archiv={**manifest["archiv"], "digest": fremd})
    with pytest.raises(BundleRejected, match="Index-Digest"):
        verify(bundle, "0.2.0")


def test_ein_anderes_archiv_zur_selben_pruefsumme_im_manifest_wird_abgelehnt(bundle, tmp_path):
    anderes = tmp_path / "anderes.tar"
    make_oci_archive(anderes, inhalt=b"andere Schicht")
    (bundle / bundles.ARCHIVE_NAME).write_bytes(anderes.read_bytes())
    with pytest.raises(BundleRejected, match="Archiv: SHA-256"):
        verify(bundle, "0.2.0")


def test_archiv_mit_anderer_groesse_als_im_manifest_wird_abgelehnt(bundle):
    with open(bundle / bundles.ARCHIVE_NAME, "ab") as handle:
        handle.write(b"\0" * 1024)  # ein Tar ignoriert Nullen am Ende: Index und Inhalt bleiben, die Datei aendert sich
    with pytest.raises(BundleRejected, match="Archiv: SHA-256"):
        verify(bundle, "0.2.0")


def test_archiv_ohne_namen_fuer_den_docker_wird_abgelehnt(bundle, tmp_path):
    """Ein Archiv ohne die Annotation (so kommt es aus buildx) hat die richtige Pruefsumme im Manifest, aber `docker load`
    wuerde das Image nicht unter dem Namen der Compose-Datei anlegen: der Neubau aus dem Archiv ergibt andere Bytes."""
    roh = tmp_path / "roh.tar"
    digest = make_oci_archive(roh)
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["archiv"]["digest"] == digest
    (bundle / bundles.ARCHIVE_NAME).write_bytes(roh.read_bytes())
    _rewrite(bundle, archiv={**manifest["archiv"], "sha256": hashlib.sha256(roh.read_bytes()).hexdigest(),
                             "bytes": roh.stat().st_size})
    with pytest.raises(BundleRejected, match=r"gateway-image\.oci\.tar weicht"):
        verify(bundle, "0.2.0")


def test_fremder_images_gateway_eintrag_wird_abgelehnt(bundle):
    manifest = json.loads((bundle / "manifest.json").read_text())
    _rewrite(bundle, images={**manifest["images"], "gateway": "ghcr.io/evil/gw@sha256:" + "c" * 64})
    with pytest.raises(BundleRejected, match="images.gateway"):
        verify(bundle, "0.2.0")


def test_reformatted_manifest_is_refused(bundle):
    manifest = json.loads((bundle / "manifest.json").read_text())
    (bundle / "manifest.json").write_text(json.dumps(manifest))  # gleicher Inhalt, andere Bytes
    with pytest.raises(BundleRejected, match="manifest.json weicht"):
        verify(bundle, "0.2.0")


def test_file_that_does_not_match_its_manifest_hash_is_refused(bundle):
    (bundle / "mosquitto.conf").write_text("listener 1883\n")
    with pytest.raises(BundleRejected, match="SHA-256"):
        verify(bundle, "0.2.0")


def test_extra_or_missing_files_and_other_versions_are_refused(bundle):
    (bundle / "extra.sh").write_text("echo\n")
    with pytest.raises(BundleRejected, match="enthaelt"):
        verify(bundle, "0.2.0")
    (bundle / "extra.sh").unlink()
    with pytest.raises(BundleRejected, match="passt nicht zur Release-Version"):
        verify(bundle, "0.9.9")
    (bundle / "mosquitto.conf").unlink()
    with pytest.raises(BundleRejected, match="enthaelt"):
        verify(bundle, "0.2.0")


def test_ohne_das_archiv_ist_das_bundle_unvollstaendig(bundle):
    (bundle / bundles.ARCHIVE_NAME).unlink()
    with pytest.raises(BundleRejected, match="enthaelt"):
        verify(bundle, "0.2.0")


def test_unreadable_manifest_is_refused(bundle):
    (bundle / "manifest.json").write_text("{kaputt")
    with pytest.raises(BundleRejected, match="Manifest"):
        verify(bundle, "0.2.0")


def test_ein_kaputtes_archiv_wird_abgelehnt_statt_abzustuerzen(bundle):
    """Bytes und Pruefsumme passen zum Manifest, das Archiv ist aber kein OCI-Archiv."""
    kaputt = b"kein tar" * 100
    (bundle / bundles.ARCHIVE_NAME).write_bytes(kaputt)
    manifest = json.loads((bundle / "manifest.json").read_text())
    _rewrite(bundle, archiv={**manifest["archiv"], "sha256": hashlib.sha256(kaputt).hexdigest(), "bytes": len(kaputt)})
    with pytest.raises(BundleRejected, match="Archiv"):
        verify(bundle, "0.2.0")


def test_cli_exit_codes(bundle, capsys):
    assert main([str(bundle), "0.2.0"]) == 0
    assert main([str(bundle), "0.9.9"]) == 1
    assert "passt nicht zur Release-Version" in capsys.readouterr().err


# Ohne PYTHONPATH, aber mit PYTHONUSERBASE, falls gesetzt: dort liegen in der VS-Code-Sandbox (flatpak) die
# Pakete der Entwicklungsumgebung (PyYAML); CI und Host setzen es nicht.
_ENV_OHNE_PYTHONPATH = {"PATH": "", **{k: v for k, v in os.environ.items() if k == "PYTHONUSERBASE"}}


def test_script_runs_without_pythonpath(bundle):
    result = subprocess.run([sys.executable, str(SCRIPT), str(bundle), "0.2.0"],
                            capture_output=True, text=True, env=_ENV_OHNE_PYTHONPATH, check=False)
    assert result.returncode == 0, result.stderr
