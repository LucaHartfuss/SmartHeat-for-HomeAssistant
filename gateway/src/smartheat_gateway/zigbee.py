"""Zigbee-Spiegel (Spec SHG G2 5): letzte Werte der Geraete aus Zigbee2MQTT mit Zeitstempel der MONOTONEN Uhr (die
Wanduhr des Pi springt beim Boot, Plan G2a Review Focus 1), Faehigkeiten aus bridge/devices (exposes), Schreiben ans
Thermostat, Koppeln. Friendly Name = IEEE-Adresse. Callbacks laufen im Bus-Thread; der Spiegel haelt nur Daten."""
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

from smartheat_gateway import topics
from smartheat_gateway.bus import Bus, decode

logger = logging.getLogger(__name__)

CAP_TEMPERATURE = "temperature"
CAP_LOCAL_TEMPERATURE = "local_temperature"
CAP_SETPOINT_WRITABLE = "occupied_heating_setpoint_schreibbar"
CAP_BATTERY = "battery"
CAP_BATTERY_LOW = "battery_low"
ACCESS_SET = 2
DEFAULT_MAX_AGE_SECONDS = 2 * 3600


@dataclass(frozen=True)
class ZigbeeDevice:
    ieee: str
    model: str
    vendor: str
    faehigkeiten: frozenset[str]
    art: str  # fuehler | thermostat | sonstig


def _features(exposes):
    for item in exposes or []:
        if not isinstance(item, dict):
            continue
        yield item
        yield from _features(item.get("features"))


def capabilities(exposes) -> frozenset[str]:
    found = set()
    for feature in _features(exposes):
        name = feature.get("property") or feature.get("name")
        access = feature.get("access", 0) if isinstance(feature.get("access"), int) else 0
        if name in (CAP_TEMPERATURE, CAP_LOCAL_TEMPERATURE, CAP_BATTERY, CAP_BATTERY_LOW):
            found.add(name)
        elif name == "occupied_heating_setpoint" and access & ACCESS_SET:
            found.add(CAP_SETPOINT_WRITABLE)
    return frozenset(found)


def _art(faehigkeiten: frozenset[str]) -> str:
    if CAP_SETPOINT_WRITABLE in faehigkeiten:
        return "thermostat"
    if faehigkeiten & {CAP_TEMPERATURE, CAP_LOCAL_TEMPERATURE}:
        return "fuehler"
    return "sonstig"


class ZigbeeMirror:
    def __init__(self, bus: Bus, clock: Callable[[], float], max_age: float = DEFAULT_MAX_AGE_SECONDS) -> None:
        self._bus = bus
        self._clock = clock
        self._max_age = max_age
        self._lock = threading.Lock()
        self._online: bool | None = None
        self._devices: dict[str, ZigbeeDevice] = {}
        self._payloads: dict[str, tuple[dict, float]] = {}
        self._hooks: list[Callable[[str, dict], None]] = []

    def start(self) -> None:
        self._bus.subscribe(f"{topics.Z2M_BASE}/#", self._on_message)

    @property
    def online(self) -> bool | None:
        return self._online

    def on_device_message(self, hook: Callable[[str, dict], None]) -> None:
        self._hooks.append(hook)

    def devices(self) -> list[ZigbeeDevice]:
        with self._lock:
            return sorted(self._devices.values(), key=lambda device: device.ieee)

    def device(self, ieee: str) -> ZigbeeDevice | None:
        with self._lock:
            return self._devices.get(ieee)

    def payload(self, ieee: str) -> dict:
        with self._lock:
            entry = self._payloads.get(ieee)
        return dict(entry[0]) if entry else {}

    def last_seen(self, ieee: str) -> float | None:
        with self._lock:
            entry = self._payloads.get(ieee)
        return entry[1] if entry else None

    def value(self, ieee: str, field: str):
        if self._online is False:
            raise ValueError("Zigbee2MQTT offline")
        with self._lock:
            entry = self._payloads.get(ieee)
        if entry is None:
            raise ValueError(f"Zigbee-Geraet {ieee} hat noch keinen Wert gemeldet")
        payload, seen = entry
        if self._clock() - seen > self._max_age:
            raise ValueError(f"Wert von {ieee} veraltet ({self._clock() - seen:.0f} s)")
        if field not in payload:
            raise ValueError(f"Zigbee-Geraet {ieee} meldet kein Feld {field}")
        return payload[field]

    def write_setpoint(self, ieee: str, value: float) -> None:
        self._bus.publish(f"{topics.Z2M_BASE}/{ieee}/set", {"occupied_heating_setpoint": value})

    def permit_join(self, seconds: int) -> None:
        self._bus.publish(f"{topics.Z2M_BASE}/bridge/request/permit_join", {"time": int(seconds)})

    def _on_message(self, topic: str, raw: bytes, retain: bool) -> None:
        parts = topic.split("/")
        body = decode(raw)
        if parts[1:] == ["bridge", "state"]:
            state = body.get("state") if isinstance(body, dict) else body
            self._online = state == "online" if state is not None else None
        elif parts[1:] == ["bridge", "devices"] and isinstance(body, list):
            self._set_devices(body)
        elif len(parts) == 2 and parts[1] != "bridge" and isinstance(body, dict):
            with self._lock:
                self._payloads[parts[1]] = (body, self._clock())
            for hook in self._hooks:
                hook(parts[1], body)

    def _set_devices(self, raw_devices: list) -> None:
        devices = {}
        for item in raw_devices:
            if not isinstance(item, dict) or item.get("type") == "Coordinator":
                continue
            definition = item.get("definition") or {}
            faehigkeiten = capabilities(definition.get("exposes"))
            ieee = str(item.get("ieee_address", ""))
            devices[ieee] = ZigbeeDevice(
                ieee,
                str(definition.get("model", "")),
                str(definition.get("vendor", "")),
                faehigkeiten,
                _art(faehigkeiten),
            )
        with self._lock:
            self._devices = devices
