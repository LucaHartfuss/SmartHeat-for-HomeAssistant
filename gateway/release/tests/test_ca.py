import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, ExtensionOID, NameOID

import ca
from smartheat_host import kette

CA_PY = Path(__file__).resolve().parents[1] / "ca.py"
JETZT = datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC)
PASSPHRASE = "passphrase-fuer-den-test"
# Zusammengesetzt: der Geheimnis-Scan (lint) sucht den PEM-Kopf privater Schluessel in jeder Datei.
VERSCHLUESSELT = "-----BEGIN " + "ENCRYPTED PRIVATE KEY-----"


@pytest.fixture
def root(tmp_path):
    """Eine Root in tmp_path/root (JETZT, 15 Jahre) samt Passphrase; (Zertifikatsdatei, Schluesseldatei)."""
    ziel = tmp_path / "root"
    ca.root_anlegen(ziel, PASSPHRASE, JETZT)
    return ziel / "root.crt", ziel / "root.key"


def _csr(key=None, *, erweiterungen=(), cn="Token") -> bytes:
    key = key or ec.generate_private_key(ec.SECP256R1())
    builder = x509.CertificateSigningRequestBuilder().subject_name(
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
    for extension, critical in erweiterungen:
        builder = builder.add_extension(extension, critical=critical)
    return builder.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM)


def _cert(pem: bytes | str) -> x509.Certificate:
    return x509.load_pem_x509_certificate(pem.encode() if isinstance(pem, str) else pem)


def _ausstellen(root, csr: bytes, monate=24, jetzt=JETZT, passphrase=PASSPHRASE) -> x509.Certificate:
    cert_pem = ca.ausstellen(root[0].read_bytes(), root[1].read_bytes(), passphrase, csr, monate, jetzt)
    return _cert(cert_pem)


def _lauf(*args, stdin="", **env):
    return subprocess.run([sys.executable, str(CA_PY), *args], input=stdin, capture_output=True, text=True,
                          env={**os.environ, **env}, timeout=120)


# --- root-anlegen -------------------------------------------------------------------------------------------------

def test_root_ist_ca_und_15_jahre_gueltig(root):
    cert = _cert(root[0].read_bytes())
    assert cert.subject.rfc4514_string() == "CN=Setwell Gateway Release Root"
    assert cert.issuer == cert.subject
    assert cert.not_valid_before_utc == JETZT and cert.not_valid_after_utc == datetime(2041, 10, 26, 12, tzinfo=UTC)
    basic = cert.extensions.get_extension_for_class(x509.BasicConstraints)
    assert basic.critical and basic.value.ca is True
    usage = cert.extensions.get_extension_for_class(x509.KeyUsage)
    assert usage.critical and usage.value.key_cert_sign and not usage.value.digital_signature
    assert isinstance(cert.public_key(), ec.EllipticCurvePublicKey) and cert.public_key().curve.name == "secp256r1"
    assert len(kette.load_roots(root[0].read_text())) == 1  # der Pruefer des Geraets nimmt sie als Root an


def test_root_schluessel_ist_verschluesselt(root):
    text = root[1].read_text()
    assert text.startswith(VERSCHLUESSELT)
    with pytest.raises(TypeError):
        serialization.load_pem_private_key(root[1].read_bytes(), None)
    with pytest.raises(ValueError):
        serialization.load_pem_private_key(root[1].read_bytes(), b"falsch")
    key = serialization.load_pem_private_key(root[1].read_bytes(), PASSPHRASE.encode())
    assert key.public_key().public_numbers() == _cert(root[0].read_bytes()).public_key().public_numbers()
    asn1 = subprocess.run(["openssl", "asn1parse", "-in", str(root[1])], capture_output=True, text=True, check=True).stdout
    assert "aes-256-cbc" in asn1
    iterationen = int(re.search(r"PBKDF2.*?INTEGER\s+:([0-9A-F]+)", asn1, re.S).group(1), 16)
    assert iterationen >= 600_000


def test_root_dateien_sind_nur_fuer_den_besitzer_und_werden_nie_ueberschrieben(tmp_path):
    ziel = tmp_path / "root"
    ca.root_anlegen(ziel, PASSPHRASE, JETZT)
    assert ziel.stat().st_mode & 0o077 == 0 and (ziel / "root.key").stat().st_mode & 0o077 == 0
    vorher = (ziel / "root.key").read_bytes()
    with pytest.raises(ca.CaFehler):
        ca.root_anlegen(ziel, PASSPHRASE, JETZT)
    assert (ziel / "root.key").read_bytes() == vorher


