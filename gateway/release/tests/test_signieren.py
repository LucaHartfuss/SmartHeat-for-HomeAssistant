import base64
import json
import os
import shutil
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

import ca
import signieren
from smartheat_host import kette

SIGNIEREN_PY = Path(__file__).resolve().parents[1] / "signieren.py"
ECHTES_OPENSSL = shutil.which("openssl")
MANIFEST = b'{"version": "1.2.3"}\n'
URI = "pkcs11:token=Setwell%20Release;object=release-key;type=private?pin-value=GEHEIME-PIN-4711"


@pytest.fixture
def kette_dir(tmp_path):
    ziel = tmp_path / "kette"
    ca.test_kette_anlegen(ziel)
    return ziel


def _jetzt() -> datetime:
    return datetime.now(UTC)


@pytest.fixture
def openssl_attrappe(tmp_path, monkeypatch):
    """Attrappe `openssl` im PATH: protokolliert Argumente und stdin, gibt eine ECDSA-Signatur des Schluessels aus
    $ATTRAPPE_SCHLUESSEL zurueck (per echtem openssl) oder scheitert, wenn ATTRAPPE_FEHLER gesetzt ist."""
    bin_dir, logs = tmp_path / "bin", tmp_path / "logs"
    bin_dir.mkdir()
    logs.mkdir()
    attrappe = bin_dir / "openssl"
    attrappe.write_text(f'''#!{sys.executable}
import json, os, subprocess, sys
daten = sys.stdin.buffer.read()
with open(os.environ["ATTRAPPE_LOG"], "a") as protokoll:
    protokoll.write(json.dumps({{"argv": sys.argv[1:], "stdin": daten.decode()}}) + "\\n")
if os.environ.get("ATTRAPPE_LEER"):
    sys.exit(0)
if os.environ.get("ATTRAPPE_ECHO"):
    sys.stderr.write("openssl: kann " + sys.argv[-1] + " nicht lesen\\n")
if os.environ.get("ATTRAPPE_FEHLER"):
    sys.stderr.write("openssl: " + os.environ["ATTRAPPE_FEHLER"] + "\\n")
    sys.exit(1)
ergebnis = subprocess.run(["{ECHTES_OPENSSL}", "dgst", "-sha256", "-sign", os.environ["ATTRAPPE_SCHLUESSEL"]],
                          input=daten, capture_output=True)
sys.stdout.buffer.write(ergebnis.stdout)
sys.exit(ergebnis.returncode)
''')
    attrappe.chmod(attrappe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("ATTRAPPE_LOG", str(logs / "openssl.log"))
    monkeypatch.delenv("ATTRAPPE_FEHLER", raising=False)
    monkeypatch.delenv("ATTRAPPE_LEER", raising=False)
    monkeypatch.delenv("ATTRAPPE_ECHO", raising=False)

    def aufrufe() -> list[dict]:
        pfad = logs / "openssl.log"
        return [json.loads(zeile) for zeile in pfad.read_text().splitlines()] if pfad.exists() else []

    return aufrufe


def _text(kette_dir, daten=MANIFEST, schluessel=None) -> str:
    return signieren.signiere(daten, (kette_dir / "release.crt").read_text(), schluessel or str(kette_dir / "release.key"))


def test_signiere_mit_pem_datei_prueft_gegen_die_test_kette(kette_dir):
    roots = kette.load_roots((kette_dir / "root.crt").read_text())
    text = _text(kette_dir)
    dokument = json.loads(text)
    assert set(dokument) == {"format", "zertifikat", "signatur"} and dokument["format"] == "smartheat-kette-1"
    assert dokument["zertifikat"] == (kette_dir / "release.crt").read_text()
    kette.verify(roots, MANIFEST, text, _jetzt())  # genau die Pruefung des Geraets
    with pytest.raises(kette.SignatureError):
        kette.verify(roots, MANIFEST + b" ", text, _jetzt())  # andere Daten: Signatur passt nicht
    fremd = kette_dir.parent / "fremd"
    ca.test_kette_anlegen(fremd)
    with pytest.raises(kette.SignatureError):
        kette.verify(kette.load_roots((fremd / "root.crt").read_text()), MANIFEST, text, _jetzt())  # andere Root


def test_signatur_ist_ecdsa_sha256_in_der(kette_dir):
    text = json.loads(_text(kette_dir))
    key = serialization.load_pem_private_key((kette_dir / "release.key").read_bytes(), None)
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    key.public_key().verify(base64.b64decode(text["signatur"], validate=True), MANIFEST, ec.ECDSA(hashes.SHA256()))


def test_pkcs11_uri_ruft_openssl_mit_provider(kette_dir, openssl_attrappe, monkeypatch):
    monkeypatch.setenv("ATTRAPPE_SCHLUESSEL", str(kette_dir / "release.key"))
    text = _text(kette_dir, schluessel=URI)
    aufruf = openssl_attrappe()
    assert [a["argv"] for a in aufruf] == [["dgst", "-sha256", "-provider", "pkcs11", "-provider", "default", "-sign", URI]]
    assert aufruf[0]["stdin"] == MANIFEST.decode()  # das Manifest geht ueber stdin, nie als Argument
    kette.verify(kette.load_roots((kette_dir / "root.crt").read_text()), MANIFEST, text, _jetzt())


def test_pem_datei_ruft_openssl_ohne_provider(kette_dir, openssl_attrappe, monkeypatch):
    monkeypatch.setenv("ATTRAPPE_SCHLUESSEL", str(kette_dir / "release.key"))
    _text(kette_dir)
    assert [a["argv"] for a in openssl_attrappe()] == [["dgst", "-sha256", "-sign", str(kette_dir / "release.key")]]


def test_pem_datei_die_fehlt_scheitert_ohne_openssl(kette_dir, openssl_attrappe):
    with pytest.raises(signieren.SigniereFehler):
        _text(kette_dir, schluessel=str(kette_dir / "gibt-es-nicht.key"))
    assert openssl_attrappe() == []


@pytest.mark.parametrize("fehler", [True, False], ids=["openssl-scheitert", "leere-signatur"])
def test_openssl_fehler_wird_zu_signierefehler_ohne_den_schluessel(kette_dir, openssl_attrappe, monkeypatch, fehler):
    monkeypatch.setenv("ATTRAPPE_FEHLER" if fehler else "ATTRAPPE_LEER", "Token fehlt")
    with pytest.raises(signieren.SigniereFehler) as info:
        _text(kette_dir, schluessel=URI)
    assert "GEHEIME-PIN" not in str(info.value) and "pkcs11:" not in str(info.value)


def test_signierschluessel_erscheint_nie_in_der_ausgabe(kette_dir, openssl_attrappe, tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "manifest.json").write_bytes(MANIFEST)
    umgebung = {**os.environ, "ATTRAPPE_SCHLUESSEL": str(kette_dir / "release.key"), "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "host")}
    args = [sys.executable, str(SIGNIEREN_PY), str(bundle), "--zertifikat", str(kette_dir / "release.crt"), "--schluessel", URI,
            "--roots", str(kette_dir / "root.crt")]
    gut = subprocess.run(args, env=umgebung, capture_output=True, text=True)
    assert gut.returncode == 0, gut.stderr
    schlecht = subprocess.run(args, env={**umgebung, "ATTRAPPE_FEHLER": "Token fehlt"}, capture_output=True, text=True)
    assert schlecht.returncode == 1
    # openssl nennt den Schluessel in seiner Fehlermeldung (so tun echte Versionen bei unlesbaren Schluesseln)
    echo = subprocess.run(args, env={**umgebung, "ATTRAPPE_FEHLER": "Token fehlt", "ATTRAPPE_ECHO": "1"},
                          capture_output=True, text=True)
    assert echo.returncode == 1 and "[Schluessel]" in echo.stderr
    for ergebnis in (gut, schlecht, echo):
        for geheim in ("GEHEIME-PIN", "pkcs11:", "Setwell%20Release", "release-key"):
            assert geheim not in ergebnis.stdout + ergebnis.stderr, geheim
    assert "Token fehlt" in schlecht.stderr  # Gegenprobe: die Meldung von openssl kommt durch


# --- Kommandozeile ------------------------------------------------------------------------------------------------

def _cli(kette_dir, bundle, *extra, roots=None, schluessel=None, zertifikat=None):
    umgebung = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "host")}
    return subprocess.run([sys.executable, str(SIGNIEREN_PY), str(bundle), "--zertifikat",
                           str(zertifikat or kette_dir / "release.crt"), "--schluessel",
                           str(schluessel or kette_dir / "release.key"), "--roots", str(roots or kette_dir / "root.crt"),
                           *extra], env=umgebung, capture_output=True, text=True)


