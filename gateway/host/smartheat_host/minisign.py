"""Pruefer fuer minisign-Signaturen (Spec SHG G2b-1 7): Ed25519, Format des Programms minisign (Signatur ueber die
Datei 'Ed' oder ueber ihren BLAKE2b-512-Hash 'ED', dazu die globale Signatur ueber Signatur + vertrauenswuerdigen
Kommentar). Nur Standardbibliothek und cryptography (python3-cryptography auf dem Pi)."""
import base64
import binascii
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

PLACEHOLDER_MARKER = "SMARTHEAT-PLATZHALTER"
_TRUSTED = "trusted comment: "


class SignatureError(Exception):
    pass


@dataclass(frozen=True)
class PublicKey:
    key_id: bytes
    key: Ed25519PublicKey


def _b64(line: str, length: int, what: str) -> bytes:
    try:
        raw = base64.b64decode(line.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise SignatureError(f"{what}: kein Base64") from None
    if len(raw) != length:
        raise SignatureError(f"{what}: falsche Laenge")
    return raw


def load_public_key(text: str) -> PublicKey:
    if PLACEHOLDER_MARKER in text:
        raise SignatureError("release.pub ist ein Platzhalter, Updates sind gesperrt")
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2 or not lines[0].startswith("untrusted comment:"):
        raise SignatureError("Oeffentlicher Schluessel: unbekanntes Format")
    raw = _b64(lines[1], 42, "Oeffentlicher Schluessel")
    if raw[:2] != b"Ed":
        raise SignatureError("Oeffentlicher Schluessel: kein Ed25519")
    return PublicKey(raw[2:10], Ed25519PublicKey.from_public_bytes(raw[10:]))


def verify(public: PublicKey, data: bytes, signature_text: str) -> str:
    lines = signature_text.splitlines()
    if len(lines) < 4 or not lines[0].startswith("untrusted comment:") or not lines[2].startswith(_TRUSTED):
        raise SignatureError("Signatur: unbekanntes Format")
    raw = _b64(lines[1], 74, "Signatur")
    algorithm, key_id, signature = raw[:2], raw[2:10], raw[10:]
    if algorithm not in (b"Ed", b"ED"):
        raise SignatureError("Signatur: unbekannter Algorithmus")
    if key_id != public.key_id:
        raise SignatureError("Signatur stammt von einem anderen Schluessel")
    message = data if algorithm == b"Ed" else hashlib.blake2b(data, digest_size=64).digest()
    comment = lines[2][len(_TRUSTED):]
    try:
        public.key.verify(signature, message)
        public.key.verify(_b64(lines[3], 64, "Globale Signatur"), signature + comment.encode())
    except InvalidSignature:
        raise SignatureError("Signatur ungueltig") from None
    return comment


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 3:
        print("Aufruf: python3 -m smartheat_host.minisign PUB DATEI SIG", file=sys.stderr)
        return 2
    try:
        comment = verify(load_public_key(Path(args[0]).read_text()), Path(args[1]).read_bytes(),
                         Path(args[2]).read_text())
    except (OSError, SignatureError) as error:
        print(f"UNGUELTIG: {error}", file=sys.stderr)
        return 1
    print(f"OK: {comment}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
