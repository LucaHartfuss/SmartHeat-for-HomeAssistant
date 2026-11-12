import base64
import json
import os
import shutil
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from oci_helper import make_oci_archive

import ca
import signieren
from build_bundle import build
from kalender import plus_monate
from smartheat_host import bundles, kette

GW = Path(__file__).resolve().parents[2]

SIGNIEREN_PY = Path(__file__).resolve().parents[1] / "signieren.py"
ECHTES_OPENSSL = shutil.which("openssl")
MANIFEST = b'{"version": "1.2.3"}\n'
URI = "pkcs11:token=Setwell%20Release;object=release-key;type=private?pin-value=GEHEIME-PIN-4711"


@pytest.fixture
def kette_dir(tmp_path):
    ziel = tmp_path / "kette"
    ca.test_kette_anlegen(ziel)
    return ziel


def _echtes_manifest(wo: Path, art: str = "funktion") -> bytes:
    """Die Bytes eines Manifests, wie build_bundle.py es schreibt (das Geraet liest es mit bundles.parse_manifest)."""
    archiv = wo / "image.oci.tar"
    make_oci_archive(archiv)
    build(GW / "compose" / "docker-compose.yml", GW / "compose" / "mosquitto.conf", archiv, "0.2.0", wo / "gebaut", art=art,
          jetzt=datetime(2026, 1, 15, 9, 0, tzinfo=UTC))
    return (wo / "gebaut" / "manifest.json").read_bytes()


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
    (bundle / "manifest.json").write_bytes(_echtes_manifest(tmp_path))
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
    (ziel / "manifest.json").write_bytes(_echtes_manifest(tmp_path))
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
    platzhalter = tmp_path / "release-roots.pem"
    platzhalter.write_text("SMARTHEAT-PLATZHALTER\n")
    ergebnis = _cli(kette_dir, bundle, roots=platzhalter)
    assert ergebnis.returncode == 1 and not (bundle / "manifest.json.sig").exists()


def test_cli_aus_schreibt_an_einen_anderen_ort(kette_dir, bundle, tmp_path):
    ziel = tmp_path / "anders.sig"
    assert _cli(kette_dir, bundle, "--aus", str(ziel)).returncode == 0
    assert ziel.is_file() and not (bundle / "manifest.json.sig").exists()


def test_cli_falscher_aufruf_exit_2():
    assert subprocess.run([sys.executable, str(SIGNIEREN_PY)], capture_output=True).returncode == 2


# --- Ablauf neu setzen ---------------------------------------------------------------------------------------------

JETZT = datetime(2026, 11, 12, 8, 30, tzinfo=UTC)


def _blatt_pem(monate: int, jetzt: datetime) -> str:
    """Ein Release-Zertifikat (PEM), das `monate` Monate ab `jetzt` gilt, ausgestellt von einer eigenen Test-Root."""
    root_key, blatt_key = ca._schluessel(), ca._schluessel()
    root = ca._root_zertifikat(root_key, "Test-Root", jetzt)
    return ca._blatt(root, root_key, blatt_key.public_key(), monate, jetzt).decode()


def _ablauf_von(manifest: bytes) -> datetime:
    return bundles.parse_manifest(manifest).ablauf


def test_der_ablauf_ist_zwoelf_monate_ab_jetzt(tmp_path):
    neu = signieren.ablauf_setzen(_echtes_manifest(tmp_path), _blatt_pem(24, JETZT), JETZT)
    assert _ablauf_von(neu) == datetime(2027, 11, 12, 8, 30, tzinfo=UTC)


def test_der_ablauf_wird_auf_sekunden_gerundet(tmp_path):
    neu = signieren.ablauf_setzen(_echtes_manifest(tmp_path), _blatt_pem(24, JETZT), JETZT.replace(microsecond=999_999))
    assert _ablauf_von(neu) == datetime(2027, 11, 12, 8, 30, tzinfo=UTC)


@pytest.mark.parametrize(("monate", "erwartet"), [
    (11, datetime(2027, 10, 12, 8, 30, tzinfo=UTC)),   # Zertifikat endet vor den zwoelf Monaten: der Ablauf folgt ihm
    (12, datetime(2027, 11, 12, 8, 30, tzinfo=UTC)),   # genau gleich
    (13, datetime(2027, 11, 12, 8, 30, tzinfo=UTC)),   # Zertifikat laenger: zwoelf Monate
])
def test_der_ablauf_liegt_nie_nach_dem_zertifikat(tmp_path, monate, erwartet):
    pem = _blatt_pem(monate, JETZT)
    neu = signieren.ablauf_setzen(_echtes_manifest(tmp_path), pem, JETZT)
    assert _ablauf_von(neu) == erwartet
    bis = x509.load_pem_x509_certificate(pem.encode()).not_valid_after_utc
    assert _ablauf_von(neu) <= bis


