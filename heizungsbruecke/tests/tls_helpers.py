"""Test-PKI fuer smartheat_transport (nur Tests; cryptography steht im Extra dev). Die Erweiterungen
genuegen dem strengen X.509-Modus von Python 3.13 (Key Identifier, Key Usage)."""
import datetime
import ipaddress

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def _name(cn):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _validity(builder):
    now = datetime.datetime.now(datetime.UTC)
    return builder.not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1))


def _key_pem(key) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ).decode()


def _cert_pem(cert) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def make_ca(cn: str = "SmartHeat Test-CA"):
    key = ec.generate_private_key(ec.SECP256R1())
    builder = _validity(
        x509.CertificateBuilder().subject_name(_name(cn)).issuer_name(_name(cn)).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
    )
    cert = (
        builder.add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=False, content_commitment=False, key_encipherment=False, data_encipherment=False,
            key_agreement=False, key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False,
        ), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def issue(ca, cn: str, *, server: bool = False) -> tuple[str, str]:
    ca_key, ca_cert = ca
    key = ec.generate_private_key(ec.SECP256R1())
    builder = _validity(
        x509.CertificateBuilder().subject_name(_name(cn)).issuer_name(ca_cert.subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
    )
    builder = (
        builder.add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=False, data_encipherment=False,
            key_agreement=False, key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False,
        ), critical=True)
        .add_extension(x509.ExtendedKeyUsage(
            [ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
    )
    if server:
        builder = builder.add_extension(x509.SubjectAlternativeName(
            [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
    return _key_pem(key), _cert_pem(builder.sign(ca_key, hashes.SHA256()))


def ca_pem(ca) -> str:
    return _cert_pem(ca[1])