def test_root_anlegen_cli_fragt_die_passphrase_zweimal_und_schreibt_sie_nie_aus(tmp_path):
    ziel = tmp_path / "root"
    ergebnis = _lauf("root-anlegen", "--aus", str(ziel), stdin=f"{PASSPHRASE}\n{PASSPHRASE}\n")
    assert ergebnis.returncode == 0, ergebnis.stderr
    assert PASSPHRASE not in ergebnis.stdout + ergebnis.stderr
    assert {p.name for p in ziel.iterdir()} == {"root.crt", "root.key"}
    verschieden = tmp_path / "zwei"
    schlecht = _lauf("root-anlegen", "--aus", str(verschieden), stdin=f"{PASSPHRASE}\nanders-anders-anders\n")
    assert schlecht.returncode == 1 and not verschieden.exists()
    kurz = _lauf("root-anlegen", "--aus", str(tmp_path / "kurz"), stdin="kurz\nkurz\n")
    assert kurz.returncode == 1 and not (tmp_path / "kurz").exists()


# --- ausstellen ---------------------------------------------------------------------------------------------------

def test_ausgestelltes_zertifikat_ist_24_monate_code_signing(root):
    jetzt = datetime(2027, 1, 31, 8, 30, 15, tzinfo=UTC)
    cert = _ausstellen(root, _csr(), 24, jetzt)
    assert cert.not_valid_before_utc == jetzt and cert.not_valid_after_utc == datetime(2029, 1, 31, 8, 30, 15, tzinfo=UTC)
    assert cert.issuer == _cert(root[0].read_bytes()).subject
    basic = cert.extensions.get_extension_for_class(x509.BasicConstraints)
    assert basic.critical and basic.value.ca is False
    usage = cert.extensions.get_extension_for_class(x509.KeyUsage)
    assert usage.critical and usage.value.digital_signature and not usage.value.key_cert_sign
    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    assert list(eku.value) == [ExtendedKeyUsageOID.CODE_SIGNING]
    assert cert.signature_hash_algorithm.name == "sha256"
    # der Pruefer des Geraets nimmt das Blatt an (mit einer Signatur des CSR-Schluessels, siehe test_signieren)
    cert.verify_directly_issued_by(_cert(root[0].read_bytes()))


@pytest.mark.parametrize(("monate", "erwartet"), [
    (1, datetime(2027, 2, 28, tzinfo=UTC)),    # 31. Januar + 1 Monat: Monatsende, nie ueberlaufen
    (12, datetime(2028, 1, 31, tzinfo=UTC)),
])
def test_monate_werden_kalendarisch_gerechnet(root, monate, erwartet):
    cert = _ausstellen(root, _csr(), monate, datetime(2027, 1, 31, tzinfo=UTC))
    assert cert.not_valid_after_utc == erwartet


@pytest.mark.parametrize("monate", [0, -1, 25, 120])
def test_monate_ausserhalb_von_1_bis_24_werden_abgelehnt(root, monate):
    with pytest.raises(ca.CaFehler):
        _ausstellen(root, _csr(), monate)


def test_csr_mit_falscher_signatur_wird_abgelehnt(root):
    der = bytearray(x509.load_pem_x509_csr(_csr()).public_bytes(serialization.Encoding.DER))
    der[-1] ^= 0x01  # letztes Byte der Signatur
    kaputt = x509.load_der_x509_csr(bytes(der)).public_bytes(serialization.Encoding.PEM)
    assert not x509.load_pem_x509_csr(kaputt).is_signature_valid  # Gegenprobe: die Fassung ist wirklich ungueltig
    with pytest.raises(ca.CaFehler, match="Signatur"):
        _ausstellen(root, kaputt)


@pytest.mark.parametrize("key", [ec.generate_private_key(ec.SECP384R1()), rsa.generate_private_key(65537, 2048)],
                         ids=["p384", "rsa"])
def test_csr_mit_anderem_schluessel_als_p256_wird_abgelehnt(root, key):
    with pytest.raises(ca.CaFehler, match="P-256"):
        _ausstellen(root, _csr(key))


def test_csr_ohne_pem_wird_abgelehnt(root):
    with pytest.raises(ca.CaFehler):
        _ausstellen(root, b"kein csr")


def test_erweiterungen_der_csr_werden_nicht_uebernommen(root):
    csr = _csr(erweiterungen=[
        (x509.BasicConstraints(ca=True, path_length=None), True),
        (x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                       data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
                       encipher_only=False, decipher_only=False), True),
        (x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False),
        (x509.SubjectAlternativeName([x509.DNSName("boese.example")]), False)], cn="Fremder Name")
    assert len(x509.load_pem_x509_csr(csr).extensions) == 4  # Gegenprobe: die CSR bittet wirklich darum
    cert = _ausstellen(root, csr)
    assert {e.oid for e in cert.extensions} == {ExtensionOID.BASIC_CONSTRAINTS, ExtensionOID.KEY_USAGE,
                                                ExtensionOID.EXTENDED_KEY_USAGE}
    assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
    assert list(cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value) == [
        ExtendedKeyUsageOID.CODE_SIGNING]
    assert cert.subject.rfc4514_string() == "CN=Setwell Gateway Release"  # der Name kommt nicht aus der CSR


