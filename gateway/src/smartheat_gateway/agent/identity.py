"""Identitaet des Geraets (Spec SHG G2 6.2): Signierschluessel ECDSA P-256 und Verschluesselungsschluessel ECDH P-256
(G4, passwort_verschluesselt) unter /data/device/ (0600), Geraete-ID aus dem oeffentlichen Signierschluessel,
Uebernahme-Code 12 Zeichen Base32 (60 Bit). Neuer Code nur per Befehl new_claim_code."""
import base64
import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from smartheat_gateway.agent import wire
from smartheat_gateway.files import write_text_private
from smartheat_gateway.paths import Paths


@dataclass(frozen=True)
class Identity:
    device_id: str
    signing_key: ec.EllipticCurvePrivateKey
    enc_key: ec.EllipticCurvePrivateKey
    claim_code: str


def _der(public_key) -> bytes:
    return public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def device_id_for(public_key) -> str:
    digest = base64.b32encode(hashlib.sha256(_der(public_key)).digest()).decode()
    return wire.DEVICE_ID_PREFIX + digest[: wire.DEVICE_ID_HASH_CHARS].lower()


def public_key_b64(key: ec.EllipticCurvePrivateKey) -> str:
    return base64.b64encode(_der(key.public_key())).decode()


def new_claim_code(randbytes: Callable[[int], bytes] = secrets.token_bytes) -> str:
    return base64.b32encode(randbytes(8)).decode()[: wire.CLAIM_CODE_CHARS]


def _key(path: Path) -> ec.EllipticCurvePrivateKey:
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except FileNotFoundError:
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
        )
        write_text_private(path, pem.decode())
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise TypeError(f"{path.name} ist kein EC-Schluessel")
    return key


def load_or_create(paths: Paths, randbytes: Callable[[int], bytes] = secrets.token_bytes) -> Identity:
    signing = _key(paths.device_dir / "key.pem")
    enc = _key(paths.device_dir / "enc_key.pem")
    code_path = paths.device_dir / "claim_code"
    try:
        code = code_path.read_text().strip()
    except FileNotFoundError:
        code = new_claim_code(randbytes)
        write_text_private(code_path, code)
    return Identity(device_id_for(signing.public_key()), signing, enc, code)


def replace_claim_code(
    paths: Paths, identity: Identity, randbytes: Callable[[int], bytes] = secrets.token_bytes,
) -> Identity:
    code = new_claim_code(randbytes)
    while code == identity.claim_code:
        code = new_claim_code(randbytes)
    write_text_private(paths.device_dir / "claim_code", code)
    return Identity(identity.device_id, identity.signing_key, identity.enc_key, code)


def sign(key: ec.EllipticCurvePrivateKey, method: str, path: str, timestamp: int, body: bytes) -> str:
    der = key.sign(wire.canonical_string(method, path, timestamp, body), ec.ECDSA(hashes.SHA256()))
    return base64.urlsafe_b64encode(der).decode().rstrip("=")


def register_body(identity: Identity, version: str, capabilities: dict) -> dict:
    return {
        "device_id": identity.device_id,
        "public_key": public_key_b64(identity.signing_key),
        "enc_public_key": public_key_b64(identity.enc_key),
        "claim_code_hash": wire.claim_code_hash(identity.device_id, identity.claim_code),
        "version": version,
        "capabilities": capabilities,
    }
