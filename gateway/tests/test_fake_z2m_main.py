"""fake_z2m.main meldet auf Wunsch ein Thermostat (E2E gateway2, Plan 5b W Task 5)."""
import fake_z2m
import pytest
from fakes import FakeBus


def test_main_adds_a_thermostat_when_asked(monkeypatch):
    bus = FakeBus()
    monkeypatch.setenv("FAKE_Z2M_THERMOSTAT", "0x00124b0000000002")
    monkeypatch.setattr("smartheat_gateway.bus.LocalBus", lambda *a, **k: bus)
    monkeypatch.setattr(fake_z2m.time, "sleep", lambda s: (_ for _ in ()).throw(SystemExit(0)))
    with pytest.raises(SystemExit):
        fake_z2m.main()
    devices = [body for topic, body in bus.decoded() if topic == "zigbee2mqtt/bridge/devices"][-1]
    assert "0x00124b0000000002" in {d["ieee_address"] for d in devices}
