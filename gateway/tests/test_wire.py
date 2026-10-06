import hashlib
import json
from pathlib import Path

from smartheat_gateway.agent import wire

FIXTURE = Path(__file__).resolve().parents[3] / "tools" / "contracts" / "shg_device_v1.json"


def test_canonical_string_follows_g3_2_1():
    body = b'{"a":1}'
    expected = f"SHG1\nPOST\n/devices/register\n1700000000\n{hashlib.sha256(body).hexdigest()}".encode()
    assert wire.canonical_string("POST", "/devices/register", 1700000000, body) == expected


def test_claim_code_hash():
    assert wire.claim_code_hash("shg-abc", "ABCD") == hashlib.sha256(b"shg-abc:ABCD").hexdigest()


def test_every_command_has_errors_payload_result_and_expiry():
    for kind, command in wire.COMMANDS.items():
        assert set(wire.COMMON_ERRORS) <= set(wire.command_errors(kind)), kind
        assert command.expires_seconds is not None or kind == "driver_inventory"


def test_contract_is_json_and_matches_the_fixture_when_present():
    contract = wire.contract()
    assert json.loads(json.dumps(contract)) == contract
    if FIXTURE.exists():  # im Dev-Layout; die CI des Add-ons prueft ueber den Contract-Check
        assert contract == json.loads(FIXTURE.read_text())
