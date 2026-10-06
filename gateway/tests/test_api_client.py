import time

import pytest
from fake_device_api import FakeDeviceApi

from smartheat_gateway.agent import identity
from smartheat_gateway.agent.api_client import ApiUnavailable, DeviceApiClient, NotAuthenticated
from smartheat_gateway.paths import Paths

CAPS = {"drivers": ["simulation"], "zigbee": True}


@pytest.fixture
def api():
    server = FakeDeviceApi()
    server.start()
    yield server
    server.stop()


def test_register_commands_result(api, data_dir):
    ident = identity.load_or_create(Paths(data_dir))
    client = DeviceApiClient(api.url, ident)
    assert client.register("0.1.0", CAPS)["device_state"] == "nicht_uebernommen"
    command_id = api.enqueue("diagnostics", {})
    commands = client.commands()["commands"]
    assert [c["command_id"] for c in commands] == [command_id]
    client.result(command_id, True, result={})
    assert api.result_of(command_id)["ok"] is True
    assert client.commands()["commands"] == []


def test_wrong_clock_is_rejected_then_recovers(api, data_dir):
    ident = identity.load_or_create(Paths(data_dir))
    skewed = DeviceApiClient(api.url, ident, now=lambda: 1_000_000.0)  # Pi ohne NTP kurz nach dem Boot
    with pytest.raises(NotAuthenticated):
        skewed.register("0.1.0", CAPS)
    assert DeviceApiClient(api.url, ident).register("0.1.0", CAPS)["device_state"] == "nicht_uebernommen"


def test_same_client_succeeds_once_the_clock_is_synchronised(api, data_dir):
    ident = identity.load_or_create(Paths(data_dir))
    wall = [1_000_000.0]
    client = DeviceApiClient(api.url, ident, now=lambda: wall[0])
    with pytest.raises(NotAuthenticated):
        client.register("0.1.0", CAPS)
    wall[0] = time.time()  # NTP hat synchronisiert
    assert client.register("0.1.0", CAPS)["device_state"] == "nicht_uebernommen"


def test_foreign_key_gets_the_same_401(api, data_dir, tmp_path):
    ident = identity.load_or_create(Paths(data_dir))
    DeviceApiClient(api.url, ident).register("0.1.0", CAPS)
    other = identity.load_or_create(Paths(tmp_path / "other"))
    impostor = DeviceApiClient(api.url, identity.Identity(ident.device_id, other.signing_key, other.enc_key, "X"))
    with pytest.raises(NotAuthenticated):
        impostor.commands()


def test_unreachable_server(data_dir):
    client = DeviceApiClient("http://127.0.0.1:9", identity.load_or_create(Paths(data_dir)), timeout=0.5)
    with pytest.raises(ApiUnavailable):
        client.commands()
