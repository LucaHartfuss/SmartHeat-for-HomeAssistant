import base64
import hashlib
import os
import re
import stat

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from smartheat_gateway.agent import identity, wire
from smartheat_gateway.paths import Paths


def test_first_start_creates_stable_identity(data_dir):
    paths = Paths(data_dir)
    first = identity.load_or_create(paths)
    again = identity.load_or_create(paths)
    assert first.device_id == again.device_id and first.claim_code == again.claim_code
    assert re.fullmatch(r"shg-[a-z2-7]{16}", first.device_id)
    assert re.fullmatch(r"[A-Z2-7]{12}", first.claim_code)
    for name in ("key.pem", "enc_key.pem", "claim_code"):
        assert stat.S_IMODE(os.stat(paths.device_dir / name).st_mode) == 0o600


def test_device_id_known_answer(data_dir):
    key = identity.load_or_create(Paths(data_dir)).signing_key
    der = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    expected = "shg-" + base64.b32encode(hashlib.sha256(der).digest()).decode()[:16].lower()
    assert identity.device_id_for(key.public_key()) == expected


def test_signature_verifies_and_is_unpadded_base64url(data_dir):
    ident = identity.load_or_create(Paths(data_dir))
    signature = identity.sign(ident.signing_key, "GET", "/devices/x/commands", 1700000000, b"")
    assert "=" not in signature and "+" not in signature and "/" not in signature
    der = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    ident.signing_key.public_key().verify(
        der, wire.canonical_string("GET", "/devices/x/commands", 1700000000, b""), ec.ECDSA(hashes.SHA256()),
    )


def test_new_claim_code_keeps_the_keys(data_dir):
    paths = Paths(data_dir)
    first = identity.load_or_create(paths)
    second = identity.replace_claim_code(paths, first)
    assert second.device_id == first.device_id and second.claim_code != first.claim_code
    assert identity.load_or_create(paths).claim_code == second.claim_code
    assert stat.S_IMODE(os.stat(paths.device_dir / "claim_code").st_mode) == 0o600


def test_register_body_carries_only_the_hash(data_dir):
    ident = identity.load_or_create(Paths(data_dir))
    body = identity.register_body(ident, "0.1.0", {"drivers": ["simulation"], "zigbee": True})
    assert tuple(body) == wire.REQUEST_FIELDS["register"]
    assert body["claim_code_hash"] == wire.claim_code_hash(ident.device_id, ident.claim_code)
    assert ident.claim_code not in str(body)
