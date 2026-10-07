"""Fake-Zigbee2MQTT (Spec SHG G2 7.2): bridge/state, bridge/devices, Messwerte, Thermostat-set, permit_join. Laeuft in
Unit-Tests auf FakeBus und in compose.dev bzw. E2E als eigener Prozess auf dem lokalen Bus (main())."""
import os
import time

from smartheat_gateway.bus import decode

BASE = "zigbee2mqtt"
_TEMPERATURE = {"type": "numeric", "name": "temperature", "property": "temperature", "access": 1, "unit": "°C"}
_BATTERY = {"type": "numeric", "name": "battery", "property": "battery", "access": 1, "unit": "%"}
_BATTERY_LOW = {"type": "binary", "name": "battery_low", "property": "battery_low", "access": 1}


class FakeZigbee2Mqtt:
    def __init__(self, bus) -> None:
        self._bus = bus
        self._devices: list[dict] = []
        self._sleepy: set[str] = set()
        self._held: dict[str, float] = {}
        self.permit_join_requests: list[int] = []
        bus.subscribe(f"{BASE}/bridge/request/permit_join", self._on_permit_join)
        bus.subscribe(f"{BASE}/+/set", self._on_set)

    def bridge(self, online: bool = True) -> None:
        self._bus.publish(f"{BASE}/bridge/state", {"state": "online" if online else "offline"}, retain=True)

    def add_sensor(self, ieee: str, battery: str | None = "prozent") -> None:
        exposes = [_TEMPERATURE] + ([_BATTERY] if battery == "prozent" else [_BATTERY_LOW] if battery == "flag" else [])
        self._add(ieee, "SNZB-02D", "SONOFF", exposes)

    def add_thermostat(self, ieee: str) -> None:
        climate = {"type": "climate", "features": [
            {"name": "occupied_heating_setpoint", "property": "occupied_heating_setpoint", "access": 7},
            {"name": "local_temperature", "property": "local_temperature", "access": 5},
        ]}
        self._add(ieee, "TRV-Test", "Test", [climate, _BATTERY_LOW])

    def make_sleepy(self, ieee: str) -> None:
        """Das Geraet schlaeft: Schreibbefehle bleiben bis wake() liegen (batteriebetriebenes Thermostat)."""
        self._sleepy.add(ieee)

    def wake(self, ieee: str, stale_setpoint: float | None = None) -> None:
        """Aufwachen: zuerst optional der alte Stand (verspaetete Meldung), dann der liegengebliebene Schreibwert."""
        if stale_setpoint is not None:
            self.report(ieee, occupied_heating_setpoint=stale_setpoint, local_temperature=20.0)
        if ieee in self._held:
            self.report(ieee, occupied_heating_setpoint=self._held.pop(ieee), local_temperature=20.0)

    def report(self, ieee: str, **values) -> None:
        self._bus.publish(f"{BASE}/{ieee}", values, retain=True)

    def _add(self, ieee: str, model: str, vendor: str, exposes: list) -> None:
        self._devices.append({
            "ieee_address": ieee, "friendly_name": ieee, "type": "EndDevice",
            "definition": {"model": model, "vendor": vendor, "exposes": exposes},
        })
        self._bus.publish(f"{BASE}/bridge/devices", self._devices, retain=True)

    def _on_permit_join(self, topic, payload, retain) -> None:
        body = decode(payload)
        if isinstance(body, dict):
            self.permit_join_requests.append(int(body.get("time", 0)))
            self._bus.publish(f"{BASE}/bridge/response/permit_join", {"status": "ok", "data": body})

    def _on_set(self, topic, payload, retain) -> None:
        ieee = topic.split("/")[1]
        body = decode(payload)
        if not (isinstance(body, dict) and "occupied_heating_setpoint" in body):
            return
        if ieee in self._sleepy:
            self._held[ieee] = body["occupied_heating_setpoint"]
            return
        self.report(ieee, occupied_heating_setpoint=body["occupied_heating_setpoint"], local_temperature=20.0)


def main() -> None:  # pragma: no cover - laeuft im Container
    from smartheat_gateway.bus import LocalBus, credentials_from_env
    bus = LocalBus(
        os.environ.get("SHG_BUS_HOST", "mosquitto"), int(os.environ.get("SHG_BUS_PORT", "1883")), "fake-z2m",
        credentials=credentials_from_env(),
    )
    bus.start()
    bus.wait_connected()
    z2m = FakeZigbee2Mqtt(bus)
    z2m.bridge(online=True)
    sensor = os.environ.get("FAKE_Z2M_SENSOR", "0x00124b0000000001")
    z2m.add_sensor(sensor)
    temperature = float(os.environ.get("FAKE_Z2M_TEMPERATURE", "20.0"))
    while True:
        z2m.report(sensor, temperature=temperature, battery=90, linkquality=120)
        time.sleep(10)


if __name__ == "__main__":
    main()
