#!/usr/bin/env python3
"""Kettenwerkzeug des Betreibers (Sicherheit, S-3, S-10): Offline-Root, Release-Zertifikat und Test-Kette fuer die
Kettenpruefung des Geraets (smartheat_host.kette). Laeuft am Betreiberrechner und im Labor, nie auf dem Geraet. Nur
Standardbibliothek, cryptography und das Programm openssl (Verschluesseln des Root-Schluessels).

  ca.py root-anlegen --aus DIR
      Neue Offline-Root: DIR/root.crt (Zertifikat, 15 Jahre, CN "Setwell Gateway Release Root", CA mit keyCertSign,
      P-256) und DIR/root.key (PKCS#8, AES-256-CBC mit PBKDF2-HMAC-SHA256, 1 000 000 Runden). Die Passphrase (mindestens
      12 Zeichen) wird zweimal am Terminal abgefragt; ohne Terminal liest das Werkzeug zwei Zeilen von stdin.
  ca.py ausstellen --root-cert F --root-key F --csr F [--monate 24] --aus FILE
      Release-Zertifikat fuer den Schluessel einer CSR (der Schluessel liegt auf dem Hardware-Token und verlaesst ihn
      nie). Geprueft wird die CSR-Signatur und dass der Schluessel P-256 ist; Name und Erweiterungen der CSR werden nicht
      uebernommen, das Zertifikat traegt immer CN "Setwell Gateway Release", CA:FALSE (kritisch), KeyUsage
      digitalSignature (kritisch) und ExtendedKeyUsage codeSigning. notAfter ist hoechstens das Ende der Root.
      Die Passphrase der Root wird einmal abgefragt.
  ca.py test-kette --aus DIR
      Wegwerf-Kette fuer Labor und Probelaeufe: DIR/root.crt, DIR/release.crt, DIR/release.key (PEM, unverschluesselt,
      0600). Nie fuer echte Geraete: die Root traegt "(Test)" im Namen.

Bestehende Dateien werden nie ueberschrieben. Schluessel und Passphrasen erscheinen nie in der Ausgabe."""
import argparse
import getpass
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ROOT_CN = "Setwell Gateway Release Root"
TEST_ROOT_CN = "Setwell Gateway Release Root (Test)"
BLATT_CN = "Setwell Gateway Release"
ROOT_JAHRE = 15
MAX_MONATE = 24
MIN_PASSPHRASE = 12
PBKDF2_RUNDEN = 1_000_000
_KRYPTO_FEHLER = (ValueError, TypeError, UnsupportedAlgorithm)


class CaFehler(Exception):
    pass


def _jetzt() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _plus_monate(start: datetime, monate: int) -> datetime:
    """`monate` Kalendermonate spaeter, am Monatsende auf den letzten Tag des Zielmonats gekuerzt (31.1. + 1 = 28.2.)."""
    index = start.year * 12 + start.month - 1 + monate
    jahr, monat = divmod(index, 12)
    monat += 1
    for tag in range(start.day, 0, -1):
        try:
            return start.replace(year=jahr, month=monat, day=tag)
        except ValueError:
            continue
    raise AssertionError("unerreichbar: Tag 1 gibt es in jedem Monat")


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _schluessel() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _ist_p256(key: object) -> bool:
    return isinstance(key, ec.EllipticCurvePublicKey) and isinstance(key.curve, ec.SECP256R1)


def _root_zertifikat(key: ec.EllipticCurvePrivateKey, cn: str, jetzt: datetime) -> x509.Certificate:
    usage = x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                          data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=False,
                          encipher_only=False, decipher_only=False)
    return (x509.CertificateBuilder().subject_name(_name(cn)).issuer_name(_name(cn)).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(jetzt).not_valid_after(_plus_monate(jetzt, 12 * ROOT_JAHRE))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(usage, critical=True)
            .sign(key, hashes.SHA256()))


def _blatt(root_cert: x509.Certificate, root_key: ec.EllipticCurvePrivateKey, public_key: ec.EllipticCurvePublicKey,
           monate: int, jetzt: datetime) -> bytes:
    """Release-Zertifikat (PEM) fuer `public_key`; alles ausser dem Schluessel setzt allein dieses Werkzeug."""
    if not 1 <= monate <= MAX_MONATE:
        raise CaFehler(f"Monate muessen zwischen 1 und {MAX_MONATE} liegen")
    if not root_cert.not_valid_before_utc <= jetzt <= root_cert.not_valid_after_utc:
        raise CaFehler("Root ist abgelaufen oder noch nicht gueltig")
    bis = min(_plus_monate(jetzt, monate), root_cert.not_valid_after_utc)
    usage = x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                          data_encipherment=False, key_agreement=False, key_cert_sign=False, crl_sign=False,
                          encipher_only=False, decipher_only=False)
    cert = (x509.CertificateBuilder().subject_name(_name(BLATT_CN)).issuer_name(root_cert.subject)
            .public_key(public_key).serial_number(x509.random_serial_number())
            .not_valid_before(jetzt).not_valid_after(bis)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(usage, critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CODE_SIGNING]), critical=False)
            .sign(root_key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM)


