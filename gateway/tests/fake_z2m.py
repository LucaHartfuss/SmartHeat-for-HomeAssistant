"""Fake-Zigbee2MQTT (Spec SHG G2 7.2): bridge/state, bridge/devices, Messwerte, Thermostat-set, permit_join, Interview
(bridge/event). Laeuft in Unit-Tests auf FakeBus und in compose.dev bzw. E2E als eigener Prozess auf dem lokalen Bus
(main())."""
import os
import time

from smartheat_gateway.bus import decode

BASE = "zigbee2mqtt"
_TEMPERATURE = {"type": "numeric", "name": "temperature", "property": "temperature", "access": 1, "unit": "°C"}
_BATTERY = {"type": "numeric", "name": "battery", "property": "battery", "access": 1, "unit": "%"}
_BATTERY_LOW = {"type": "binary", "name": "battery_low", "property": "battery_low", "access": 1}
#: Aqara W100 wie Zigbee2MQTT 2.14 (Konverter lumi.ts, Auszug: die Felder, die das Gateway liest oder schreibt)
_W100 = [
    {"type": "binary", "name": "thermostat_mode", "property": "thermostat_mode", "access": 7,
     "value_on": "ON", "value_off": "OFF"},
    {"type": "climate", "features": [
        {"name": "system_mode", "property": "system_mode", "access": 7, "values": ["off", "heat", "cool", "auto"]},
        {"name": "occupied_heating_setpoint", "property": "occupied_heating_setpoint", "access": 7},
        {"name": "local_temperature", "property": "local_temperature", "access": 5},
    ]},
    {"type": "binary", "name": "auto_hide_middle_line", "property": "auto_hide_middle_line", "access": 7},
    _TEMPERATURE, _BATTERY,
]


class FakeZigbee2Mqtt:
    def __init__(self, bus) -> None:
        self._bus = bus
        self._devices: list[dict] = []
        self._sleepy: set[str] = set()
        self._held: dict[str, float] = {}
        self.permit_join_requests: list[int] = []
        self.set_requests: list[tuple[str, dict]] = []  # jede set-Nachricht (IEEE, Inhalt) in Reihenfolge
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

    def add_w100(self, ieee: str) -> None:
        self._add(ieee, "TH-S04D", "Aqara", _W100)

    def add_ohne_definition(self, ieee: str, interview_completed: bool = False) -> None:
        """Geraet, das Zigbee2MQTT schon kennt, dessen Interview aber noch laeuft oder scheiterte: keine Definition."""
        self._devices.append({
            "ieee_address": ieee, "friendly_name": ieee, "type": "EndDevice", "definition": None,
            "interview_completed": interview_completed,
        })
        self._bus.publish(f"{BASE}/bridge/devices", self._devices, retain=True)

    def interview(self, ieee: str, status: str = "successful") -> None:
        """bridge/event device_interview wie Zigbee2MQTT nach dem Koppeln (die Definition nur bei successful)."""
        data = {"friendly_name": ieee, "ieee_address": ieee, "status": status}
        if status == "successful":
            device = next(item for item in self._devices if item["ieee_address"] == ieee)
            data.update(supported=True, definition=device["definition"])
        self._bus.publish(f"{BASE}/bridge/event", {"type": "device_interview", "data": data})

    def make_sleepy(self, ieee: str) -> None:
        """Das Geraet schlaeft: Schreibbefehle bleiben bis wake() liegen (batteriebetriebenes Thermostat)."""
        self._sleepy.add(ieee)

    def wake(self, ieee: str, stale_setpoint: float | None = None) -> None:
        """Aufwachen: zuerst optional der alte Stand (verspaetete Meldung), dann der liegengebliebene Schreibwert.
        Danach schlaeft das Geraet wieder (weitere Schreibbefehle bleiben liegen, bis wake() erneut gerufen wird)."""
        if stale_setpoint is not None:
            self.report(ieee, occupied_heating_setpoint=stale_setpoint, local_temperature=20.0)
        if ieee in self._held:
            self.report(ieee, occupied_heating_setpoint=self._held.pop(ieee), local_temperature=20.0)

    def report(self, ieee: str, **values) -> None:
        self._bus.publish(f"{BASE}/{ieee}", values, retain=True)

    def _add(self, ieee: str, model: str, vendor: str, exposes: list) -> None:
        self._devices.append({
            "ieee_address": ieee, "friendly_name": ieee, "type": "EndDevice", "interview_completed": True,
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
        if isinstance(body, dict):
            self.set_requests.append((ieee, body))
        if not (isinstance(body, dict) and "occupied_heating_setpoint" in body):
            return
        if ieee in self._sleepy:
            self._held[ieee] = body["occupied_heating_setpoint"]
            return
        self.report(ieee, occupied_heating_setpoint=body["occupied_heating_setpoint"], local_temperature=20.0)


def main() -> None:  # laeuft im Container (E2E gateway2), getestet in test_fake_z2m_main
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
    thermostat = os.environ.get("FAKE_Z2M_THERMOSTAT")  # E2E gateway2: Bedienwunsch am Thermostat (Plan 5b W)
    if thermostat:
        z2m.add_thermostat(thermostat)
        z2m.report(thermostat, occupied_heating_setpoint=20.0, local_temperature=temperature, battery_low=False)
    while True:
        z2m.report(sensor, temperature=temperature, battery=90, linkquality=120)
        time.sleep(10)


if __name__ == "__main__":
    main()