def test_ein_abgelaufenes_oder_gerade_ablaufendes_zertifikat_wird_nicht_signiert(tmp_path):
    vorher = JETZT - timedelta(days=61)
    pem = _blatt_pem(1, vorher)  # gueltig bis vor rund einem Monat
    with pytest.raises(signieren.SigniereFehler, match="Zertifikat"):
        signieren.ablauf_setzen(_echtes_manifest(tmp_path), pem, JETZT)
    bis = x509.load_pem_x509_certificate(pem.encode()).not_valid_after_utc
    with pytest.raises(signieren.SigniereFehler, match="Zertifikat"):
        signieren.ablauf_setzen(_echtes_manifest(tmp_path), pem, bis)  # in der Sekunde des Endes
    signieren.ablauf_setzen(_echtes_manifest(tmp_path), pem, bis - timedelta(seconds=1))  # eine Sekunde davor geht es


def test_art_und_alles_andere_im_manifest_bleiben_beim_neu_setzen(tmp_path):
    alt = _echtes_manifest(tmp_path, art="sicherheit")
    neu = signieren.ablauf_setzen(alt, _blatt_pem(24, JETZT), JETZT)
    vorher, nachher = json.loads(alt), json.loads(neu)
    assert vorher.pop("ablauf") != nachher.pop("ablauf")
    assert vorher == nachher and nachher["art"] == "sicherheit"


def test_das_neu_gesetzte_manifest_hat_das_format_des_bauwerkzeugs(tmp_path):
    """Gleiche Serialisierung wie build_bundle.py: ein Manifest mit unveraendertem Ablauf ist byte-gleich."""
    alt = _echtes_manifest(tmp_path)
    ablauf = _ablauf_von(alt)
    start = ablauf - timedelta(days=365)
    gleich = signieren.ablauf_setzen(alt, _blatt_pem(24, start), start)
    assert _ablauf_von(gleich) == ablauf  # zwoelf Monate vor dem alten Ablauf gesetzt ergibt wieder den alten
    assert gleich == alt


def test_ein_unlesbares_manifest_wird_nicht_signiert(tmp_path):
    for roh in (b"kein json", MANIFEST, b"[]"):
        with pytest.raises(signieren.SigniereFehler, match="Manifest"):
            signieren.ablauf_setzen(roh, _blatt_pem(24, JETZT), JETZT)


def test_cli_setzt_den_ablauf_und_signiert_das_neue_manifest(kette_dir, bundle):
    vorher = datetime.now(UTC)
    alt = (bundle / "manifest.json").read_bytes()
    ergebnis = _cli(kette_dir, bundle)
    nachher = datetime.now(UTC)
    assert ergebnis.returncode == 0, ergebnis.stderr
    neu = (bundle / "manifest.json").read_bytes()
    assert neu != alt and plus_monate(vorher.replace(microsecond=0), 12) <= _ablauf_von(neu) <= plus_monate(nachher, 12)
    # die Signatur gilt fuer genau die neuen Bytes
    kette.verify(kette.load_roots((kette_dir / "root.crt").read_text()), neu, (bundle / "manifest.json.sig").read_text(), _jetzt())


def test_cli_laesst_bei_einem_fehler_das_manifest_unveraendert(kette_dir, bundle, tmp_path):
    alt = (bundle / "manifest.json").read_bytes()
    ca.test_kette_anlegen(tmp_path / "andere")
    falsch = _cli(kette_dir, bundle, roots=tmp_path / "andere" / "root.crt")
    assert falsch.returncode == 1 and (bundle / "manifest.json").read_bytes() == alt


def test_cli_mit_aus_ist_eine_zusaetzliche_signatur_ueber_das_unveraenderte_manifest(kette_dir, bundle, tmp_path):
    """--aus erzeugt eine weitere Signatur derselben Bytes (etwa mit einer fremden Kette): das Manifest im Bundle und damit
    die Signatur daneben bleiben gueltig, der Ablauf wird nicht neu gesetzt."""
    manifest = (bundle / "manifest.json").read_bytes()
    ziel = tmp_path / "anders.sig"
    assert _cli(kette_dir, bundle, "--aus", str(ziel)).returncode == 0
    assert (bundle / "manifest.json").read_bytes() == manifest
    kette.verify(kette.load_roots((kette_dir / "root.crt").read_text()), manifest, ziel.read_text(), _jetzt())
