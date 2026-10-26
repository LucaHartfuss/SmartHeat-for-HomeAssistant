"""Erzeugt Test-Ketten fuer smartheat_host.kette: Offline-Root und befristetes Release-Zertifikat (X.509, P-256) sowie
Signaturtexte im Format smartheat-kette-1. Feste Zeitbasis: Tests geben `jetzt` selbst vor."""
import base64
import json
from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

BASIS = datetime(2026, 1, 1, tzinfo=UTC)
P256 = ec.SECP256R1()
FORMAT = "smartheat-kette-1"  # absichtlich nicht aus kette.py importiert: der Test haengt nicht am Pruefer
_FREMD_OID = x509.ObjectIdentifier("1.3.6.1.4.1.99999.1")


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def root(cn="Test Root", tage=5475, *, key=None, von=BASIS, ca=True, key_cert_sign=True, kurve=P256):
    """(Schluessel, Zertifikat) einer selbstsignierten Root; gueltig von `von` fuer `tage` Tage."""
    key = key or ec.generate_private_key(kurve)
    usage = x509.KeyUsage(digital_signature=not key_cert_sign, content_commitment=False, key_encipherment=False,
                          data_encipherment=False, key_agreement=False, key_cert_sign=key_cert_sign,
                          crl_sign=key_cert_sign, encipher_only=False, decipher_only=False)
    builder = (x509.CertificateBuilder().subject_name(_name(cn)).issuer_name(_name(cn))
               .public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(von).not_valid_after(von + timedelta(days=tage))
               .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
               .add_extension(usage, critical=True))
    return key, builder.sign(key, hashes.SHA256())


def release(root_key, root_cert, *, von, bis, ca=False, eku=(ExtendedKeyUsageOID.CODE_SIGNING,), kurve=P256,
            extra_kritisch=False, digital_signature=True, ku_kritisch=True, signatur_hash=None):
    """(Schluessel, Zertifikat) eines Release-Zertifikats, von der Root signiert. `eku=None` laesst die Erweiterung
    weg; `signatur_hash` waehlt den Hash der Root-Signatur (Vorgabe SHA-256)."""
    key = ec.generate_private_key(kurve)
    usage = x509.KeyUsage(digital_signature=digital_signature, content_commitment=False, key_encipherment=False,
                          data_encipherment=False, key_agreement=False, key_cert_sign=ca, crl_sign=False,
                          encipher_only=False, decipher_only=False)
    builder = (x509.CertificateBuilder().subject_name(_name("Test Release")).issuer_name(root_cert.subject)
               .public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(von).not_valid_after(bis)
               .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
               .add_extension(usage, critical=ku_kritisch))
    if eku is not None:
        builder = builder.add_extension(x509.ExtendedKeyUsage(list(eku)), critical=False)
    if extra_kritisch:
        builder = builder.add_extension(x509.UnrecognizedExtension(_FREMD_OID, b"\x05\x00"), critical=True)
    return key, builder.sign(root_key, signatur_hash or hashes.SHA256())


def signiere(key, cert, data: bytes) -> str:
    """Signaturtext smartheat-kette-1: Zertifikat (PEM) und ECDSA-SHA256-Signatur (DER, Base64) ueber `data`."""
    signatur = key.sign(data, ec.ECDSA(hashes.SHA256()))
    return json.dumps({"format": FORMAT, "zertifikat": cert.public_bytes(serialization.Encoding.PEM).decode(),
                       "signatur": base64.b64encode(signatur).decode()})


def roots_pem(*certs) -> str:
    return "".join(cert.public_bytes(serialization.Encoding.PEM).decode() for cert in certs)
