"""Spec 5b 1: Identitaet des Geraets (aus gateway agent/identity.py) mit TLS-Schluessel und CSR."""
import base64
import hashlib
import stat

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from device_helpers import GeraeteCa, csr_passt, zertifikat_fuer

from smartheat_device import identity, wire


def test_keys_and_code_are_created_once_and_private(tmp_path):
    first = identity.load_or_create(tmp_path)
    second = identity.load_or_create(tmp_path)
    assert (first.device_id, first.claim_code) == (second.device_id, second.claim_code)
    for name in (identity.KEY_FILE, identity.ENC_KEY_FILE, identity.TLS_KEY_FILE, identity.CLAIM_CODE_FILE):
        assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o600, name


def test_device_id_is_derived_from_the_signing_key(tmp_path):
    ident = identity.load_or_create(tmp_path)
    der = ident.signing_key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    expected = "shg-" + base64.b32encode(hashlib.sha256(der).digest()).decode()[:16].lower()
    assert ident.device_id == expected and len(ident.device_id) == len(wire.DEVICE_ID_PREFIX) + wire.DEVICE_ID_HASH_CHARS


def test_an_existing_agent_key_keeps_its_device_id(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    (tmp_path / "key.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    assert identity.load_or_create(tmp_path).device_id == identity.device_id_for(key.public_key())


def test_claim_code_is_12_base32_characters():
    code = identity.new_claim_code(lambda n: bytes(range(n)))
    assert len(code) == wire.CLAIM_CODE_CHARS and set(code) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")


def test_a_new_claim_code_is_saved_only_on_request(tmp_path):
    ident = identity.load_or_create(tmp_path)
    neu = identity.save_claim_code(tmp_path, ident, "ABCDEFGHIJKL")
    assert neu.claim_code == "ABCDEFGHIJKL" == identity.load_or_create(tmp_path).claim_code
    assert neu.device_id == ident.device_id


def test_csr_names_the_device_and_uses_the_tls_key(tmp_path):
    ident = identity.load_or_create(tmp_path)
    csr = x509.load_pem_x509_csr(identity.csr_pem(ident).encode())
    assert csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == ident.device_id
    assert csr.is_signature_valid
    assert csr.public_key().public_numbers() == ident.tls_key.public_key().public_numbers()
    assert ident.tls_key.public_key().public_numbers() != ident.signing_key.public_key().public_numbers()
    assert len(identity.csr_pem(ident)) <= wire.CSR_MAX_BYTES


def test_a_certificate_must_match_the_tls_key(tmp_path):
    ident = identity.load_or_create(tmp_path)
    ca = GeraeteCa()
    assert identity.passt_zum_schluessel(ident, zertifikat_fuer(ident, ca))
    other = identity.load_or_create(tmp_path / "anderes")
    assert not identity.passt_zum_schluessel(ident, zertifikat_fuer(other, ca))
    assert not identity.passt_zum_schluessel(ident, "kein PEM")


def test_tls_key_pem_loads_back(tmp_path):
    ident = identity.load_or_create(tmp_path)
    key = serialization.load_pem_private_key(identity.tls_key_pem(ident).encode(), password=None)
    assert key.public_key().public_numbers() == ident.tls_key.public_key().public_numbers()


def test_signature_verifies_with_the_signing_key(tmp_path):
    ident = identity.load_or_create(tmp_path)
    signature = identity.sign(ident.signing_key, "POST", "/devices/register", 5, b"{}")
    der = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    ident.signing_key.public_key().verify(der, wire.canonical_string("POST", "/devices/register", 5, b"{}"),
                                          ec.ECDSA(hashes.SHA256()))


def test_register_body_has_exactly_the_contract_fields(tmp_path):
    ident = identity.load_or_create(tmp_path)
    body = identity.register_body(ident, version="0.6.0", host="gateway",
                                  capabilities={"drivers": ["simulation"], "zigbee": True, "updater": True})
    assert set(body) == set(wire.REGISTER_FIELDS)
    assert body["claim_code_hash"] == wire.claim_code_hash(ident.device_id, ident.claim_code)
    assert csr_passt(body["csr"], ident) and body["host"] == "gateway"


def test_boot_ids_differ_and_fit():
    first, second = identity.new_boot_id(), identity.new_boot_id()
    assert first != second and 0 < len(first) <= 64
