import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from oci_helper import make_oci_archive

from build_bundle import build
from kalender import plus_monate
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
    build(COMPOSE, MOSQUITTO_CONF, archive, "0.2.0", out, art="funktion")
    return out


def _built_from(tmp_path, compose_text: str, archive: Path) -> Path:
    """Ein in sich stimmiges Bundle (Manifest passt zu den Dateien) aus einer anderen Quelle - so saehe die Ausgabe
    eines kompromittierten Bau-Rechners aus."""
    source = tmp_path / "source-compose.yml"
    source.write_text(compose_text)
    out = tmp_path / "evil"
    build(source, MOSQUITTO_CONF, archive, "0.2.0", out, art="funktion")
    return out


def _rewrite(bundle: Path, **changes) -> None:
    """Aendert das Manifest (Schluessel -> neuer Wert) und zieht seine Datei-Pruefsummen und Bytes sauber nach."""
    manifest = json.loads((bundle / "manifest.json").read_text())
    manifest.update(changes)
    (bundle / "manifest.json").write_bytes(json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n")


def test_bundle_built_from_the_tag_passes(bundle):
    verify(bundle, "0.2.0")


@pytest.fixture
def system_bundle(tmp_path, archive):
    roh = tmp_path / "gateway.raucb"
    roh.write_bytes(b"signiertes-rauc-bundle" * 100)
    out = tmp_path / "bundle-mit-system"
    build(COMPOSE, MOSQUITTO_CONF, archive, "0.2.0", out, roh, art="funktion")
    return out


def test_bundle_mit_system_aus_dem_tag_besteht(system_bundle):
    verify(system_bundle, "0.2.0")


def test_system_bundle_mit_anderem_inhalt_wird_abgelehnt(system_bundle):
    """Gleiche Groesse, ein Byte anders: das Manifest bindet die SHA-256 der Systemdatei (S-3)."""
    daten = bytearray((system_bundle / "system.raucb").read_bytes())
    daten[0] ^= 1
    (system_bundle / "system.raucb").write_bytes(bytes(daten))
    with pytest.raises(BundleRejected, match="System-Bundle: SHA-256 oder Groesse"):
        verify(system_bundle, "0.2.0")


def test_system_bundle_mit_anderer_groesse_wird_abgelehnt(system_bundle):
    with open(system_bundle / "system.raucb", "ab") as handle:
        handle.write(b"\0")
    with pytest.raises(BundleRejected, match="System-Bundle: SHA-256 oder Groesse"):
        verify(system_bundle, "0.2.0")


def test_system_bundle_ohne_eintrag_im_manifest_ist_eine_fremde_datei(bundle):
    (bundle / "system.raucb").write_bytes(b"x")
    with pytest.raises(BundleRejected, match="enthaelt"):
        verify(bundle, "0.2.0")


def test_manifest_mit_system_aber_ohne_datei_wird_abgelehnt(system_bundle):
    (system_bundle / "system.raucb").unlink()
    with pytest.raises(BundleRejected, match="enthaelt"):
        verify(system_bundle, "0.2.0")


def test_manifest_mit_anderem_compatible_wird_abgelehnt(system_bundle):
    """Ein in sich stimmiges Manifest mit anderem `compatible` weicht vom Neubau aus dem Tag ab."""
    manifest = json.loads((system_bundle / "manifest.json").read_text())
    _rewrite(system_bundle, system={**manifest["system"], "compatible": "smartheat-gateway-anderes"})
    with pytest.raises(BundleRejected, match="manifest.json weicht"):
        verify(system_bundle, "0.2.0")


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


# Ohne PYTHONPATH und ohne PATH: die Pakete (PyYAML) kommen aus dem Interpreter (venv) selbst.
_ENV_OHNE_PYTHONPATH = {"PATH": ""}


def test_script_runs_without_pythonpath(bundle):
    result = subprocess.run([sys.executable, str(SCRIPT), str(bundle), "0.2.0"],
                            capture_output=True, text=True, env=_ENV_OHNE_PYTHONPATH, check=False)
    assert result.returncode == 0, result.stderr


# --- art, ablauf und Changelog-Gate --------------------------------------------------------------------------------

JETZT = datetime(2026, 11, 12, 8, 30, tzinfo=UTC)


def _bundle_zu(tmp_path, archive, art="funktion", ablauf=None):
    out = tmp_path / "bundle-art"
    build(COMPOSE, MOSQUITTO_CONF, archive, "0.2.0", out, art=art, ablauf=ablauf, jetzt=JETZT)
    return out


def _changelog(tmp_path, *zeilen: str) -> Path:
    pfad = tmp_path / "CHANGELOG.md"
    pfad.write_text("# Changelog\n\n## 0.2.0\n\n" + "\n".join(zeilen) + "\n\n## 0.1.0\n\n- alt\n", encoding="utf-8")
    return pfad


def test_der_ablauf_muss_nach_dem_jetzt_liegen(tmp_path, archive):
    bundle = _bundle_zu(tmp_path, archive, ablauf=JETZT)
    for jetzt in (JETZT, JETZT + timedelta(seconds=1), JETZT + timedelta(days=400)):
        with pytest.raises(BundleRejected, match="abgelaufen"):
            verify(bundle, "0.2.0", jetzt=jetzt)
    verify(bundle, "0.2.0", jetzt=JETZT - timedelta(seconds=1))  # Gegenprobe: eine Sekunde davor ist es gueltig


def test_der_ablauf_darf_hoechstens_zwoelf_monate_vorausliegen(tmp_path, archive):
    grenze = plus_monate(JETZT, 12)
    verify(_bundle_zu(tmp_path, archive, ablauf=grenze), "0.2.0", jetzt=JETZT)
    zu_weit = tmp_path / "zu-weit"
    zu_weit.mkdir()
    bundle = _bundle_zu(zu_weit, archive, ablauf=grenze + timedelta(seconds=1))
    with pytest.raises(BundleRejected, match="zwoelf Monate"):
        verify(bundle, "0.2.0", jetzt=JETZT)


def test_ein_nachsignierter_ablauf_im_rahmen_besteht_weiter(tmp_path, archive):
    """Nachsignieren setzt nur den Ablauf neu; der Neubau nimmt ihn aus dem Manifest, die Dateien bleiben gleich."""
    bundle = _bundle_zu(tmp_path, archive)
    _rewrite(bundle, ablauf="2027-01-02T03:04:05Z")
    verify(bundle, "0.2.0", jetzt=JETZT)


def test_art_aus_dem_manifest_geht_in_den_neubau(tmp_path, archive):
    bundle = _bundle_zu(tmp_path, archive, art="treiberfix")
    verify(bundle, "0.2.0", jetzt=JETZT)
    _rewrite(bundle, art="funktion")  # ein anderer gueltiger Wert ist ein anderes, in sich stimmiges Manifest
    verify(bundle, "0.2.0", jetzt=JETZT)


def test_sicherheit_mit_eintrag_unter_funktion_wird_verweigert(tmp_path, archive):
    bundle = _bundle_zu(tmp_path, archive, art="sicherheit")
    log = _changelog(tmp_path, "### Sicherheit", "", "- Luecke geschlossen", "", "### Funktion", "", "- neue Ansicht")
    with pytest.raises(BundleRejected, match="Funktion"):
        verify(bundle, "0.2.0", jetzt=JETZT, changelog=log)


def test_sicherheit_ohne_eintrag_unter_funktion_besteht(tmp_path, archive):
    bundle = _bundle_zu(tmp_path, archive, art="sicherheit")
    verify(bundle, "0.2.0", jetzt=JETZT, changelog=_changelog(tmp_path, "### Sicherheit", "", "- Luecke geschlossen"))
    leer = tmp_path / "leer"
    leer.mkdir()
    verify(bundle, "0.2.0", jetzt=JETZT,
           changelog=_changelog(leer, "### Sicherheit", "", "- Luecke", "", "### Funktion", ""))  # Ueberschrift ohne Eintrag
    flach = tmp_path / "flach"
    flach.mkdir()
    verify(bundle, "0.2.0", jetzt=JETZT, changelog=_changelog(flach, "- Luecke geschlossen"))


@pytest.mark.parametrize("art", ["funktion", "treiberfix"])
def test_funktion_und_treiberfix_brauchen_das_gate_nicht(tmp_path, archive, art):
    bundle = _bundle_zu(tmp_path, archive, art=art)
    verify(bundle, "0.2.0", jetzt=JETZT, changelog=_changelog(tmp_path, "### Funktion", "", "- neue Ansicht"))
    verify(bundle, "0.2.0", jetzt=JETZT, changelog=tmp_path / "gibt-es-nicht.md")  # wird gar nicht gelesen


def test_sicherheit_ohne_abschnitt_oder_datei_im_changelog_wird_verweigert(tmp_path, archive):
    bundle = _bundle_zu(tmp_path, archive, art="sicherheit")
    ohne = tmp_path / "ohne.md"
    ohne.write_text("# Changelog\n\n## 0.1.0\n\n- alt\n", encoding="utf-8")
    for log in (ohne, tmp_path / "gibt-es-nicht.md"):
        with pytest.raises(BundleRejected, match="Changelog"):
            verify(bundle, "0.2.0", jetzt=JETZT, changelog=log)


def test_cli_prueft_das_gate_mit_changelog_und_ablauf(tmp_path, archive, capsys):
    bundle = _bundle_zu(tmp_path, archive, art="sicherheit")
    log = _changelog(tmp_path, "### Funktion", "", "- neue Ansicht")
    assert main([str(bundle), "0.2.0", "--changelog", str(log)]) == 1
    assert "Funktion" in capsys.readouterr().err
    gut = tmp_path / "gut"
    gut.mkdir()
    assert main([str(bundle), "0.2.0", "--changelog", str(_changelog(gut, "- Luecke geschlossen"))]) == 0
