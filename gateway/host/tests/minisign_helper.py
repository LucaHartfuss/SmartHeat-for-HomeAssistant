"""Erzeugt minisign-kompatible Schluessel und Signaturen fuer Tests (gleiches Format wie das Programm minisign)."""
import base64
import hashlib
import secrets

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


def keypair():
    secret = Ed25519PrivateKey.generate()
    key_id = secrets.token_bytes(8)
    raw = secret.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    public_text = "untrusted comment: test key\n" + base64.b64encode(b"Ed" + key_id + raw).decode() + "\n"
    return secret, key_id, public_text


def sign(secret, key_id: bytes, data: bytes, comment: str = "test", prehash: bool = True) -> str:
    message = hashlib.blake2b(data, digest_size=64).digest() if prehash else data
    signature = secret.sign(message)
    alg = b"ED" if prehash else b"Ed"
    global_signature = secret.sign(signature + comment.encode())
    return (
        "untrusted comment: test signature\n"
        + base64.b64encode(alg + key_id + signature).decode() + "\n"
        + f"trusted comment: {comment}\n"
        + base64.b64encode(global_signature).decode() + "\n"
    )