def _verschluesseln(key: ec.EllipticCurvePrivateKey, passphrase: str) -> bytes:
    """PKCS#8 (PEM) mit AES-256-CBC und PBKDF2-HMAC-SHA256 (1 000 000 Runden). Den Schluessel nimmt openssl auf stdin, die
    Passphrase ueber eine Pipe (-passout fd:N): beides erscheint nie in einer Datei oder in den Prozessargumenten. Die
    Voreinstellung von cryptography (BestAvailableEncryption) rechnet nur mit 2048 Runden."""
    klar = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())
    lesen, schreiben = os.pipe()
    try:
        os.write(schreiben, passphrase.encode() + b"\n")
        os.close(schreiben)
        schreiben = -1
        ergebnis = subprocess.run(
            ["openssl", "pkcs8", "-topk8", "-inform", "DER", "-outform", "PEM", "-v2", "aes-256-cbc",
             "-v2prf", "hmacWithSHA256", "-iter", str(PBKDF2_RUNDEN), "-passout", f"fd:{lesen}"],
            input=klar, capture_output=True, pass_fds=(lesen,), check=False)
    except OSError:
        raise CaFehler("openssl nicht aufrufbar") from None
    finally:
        os.close(lesen)
        if schreiben >= 0:
            os.close(schreiben)
    if ergebnis.returncode != 0 or not ergebnis.stdout:
        raise CaFehler("openssl konnte den Schluessel nicht verschluesseln")
    return ergebnis.stdout


def _schreibe(pfad: Path, daten: bytes) -> None:
    """Legt die Datei neu an (0600); eine vorhandene wird nie ueberschrieben."""
    try:
        deskriptor = os.open(pfad, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise CaFehler(f"{pfad} existiert schon, nichts ueberschrieben") from None
    with os.fdopen(deskriptor, "wb") as datei:
        datei.write(daten)


def _ordner(aus: Path, namen: tuple[str, ...]) -> None:
    """Legt den Ordner an (0700); liegt dort schon eine der Dateien, bricht die Funktion ab, bevor etwas entsteht."""
    for name in namen:
        if (aus / name).exists():
            raise CaFehler(f"{aus / name} existiert schon, nichts ueberschrieben")
    aus.mkdir(mode=0o700, parents=True, exist_ok=True)
    aus.chmod(0o700)


def root_anlegen(aus: Path, passphrase: str, jetzt: datetime | None = None) -> None:
    if len(passphrase) < MIN_PASSPHRASE:
        raise CaFehler(f"Passphrase zu kurz (mindestens {MIN_PASSPHRASE} Zeichen)")
    jetzt = jetzt or _jetzt()
    _ordner(aus, ("root.crt", "root.key"))
    key = _schluessel()
    cert = _root_zertifikat(key, ROOT_CN, jetzt)
    verschluesselt = _verschluesseln(key, passphrase)
    _schreibe(aus / "root.key", verschluesselt)
    _schreibe(aus / "root.crt", cert.public_bytes(serialization.Encoding.PEM))


def _root_laden(root_cert_pem: bytes, root_key_pem: bytes, passphrase: str) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    try:
        cert = x509.load_pem_x509_certificate(root_cert_pem)
    except _KRYPTO_FEHLER:
        raise CaFehler("Root-Zertifikat ist kein PEM") from None
    try:
        basic = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    except x509.ExtensionNotFound:
        basic = None
    if basic is None or not basic.ca:
        raise CaFehler("Root-Zertifikat ist kein CA-Zertifikat")
    try:
        key = serialization.load_pem_private_key(root_key_pem, passphrase.encode())
    except _KRYPTO_FEHLER:
        raise CaFehler("Passphrase falsch oder Root-Schluessel unlesbar") from None
    public = cert.public_key()
    if not (isinstance(key, ec.EllipticCurvePrivateKey) and isinstance(public, ec.EllipticCurvePublicKey)
            and _ist_p256(public) and key.public_key().public_numbers() == public.public_numbers()):
        raise CaFehler("Root-Schluessel passt nicht zum Root-Zertifikat (oder ist nicht P-256)")
    return cert, key


def ausstellen(root_cert_pem: bytes, root_key_pem: bytes, passphrase: str, csr_pem: bytes, monate: int,
               jetzt: datetime | None = None) -> bytes:
    """Release-Zertifikat (PEM) fuer den Schluessel der CSR, von der Root ausgestellt."""
    jetzt = jetzt or _jetzt()
    cert, key = _root_laden(root_cert_pem, root_key_pem, passphrase)
    try:
        csr = x509.load_pem_x509_csr(csr_pem)
    except _KRYPTO_FEHLER:
        raise CaFehler("CSR ist kein PEM") from None
    if not csr.is_signature_valid:
        raise CaFehler("CSR: Signatur ungueltig")
    public = csr.public_key()
    if not isinstance(public, ec.EllipticCurvePublicKey) or not _ist_p256(public):
        raise CaFehler("CSR: Schluessel ist nicht P-256")
    return _blatt(cert, key, public, monate, jetzt)


def test_kette_anlegen(aus: Path, jetzt: datetime | None = None) -> None:
    jetzt = jetzt or _jetzt()
    _ordner(aus, ("root.crt", "release.crt", "release.key"))
    root_key = _schluessel()
    root = _root_zertifikat(root_key, TEST_ROOT_CN, jetzt)
    blatt_key = _schluessel()
    blatt = _blatt(root, root_key, blatt_key.public_key(), MAX_MONATE, jetzt)
    _schreibe(aus / "release.key", blatt_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                            serialization.NoEncryption()))
    _schreibe(aus / "release.crt", blatt)
    _schreibe(aus / "root.crt", root.public_bytes(serialization.Encoding.PEM))


