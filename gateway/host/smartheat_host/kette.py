"""Pruefer fuer Release-Signaturen per X.509-Kette (Sicherheit, S-3, S-10): Offline-Root -> befristetes
Release-Zertifikat -> ECDSA-Signatur (P-256, SHA-256) ueber die Daten. Das Geraet vertraut nur dem Schluesselbund
(Roots), den es im Image traegt; das mitgelieferte Zertifikat muss von einer dieser Roots stammen, zur Pruefzeit
gelten und ein reines Code-Signing-Blatt sein. Nur Standardbibliothek und cryptography (python3-cryptography auf dem
Pi, Debian 13: Version 43)."""
import base64
import binascii
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, ExtensionOID, SignatureAlgorithmOID

PLACEHOLDER_MARKER = "SMARTHEAT-PLATZHALTER"
MAX_SIGNATUR_BYTES = 16384  # Signaturtext: ein Zertifikat und eine Signatur, weit unter 4 KiB; mehr ist ein Angriff
FORMAT = "smartheat-kette-1"
_FELDER = {"format", "zertifikat", "signatur"}
# Kritische Erweiterungen, die der Pruefer versteht und selbst auswertet; jede andere kritische lehnt ab (RFC 5280).
_BEKANNTE_KRITISCHE = {ExtensionOID.BASIC_CONSTRAINTS, ExtensionOID.KEY_USAGE, ExtensionOID.EXTENDED_KEY_USAGE}
# Fehler, die cryptography beim Laden und Pruefen fremder Zertifikate wirft.
_KRYPTO_FEHLER = (ValueError, TypeError, UnsupportedAlgorithm, InvalidSignature)


class SignatureError(Exception):
    pass


def _p256(cert: x509.Certificate) -> ec.EllipticCurvePublicKey | None:
    """Oeffentlicher Schluessel des Zertifikats, wenn er auf P-256 liegt, sonst None (auch bei unbekannter Art)."""
    try:
        key = cert.public_key()
    except (ValueError, UnsupportedAlgorithm):
        return None
    return key if isinstance(key, ec.EllipticCurvePublicKey) and isinstance(key.curve, ec.SECP256R1) else None


def _extension(cert: x509.Certificate, klasse):
    try:
        return cert.extensions.get_extension_for_class(klasse)
    except x509.ExtensionNotFound:
        return None


def _pem_zertifikate(text: str) -> list[x509.Certificate]:
    try:
        return x509.load_pem_x509_certificates(text.encode())
    except (ValueError, UnsupportedAlgorithm):
        return []


def load_roots(pem_text: str) -> list[x509.Certificate]:
    """Schluesselbund der Roots (PEM, ein oder mehrere Zertifikate). Jede Root muss CA sein, keyCertSign tragen und
    einen P-256-Schluessel haben; eine schlechte Root verdirbt den ganzen Bund. Der Platzhalter sperrt alles."""
    if PLACEHOLDER_MARKER in pem_text:
        raise SignatureError("Schluesselbund ist ein Platzhalter, Updates sind gesperrt")
    roots = _pem_zertifikate(pem_text)
    if not roots:
        raise SignatureError("Root: keine Zertifikate")
    for root in roots:
        basic = _extension(root, x509.BasicConstraints)
        if basic is None or basic.value.ca is not True:
            raise SignatureError("Root: kein CA-Zertifikat")
        usage = _extension(root, x509.KeyUsage)
        if usage is None or not usage.value.key_cert_sign:
            raise SignatureError("Root: key_cert_sign fehlt")
        if _p256(root) is None:
            raise SignatureError("Root: Schluessel nicht P-256")
    return roots


def roots_identity(pem_text: str) -> str:
    """Kennung des Schluesselbunds: SHA-256 (hex) ueber die sortierten SHA-256-Fingerabdruecke aller Roots. Aendert
    sich mit jeder hinzugekommenen, entfallenen oder getauschten Root, nicht mit ihrer Reihenfolge."""
    abdruecke = sorted(root.fingerprint(hashes.SHA256()) for root in load_roots(pem_text))
    return hashlib.sha256(b"".join(abdruecke)).hexdigest()


def _felder(signatur_text: str) -> tuple[str, str]:
    if len(signatur_text.encode()) > MAX_SIGNATUR_BYTES:
        raise SignatureError("Signaturtext zu lang")
    try:
        dokument = json.loads(signatur_text)
    except (ValueError, RecursionError):
        raise SignatureError("Signaturtext: kein JSON") from None
    if not isinstance(dokument, dict) or set(dokument) != _FELDER:
        raise SignatureError("Signaturtext: Felder fehlen oder sind unbekannt")
    if dokument["format"] != FORMAT:
        raise SignatureError("Signaturtext: falsches Format")
    if not isinstance(dokument["zertifikat"], str) or not isinstance(dokument["signatur"], str):
        raise SignatureError("Signaturtext: Felder fehlen oder sind unbekannt")
    return dokument["zertifikat"], dokument["signatur"]


