"""Hilfen fuer die Tests des Geraetekerns (Plan 5b D1): Test-CA, die CSRs des Geraets signiert (wie die lokale Test-CA
des Servers, S1 Task 6). Plain-Import (`from device_helpers import ...`), tests/ hat kein __init__.py."""
import datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from smartheat_device import identity


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class GeraeteCa:
    """Selbstsignierte EC-CA; sign_csr stellt ein Client-Zertifikat fuer den Schluessel und CN des CSR aus."""

    def __init__(self, cn: str = "smartheat-test-ca") -> None:
        self._key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
        self._cert = (
            x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(self._key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(_now() - datetime.timedelta(minutes=1))
            .not_valid_after(_now() + datetime.timedelta(days=2))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=False, content_commitment=False, key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False,
            ), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(self._key.public_key()), critical=False)
            .sign(self._key, hashes.SHA256())
        )
        self.pem = self._cert.public_bytes(serialization.Encoding.PEM).decode()

    def sign_csr(self, csr_pem: str) -> str:
        csr = x509.load_pem_x509_csr(csr_pem.encode())
        cert = (
            x509.CertificateBuilder().subject_name(csr.subject).issuer_name(self._cert.subject)
            .public_key(csr.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(_now() - datetime.timedelta(minutes=1))
            .not_valid_after(_now() + datetime.timedelta(days=2))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self._key.public_key()), critical=False)
            .sign(self._key, hashes.SHA256())
        )
        return cert.public_bytes(serialization.Encoding.PEM).decode()


def zertifikat_fuer(ident: identity.Identity, ca: GeraeteCa) -> str:
    return ca.sign_csr(identity.csr_pem(ident))


def csr_passt(csr_pem: str, ident: identity.Identity) -> bool:
    """Gehoert der CSR zum Geraet? CN = Geraete-ID, gueltige Signatur, oeffentlicher Schluessel = TLS-Schluessel.
    (ECDSA ist randomisiert: zwei csr_pem-Aufrufe sind nie byte-gleich, daher kein PEM-Vergleich.)"""
    csr = x509.load_pem_x509_csr(csr_pem.encode())
    cn = csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    return (cn == ident.device_id and csr.is_signature_valid
            and csr.public_key().public_numbers() == ident.tls_key.public_key().public_numbers())