def _zeile(frage: str) -> str:
    """Eine Zeile von stdin (ohne Terminal, etwa in Tests und Skripten); EOF ist ein Fehler."""
    zeile = sys.stdin.readline()
    if not zeile:
        raise CaFehler(f"{frage}: keine Eingabe")
    return zeile.rstrip("\n")


def _passphrase(frage: str, wiederholen: bool) -> str:
    if sys.stdin.isatty():
        erste = getpass.getpass(f"{frage}: ")
        if wiederholen and getpass.getpass("Wiederholen: ") != erste:
            raise CaFehler("Passphrasen verschieden")
        return erste
    erste = _zeile(frage)
    if wiederholen and _zeile(frage) != erste:
        raise CaFehler("Passphrasen verschieden")
    return erste


def _fingerabdruck(cert_pem: bytes) -> str:
    return x509.load_pem_x509_certificate(cert_pem).fingerprint(hashes.SHA256()).hex()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    teile = parser.add_subparsers(dest="befehl", required=True)
    root = teile.add_parser("root-anlegen", help="Offline-Root anlegen")
    root.add_argument("--aus", type=Path, required=True, metavar="DIR")
    ausgabe = teile.add_parser("ausstellen", help="Release-Zertifikat fuer eine CSR ausstellen")
    ausgabe.add_argument("--root-cert", type=Path, required=True)
    ausgabe.add_argument("--root-key", type=Path, required=True)
    ausgabe.add_argument("--csr", type=Path, required=True)
    ausgabe.add_argument("--monate", type=int, default=MAX_MONATE)
    ausgabe.add_argument("--aus", type=Path, required=True, metavar="FILE")
    test = teile.add_parser("test-kette", help="Wegwerf-Kette fuer Labor und Probelaeufe")
    test.add_argument("--aus", type=Path, required=True, metavar="DIR")
    args = parser.parse_args(argv)
    try:
        if args.befehl == "root-anlegen":
            root_anlegen(args.aus, _passphrase("Passphrase fuer den Root-Schluessel", True))
            print(f"Root angelegt: {args.aus}/root.crt, {args.aus}/root.key")
            print(f"SHA-256 der Root: {_fingerabdruck((args.aus / 'root.crt').read_bytes())}")
        elif args.befehl == "ausstellen":
            if args.aus.exists():
                raise CaFehler(f"{args.aus} existiert schon, nichts ueberschrieben")
            pem = ausstellen(args.root_cert.read_bytes(), args.root_key.read_bytes(),
                             _passphrase("Passphrase des Root-Schluessels", False), args.csr.read_bytes(), args.monate)
            _schreibe(args.aus, pem)
            gueltig = x509.load_pem_x509_certificate(pem).not_valid_after_utc.isoformat()
            print(f"Release-Zertifikat ausgestellt: {args.aus} (gueltig bis {gueltig})")
        else:
            test_kette_anlegen(args.aus)
            print(f"Test-Kette angelegt: {args.aus} (root.crt, release.crt, release.key)")
    except (CaFehler, OSError) as fehler:
        print(f"FEHLER: {fehler}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
