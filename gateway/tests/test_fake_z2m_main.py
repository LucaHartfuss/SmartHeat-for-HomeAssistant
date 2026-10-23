"""fake_z2m.main meldet auf Wunsch ein Thermostat (E2E gateway2)."""
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


def test_main_connects_to_the_bus_port_whatever_the_environment_says(monkeypatch):
    """Der Port ist `bus.BUS_PORT`; `SHG_BUS_PORT` aus der Umgebung gilt nicht mehr."""
    from smartheat_gateway import bus
    monkeypatch.setenv("SHG_BUS_PORT", "1")
    monkeypatch.delenv("SHG_BUS_HOST", raising=False)
    seen = []

    def local_bus(host, port, *args, **kwargs):
        seen.append((host, port))
        return FakeBus()

    monkeypatch.setattr("smartheat_gateway.bus.LocalBus", local_bus)
    monkeypatch.setattr(fake_z2m.time, "sleep", lambda s: (_ for _ in ()).throw(SystemExit(0)))
    with pytest.raises(SystemExit):
        fake_z2m.main()
    assert seen == [("mosquitto", bus.BUS_PORT)]