def test_not_after_nie_nach_der_root(root):
    jetzt = datetime(2041, 4, 26, 12, tzinfo=UTC)  # 6 Monate vor dem Ende der Root
    cert = _ausstellen(root, _csr(), 24, jetzt)
    assert cert.not_valid_after_utc == datetime(2041, 10, 26, 12, tzinfo=UTC)  # das Ende der Root, nicht 2043
    ganz_knapp = _ausstellen(root, _csr(), 1, datetime(2041, 10, 26, 11, tzinfo=UTC))
    assert ganz_knapp.not_valid_after_utc == datetime(2041, 10, 26, 12, tzinfo=UTC)


@pytest.mark.parametrize("jetzt", [datetime(2026, 10, 26, 11, 59, 59, tzinfo=UTC), datetime(2041, 10, 26, 12, 0, 1, tzinfo=UTC)],
                         ids=["vor-der-root", "nach-der-root"])
def test_keine_ausstellung_ausserhalb_der_gueltigkeit_der_root(root, jetzt):
    with pytest.raises(ca.CaFehler, match="Root"):
        _ausstellen(root, _csr(), 24, jetzt)


def test_falsche_passphrase_der_root_stellt_nichts_aus(root, tmp_path):
    with pytest.raises(ca.CaFehler, match="Passphrase"):
        _ausstellen(root, _csr(), passphrase="falsch-falsch-falsch")


def test_ausstellen_cli_schreibt_datei_ohne_zu_ueberschreiben(root, tmp_path):
    csr = tmp_path / "token.csr"
    csr.write_bytes(_csr())
    ziel = tmp_path / "release.crt"
    args = ["ausstellen", "--root-cert", str(root[0]), "--root-key", str(root[1]), "--csr", str(csr), "--monate", "24",
            "--aus", str(ziel)]
    ergebnis = _lauf(*args, stdin=PASSPHRASE + "\n")
    assert ergebnis.returncode == 0, ergebnis.stderr
    assert PASSPHRASE not in ergebnis.stdout + ergebnis.stderr
    cert = _cert(ziel.read_bytes())
    assert cert.not_valid_after_utc.year in (2028, 2029) and cert.subject.rfc4514_string() == "CN=Setwell Gateway Release"
    vorher = ziel.read_bytes()
    zweiter = _lauf(*args, stdin=PASSPHRASE + "\n")
    assert zweiter.returncode == 1 and ziel.read_bytes() == vorher
    falsch = _lauf(*[a if a != str(ziel) else str(tmp_path / "x.crt") for a in args], stdin="falsch-falsch-falsch\n")
    assert falsch.returncode == 1 and not (tmp_path / "x.crt").exists()


# --- test-kette ---------------------------------------------------------------------------------------------------

def test_test_kette_schreibt_root_blatt_und_unverschluesselten_schluessel(tmp_path):
    ziel = tmp_path / "kette"
    ca.test_kette_anlegen(ziel, JETZT)
    assert {p.name for p in ziel.iterdir()} == {"root.crt", "release.crt", "release.key"}
    assert ziel.stat().st_mode & 0o077 == 0 and (ziel / "release.key").stat().st_mode & 0o077 == 0
    roots = kette.load_roots((ziel / "root.crt").read_text())
    blatt = _cert((ziel / "release.crt").read_bytes())
    blatt.verify_directly_issued_by(roots[0])
    assert roots[0].subject.rfc4514_string() == "CN=Setwell Gateway Release Root (Test)"
    key = serialization.load_pem_private_key((ziel / "release.key").read_bytes(), None)
    assert key.public_key().public_numbers() == blatt.public_key().public_numbers()
    assert blatt.not_valid_after_utc == datetime(2028, 10, 26, 12, tzinfo=UTC)
    with pytest.raises(ca.CaFehler):
        ca.test_kette_anlegen(ziel, JETZT)


def test_zwei_test_ketten_vertrauen_einander_nicht(tmp_path):
    ca.test_kette_anlegen(tmp_path / "a", JETZT)
    ca.test_kette_anlegen(tmp_path / "b", JETZT)
    fremd = _cert((tmp_path / "b" / "release.crt").read_bytes())
    with pytest.raises(Exception):  # noqa: B017 - cryptography wirft je nach Art ValueError oder InvalidSignature
        fremd.verify_directly_issued_by(_cert((tmp_path / "a" / "root.crt").read_bytes()))


def test_test_kette_cli(tmp_path):
    ergebnis = _lauf("test-kette", "--aus", str(tmp_path / "k"))
    assert ergebnis.returncode == 0, ergebnis.stderr
    assert (tmp_path / "k" / "release.key").is_file()
    assert _lauf("test-kette", "--aus", str(tmp_path / "k")).returncode == 1
    assert _lauf().returncode == 2