@pytest.fixture
def bundle(tmp_path):
    ziel = tmp_path / "bundle"
    ziel.mkdir()
    (ziel / "manifest.json").write_bytes(MANIFEST)
    return ziel


def test_cli_schreibt_die_signatur_und_prueft_sie_mit_dem_geraetecode(kette_dir, bundle):
    ergebnis = _cli(kette_dir, bundle)
    assert ergebnis.returncode == 0, ergebnis.stderr
    signatur = bundle / "manifest.json.sig"
    assert signatur.stat().st_mode & 0o777 == 0o644
    pruefung = subprocess.run([sys.executable, "-m", "smartheat_host.kette", str(kette_dir / "root.crt"),
                               str(bundle / "manifest.json"), str(signatur)], capture_output=True, text=True,
                              env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "host")})
    assert pruefung.returncode == 0, pruefung.stderr
    assert "OK" in ergebnis.stdout


def test_cli_ersetzt_eine_alte_signatur_und_laesst_bei_fehlern_keine_zurueck(kette_dir, bundle, tmp_path):
    (bundle / "manifest.json.sig").write_text("alt")
    assert _cli(kette_dir, bundle).returncode == 0
    assert (bundle / "manifest.json.sig").read_text() != "alt"
    ca.test_kette_anlegen(tmp_path / "andere")
    # Roots, denen das Zertifikat nicht gehoert: die Pruefung scheitert, nichts bleibt liegen
    falsch = _cli(kette_dir, bundle, roots=tmp_path / "andere" / "root.crt")
    assert falsch.returncode == 1 and "UNGUELTIG" in falsch.stderr
    assert not (bundle / "manifest.json.sig").exists()
    # Zertifikat und Schluessel gehoeren nicht zusammen
    ungleich = _cli(kette_dir, bundle, zertifikat=tmp_path / "andere" / "release.crt")
    assert ungleich.returncode == 1 and not (bundle / "manifest.json.sig").exists()


def test_cli_ohne_manifest_oder_mit_platzhalter_roots_scheitert(kette_dir, tmp_path, bundle):
    leer = tmp_path / "leer"
    leer.mkdir()
    assert _cli(kette_dir, leer).returncode == 1
    platzhalter = tmp_path / "release.pub"
    platzhalter.write_text("SMARTHEAT-PLATZHALTER\n")
    ergebnis = _cli(kette_dir, bundle, roots=platzhalter)
    assert ergebnis.returncode == 1 and not (bundle / "manifest.json.sig").exists()


def test_cli_aus_schreibt_an_einen_anderen_ort(kette_dir, bundle, tmp_path):
    ziel = tmp_path / "anders.sig"
    assert _cli(kette_dir, bundle, "--aus", str(ziel)).returncode == 0
    assert ziel.is_file() and not (bundle / "manifest.json.sig").exists()


def test_cli_falscher_aufruf_exit_2():
    assert subprocess.run([sys.executable, str(SIGNIEREN_PY)], capture_output=True).returncode == 2
