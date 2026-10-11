"""Test-CA der Gateway-Tests (Plan 5b D2): signiert Geraete-CSRs (CN = Geraete-ID, clientAuth). Eine Stelle fuer die
Unit-Tests (geraet_fakes, world) und den Fake-Bootstrap der Docker-Pruefungen, der sie im Container ohne die uebrigen
Test-Hilfen importiert; daher nur cryptography. Neu erzeugt (Unit-Tests) oder aus ca.pem/ca.key eines Ordners geladen
(Test-PKI der Docker-Pruefungen, tests/gateway_lib.sh make_pki; je Lauf neu, nie eingecheckt)."""
import datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class GeraeteCa:
    def __init__(self, key=None, cert: x509.Certificate | None = None) -> None:
        if key is None or cert is None:
            key = ec.generate_private_key(ec.SECP256R1())
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "smartheat-test-ca")])
            cert = (
                x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(_now() - datetime.timedelta(minutes=1))
                .not_valid_after(_now() + datetime.timedelta(days=2))
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                .sign(key, hashes.SHA256()))
        self._key, self._cert = key, cert
        self.pem = cert.public_bytes(serialization.Encoding.PEM).decode()

    @classmethod
    def laden(cls, ca_dir: Path) -> "GeraeteCa":
        """CA aus ca_dir/ca.pem und ca_dir/ca.key (PEM, ohne Passwort)."""
        cert = x509.load_pem_x509_certificate((ca_dir / "ca.pem").read_bytes())
        key = serialization.load_pem_private_key((ca_dir / "ca.key").read_bytes(), password=None)
        return cls(key, cert)

    def sign_csr(self, csr_pem: str) -> str:
        """Wirft ValueError, wenn csr_pem kein CSR ist. Subjekt (CN) bleibt wie im CSR."""
        csr = x509.load_pem_x509_csr(csr_pem.encode())
        cert = (
            x509.CertificateBuilder().subject_name(csr.subject).issuer_name(self._cert.subject)
            .public_key(csr.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(_now() - datetime.timedelta(minutes=1)).not_valid_after(_now() + datetime.timedelta(days=2))
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
            .sign(self._key, hashes.SHA256()))
        return cert.public_bytes(serialization.Encoding.PEM).decode()
