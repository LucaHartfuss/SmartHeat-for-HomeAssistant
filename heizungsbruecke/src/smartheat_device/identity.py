"""Identitaet des Geraets: Signierschluessel ECDSA P-256 (Geraete-ID und
signierte Bootstrap-Anfragen), eigener TLS-Schluessel fuer das MQTT-Zertifikat (CSR mit CN = Geraete-ID) und
Uebernahme-Code (12 Zeichen Base32, 60 Bit), alle unter <daten>/device/ (0600). Den Verschluesselungsschluessel enc_key.pem erzeugt das Geraet nicht; eine
vorhandene Datei bleibt liegen und wird nicht gelesen. Geraete-ID = shg- + die ersten 16
Zeichen von Base32(SHA-256(DER-SPKI)) in Kleinbuchstaben (unveraendert). boot_id: neu je Prozessstart (Erkennung doppelt
laufender Geraete). Schluessel und Code erscheinen nie in einem Log (Regel 6)."""
import base64
import hashlib
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from smartheat_core import dateien
from smartheat_device import nachrichten, wire

KEY_FILE = "key.pem"
TLS_KEY_FILE = "tls_key.pem"
CLAIM_CODE_FILE = "claim_code"


@dataclass(frozen=True)
class Identity:
    device_id: str
    signing_key: ec.EllipticCurvePrivateKey = field(repr=False)
    tls_key: ec.EllipticCurvePrivateKey = field(repr=False)
    claim_code: str = field(repr=False)


def _der(public_key) -> bytes:
    return public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def device_id_for(public_key) -> str:
    digest = base64.b32encode(hashlib.sha256(_der(public_key)).digest()).decode()
    return wire.DEVICE_ID_PREFIX + digest[: wire.DEVICE_ID_HASH_CHARS].lower()


def public_key_b64(key: ec.EllipticCurvePrivateKey) -> str:
    return _KODIERUNGEN[wire.PUBLIC_KEY_ENCODING](_der(key.public_key()))


def _pem(key: ec.EllipticCurvePrivateKey) -> str:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def _key(path: Path) -> ec.EllipticCurvePrivateKey:
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except FileNotFoundError:
        key = ec.generate_private_key(ec.SECP256R1())
        dateien.schreiben_text(path, _pem(key), modus=dateien.PRIVAT)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise TypeError(f"{path.name} ist kein EC-Schluessel")
    return key


def new_claim_code(randbytes: Callable[[int], bytes] = secrets.token_bytes) -> str:
    return base64.b32encode(randbytes(8)).decode()[: wire.CLAIM_CODE_CHARS]


def load_or_create(device_dir: Path, randbytes: Callable[[int], bytes] = secrets.token_bytes) -> Identity:
    signing = _key(device_dir / KEY_FILE)
    tls = _key(device_dir / TLS_KEY_FILE)
    code_path = device_dir / CLAIM_CODE_FILE
    try:
        code = code_path.read_text().strip()
    except FileNotFoundError:
        code = new_claim_code(randbytes)
        dateien.schreiben_text(code_path, code, modus=dateien.PRIVAT)
    return Identity(device_id_for(signing.public_key()), signing, tls, code)


def save_claim_code(device_dir: Path, ident: Identity, code: str) -> Identity:
    """Neuer Uebernahme-Code (Befehl new_claim_code): erst gespeichert, wenn der Server ihn angenommen hat."""
    dateien.schreiben_text(device_dir / CLAIM_CODE_FILE, code, modus=dateien.PRIVAT)
    return replace(ident, claim_code=code)


def _base64url_ohne_padding(daten: bytes) -> str:
    return base64.urlsafe_b64encode(daten).decode().rstrip("=")


def _base64(daten: bytes) -> str:
    return base64.b64encode(daten).decode()


#: Kodierungen des Vertrags (wire.SIGNATURE_ENCODING, wire.PUBLIC_KEY_ENCODING). Nennt der Vertrag eine andere,
#: scheitert das Geraet laut (KeyError) statt still falsch zu kodieren.
_KODIERUNGEN = {"base64url_unpadded": _base64url_ohne_padding, "base64_der_spki": _base64}
assert {wire.SIGNATURE_ENCODING, wire.PUBLIC_KEY_ENCODING} <= set(_KODIERUNGEN), "Kodierung des Vertrags unbekannt"


def sign(key: ec.EllipticCurvePrivateKey, method: str, path: str, timestamp: int, body: bytes) -> str:
    der = key.sign(wire.canonical_string(method, path, timestamp, body), ec.ECDSA(hashes.SHA256()))
    return _KODIERUNGEN[wire.SIGNATURE_ENCODING](der)


def csr_pem(ident: Identity) -> str:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, ident.device_id)])
    csr = x509.CertificateSigningRequestBuilder().subject_name(name).sign(ident.tls_key, hashes.SHA256())
    return csr.public_bytes(serialization.Encoding.PEM).decode()


def tls_key_pem(ident: Identity) -> str:
    return _pem(ident.tls_key)


def passt_zum_schluessel(ident: Identity, certificate_pem: str) -> bool:
    """Gehoert das Zertifikat aus der Bootstrap-Antwort zum eigenen TLS-Schluessel? (Schutz vor einer Verwechslung.)"""
    try:
        cert = x509.load_pem_x509_certificate(certificate_pem.encode())
    except ValueError:
        return False
    return _der(cert.public_key()) == _der(ident.tls_key.public_key())


def register_body(ident: Identity, *, version: str, host: str, capabilities: dict) -> dict:
    return nachrichten.register(
        device_id=ident.device_id, public_key=public_key_b64(ident.signing_key),
        claim_code_hash=wire.claim_code_hash(ident.device_id, ident.claim_code), version=version, host=host,
        capabilities=capabilities, csr=csr_pem(ident))


def new_boot_id() -> str:
    return uuid.uuid4().hex
