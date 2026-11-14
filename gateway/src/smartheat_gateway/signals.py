"""SignalSource des Gateways: zigbee:<ieee>:<feld> aus dem Zigbee-Spiegel, soll:room_target aus
dem Soll-Speicher, treiber:<rolle> aus dem Cache des Treibers, raum:mittel = Raummittel (room_mean) ueber die
Referenzraum-Fuehler, raum:<n> = Mittel der Fuehler des Raums mit dieser Nummer (Referenz- und Kontrollraeume). Ungueltige Werte werfen ValueError/KeyError wie beim HA-Host; ein getrennter lokaler Bus wirft
SourceUnavailable, ein erschoepftes Kontingent des Treibers QuelleErschoepft; eine unbekannte Referenz SignalNotFound. Gelesen wird nur im Worker-Thread."""
import re

from smartheat_gateway.bus import Bus
from smartheat_gateway.quota import QuotaExhausted
from smartheat_gateway.zigbee import ZigbeeMirror
from smartheat_runtime.ports import QuelleErschoepft, SignalNotFound, SignalUngueltig, SourceUnavailable
from smartheat_runtime.room_mean import room_mean

REF_ROOM_TARGET = "soll:room_target"
REF_ROOM_MEAN = "raum:mittel"
_RAUM = re.compile(r"raum:([0-9]+)")
_ZIGBEE = re.compile(r"zigbee:(0x[0-9a-f]{16}):([a-z_]+)")


class GatewaySignalSource:
    def __init__(self, bus: Bus, mirror: ZigbeeMirror) -> None:
        self._bus = bus
        self._mirror = mirror
        self._driver = None
        self._store = None
        self._room_refs: tuple[str, ...] = ()
        self._raeume: dict[int, tuple[str, ...]] = {}

    def bind(self, driver, store, room_sensor_refs: tuple[str, ...],
             raeume: tuple[tuple[int, tuple[str, ...]], ...] = ()) -> None:
        """`room_sensor_refs`: Fuehler der Referenzraeume (Kreismittel); `raeume`: (Nummer, Fuehler) je Raum."""
        self._driver, self._store, self._room_refs = driver, store, tuple(room_sensor_refs)
        self._raeume = {nummer: tuple(fuehler) for nummer, fuehler in raeume}

    def kontingent_meldung(self) -> str | None:
        """Kundentext, solange das Abfragekontingent des Treibers erschoepft und sein Cache veraltet ist (die Laufzeit
        meldet es, kontingent.check), sonst None."""
        return None if self._driver is None else self._driver.kontingent_meldung()

    def _mittel(self, sensors: tuple[str, ...]) -> float:
        values = []
        for sensor in sensors:
            try:
                values.append(self.get_state(sensor))
            except (ValueError, KeyError, TypeError, SignalNotFound):
                values.append(None)
        mean = room_mean(values)
        if mean is None:
            raise SignalUngueltig("kein Raumfuehler liefert einen gueltigen Wert")
        return mean

    def _raw(self, ref: str):
        if ref == REF_ROOM_TARGET:
            if self._store is None:
                raise SourceUnavailable("Soll-Speicher noch nicht geladen")
            return self._store.value
        if ref == REF_ROOM_MEAN:
            return self._mittel(self._room_refs)
        raum = _RAUM.fullmatch(ref)
        if raum is not None:
            sensors = self._raeume.get(int(raum.group(1)))
            if sensors is None:
                raise SignalNotFound(ref)
            return self._mittel(sensors)
        if ref.startswith("treiber:"):
            if self._driver is None:
                raise SourceUnavailable("Treiber noch nicht geladen")
            try:
                return self._driver.read_signal(ref.removeprefix("treiber:"))
            except QuotaExhausted as error:
                raise QuelleErschoepft(str(error)) from None
        match = _ZIGBEE.fullmatch(ref)
        if match is None:
            raise SignalNotFound(ref)
        if not self._bus.connected:
            raise SourceUnavailable("lokaler Bus getrennt")
        return self._mirror.value(match.group(1), match.group(2))

    def get_state(self, ref: str) -> float:
        value = self._raw(ref)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SignalUngueltig(f"{ref} liefert keine Zahl: {value!r}")
        try:
            return float(value)
        except OverflowError:  # JSON liefert beliebig grosse Ganzzahlen
            raise SignalUngueltig(f"{ref} liegt ausserhalb des Zahlenbereichs") from None

    def get_raw_state(self, ref: str) -> str:
        value = self._raw(ref)
        if isinstance(value, bool):
            return "on" if value else "off"  # wie HA binary_sensor
        return str(value)

    def get_unit(self, ref: str) -> str:
        """Das Gateway hat keine Energiekanaele und damit keine Einheit hinter einer Referenz."""
        raise ValueError("Gateway-Signale haben keine Einheit")

    def device_key(self, ref: str) -> str:
        match = _ZIGBEE.fullmatch(ref)
        return match.group(1) if match else ref
