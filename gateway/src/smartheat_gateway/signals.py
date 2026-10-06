"""SignalSource des Gateways (Spec SHG G2 3.2): zigbee:<ieee>:<feld> aus dem Zigbee-Spiegel, soll:room_target aus
dem Soll-Speicher, treiber:<rolle> aus dem Cache des Treibers, raum:mittel = Raummittel (room_mean) ueber die
Referenzraum-Fuehler. Ungueltige Werte werfen ValueError/KeyError wie beim HA-Host; ein getrennter lokaler Bus wirft
SourceUnavailable; eine unbekannte Referenz SignalNotFound. Gelesen wird nur im Worker-Thread."""
import re

from smartheat_gateway.bus import Bus
from smartheat_gateway.zigbee import ZigbeeMirror
from smartheat_runtime.ports import SignalNotFound, SourceUnavailable
from smartheat_runtime.room_mean import room_mean

REF_ROOM_TARGET = "soll:room_target"
REF_ROOM_MEAN = "raum:mittel"
_ZIGBEE = re.compile(r"zigbee:(0x[0-9a-f]{16}):([a-z_]+)")


class GatewaySignalSource:
    def __init__(self, bus: Bus, mirror: ZigbeeMirror) -> None:
        self._bus = bus
        self._mirror = mirror
        self._driver = None
        self._store = None
        self._room_refs: tuple[str, ...] = ()

    def bind(self, driver, store, room_sensor_refs: tuple[str, ...]) -> None:
        self._driver, self._store, self._room_refs = driver, store, tuple(room_sensor_refs)

    def _raw(self, ref: str):
        if ref == REF_ROOM_TARGET:
            if self._store is None:
                raise SourceUnavailable("Soll-Speicher noch nicht geladen")
            return self._store.value
        if ref == REF_ROOM_MEAN:
            values = []
            for sensor in self._room_refs:
                try:
                    values.append(self.get_state(sensor))
                except (ValueError, KeyError, TypeError, SignalNotFound):
                    values.append(None)
            mean = room_mean(values)
            if mean is None:
                raise ValueError("kein Raumfuehler liefert einen gueltigen Wert")
            return mean
        if ref.startswith("treiber:"):
            if self._driver is None:
                raise SourceUnavailable("Treiber noch nicht geladen")
            return self._driver.read_signal(ref.removeprefix("treiber:"))
        match = _ZIGBEE.fullmatch(ref)
        if match is None:
            raise SignalNotFound(ref)
        if not self._bus.connected:
            raise SourceUnavailable("lokaler Bus getrennt")
        return self._mirror.value(match.group(1), match.group(2))

    def get_state(self, ref: str) -> float:
        value = self._raw(ref)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{ref} liefert keine Zahl: {value!r}")
        return float(value)

    def get_raw_state(self, ref: str) -> str:
        value = self._raw(ref)
        if isinstance(value, bool):
            return "on" if value else "off"  # wie HA binary_sensor (Plan G2a Review Focus 2)
        return str(value)

    def device_key(self, ref: str) -> str:
        match = _ZIGBEE.fullmatch(ref)
        return match.group(1) if match else ref