def _gilt(cert: x509.Certificate, jetzt: datetime) -> bool:
    return cert.not_valid_before_utc <= jetzt <= cert.not_valid_after_utc


def _pruefe_blatt(cert: x509.Certificate) -> ec.EllipticCurvePublicKey:
    """Das Blatt muss ein reines Code-Signing-Zertifikat sein; liefert seinen P-256-Schluessel."""
    basic = _extension(cert, x509.BasicConstraints)
    if basic is None:
        raise SignatureError("Zertifikat: BasicConstraints fehlt")
    if basic.value.ca is not False:
        raise SignatureError("Zertifikat: CA-Zertifikat nicht erlaubt")
    usage = _extension(cert, x509.KeyUsage)
    if usage is None or not usage.critical or not usage.value.digital_signature:
        raise SignatureError("Zertifikat: KeyUsage digitalSignature (kritisch) fehlt")
    eku = _extension(cert, x509.ExtendedKeyUsage)
    if eku is None or ExtendedKeyUsageOID.CODE_SIGNING not in eku.value:
        raise SignatureError("Zertifikat: Code-Signing fehlt")
    for extension in cert.extensions:
        if extension.critical and extension.oid not in _BEKANNTE_KRITISCHE:
            raise SignatureError("Zertifikat: unbekannte kritische Erweiterung")
    key = _p256(cert)
    if key is None:
        raise SignatureError("Zertifikat: Schluessel nicht P-256")
    if cert.signature_algorithm_oid != SignatureAlgorithmOID.ECDSA_WITH_SHA256:
        raise SignatureError("Zertifikat: Signaturalgorithmus nicht ecdsa-with-SHA256")
    return key


def verify(roots: list[x509.Certificate], data: bytes, signatur_text: str, jetzt: datetime,
           untergrenze: datetime | None = None) -> x509.Certificate:
    """Prueft, dass `signatur_text` (smartheat-kette-1) die Signatur eines von einer der `roots` ausgestellten,
    zu `jetzt` gueltigen Code-Signing-Zertifikats ueber `data` ist; liefert das Zertifikat. `untergrenze`: kein
    Zertifikat, das vor diesem Zeitpunkt beginnt (Schutz vor einem Rueckfall auf altes Material, FR-25).
    `jetzt` und `untergrenze` muessen eine Zeitzone tragen."""
    if jetzt.tzinfo is None or (untergrenze is not None and untergrenze.tzinfo is None):
        raise ValueError("jetzt und untergrenze brauchen eine Zeitzone")
    pem, signatur_b64 = _felder(signatur_text)
    zertifikate = _pem_zertifikate(pem)
    if len(zertifikate) != 1:
        raise SignatureError("Signaturtext: Zertifikat: kein PEM")
    cert = zertifikate[0]
    aussteller = []
    for root in roots:
        try:
            cert.verify_directly_issued_by(root)
        except _KRYPTO_FEHLER:
            continue
        aussteller.append(root)
    if not aussteller:
        raise SignatureError("Zertifikat stammt von keiner vertrauten Root")
    if not any(_gilt(root, jetzt) for root in aussteller):
        raise SignatureError("Root abgelaufen oder noch nicht gueltig")
    if jetzt < cert.not_valid_before_utc:
        raise SignatureError("Zertifikat noch nicht gueltig")
    if jetzt > cert.not_valid_after_utc:
        raise SignatureError("Zertifikat abgelaufen")
    if untergrenze is not None and cert.not_valid_before_utc < untergrenze:
        raise SignatureError("Zertifikat beginnt vor der Untergrenze")
    key = _pruefe_blatt(cert)
    try:
        signatur = base64.b64decode(signatur_b64, validate=True)
    except (binascii.Error, ValueError):
        raise SignatureError("Signatur: kein Base64") from None
    try:
        key.verify(signatur, data, ec.ECDSA(hashes.SHA256()))
    except _KRYPTO_FEHLER:
        raise SignatureError("Signatur ungueltig") from None
    return cert


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 3:
        print("Aufruf: python3 -m smartheat_host.kette ROOTS DATEI SIG", file=sys.stderr)
        return 2
    try:
        roots = load_roots(Path(args[0]).read_text())
        cert = verify(roots, Path(args[1]).read_bytes(), Path(args[2]).read_text(), datetime.now(UTC))
    except (OSError, ValueError, SignatureError) as error:
        print(f"UNGUELTIG: {error}", file=sys.stderr)
        return 1
    print(f"OK: {cert.subject.rfc4514_string()} (gueltig bis {cert.not_valid_after_utc.isoformat()})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
