"""Trigger-Eingang des Gateways (Spec SHG G2 3.4, Vertrag smartheat_runtime.ports.TriggerSource; Spec 5b 3.2/3.3,
Plan D2). Bus-Thread: stellt nur Ereignisse ein. Soll-Eingang: die Bedienung vom Server (GatewayDeviceHost, Quelle
portal) und das Thermostat ueber Zigbee gehen als EV_TARGET_INPUT in den Worker; die Soll-Logik (Soll-Speicher,
Entprellen, Wunsch, Rueckschreiben, Soll-Quelle aus/wieder an) ist hostneutral und liegt in
smartheat_runtime.soll_eingang (SollEingang); das Thermostat ist ihre SollQuelle (_ZigbeeQuelle). Hier bleiben die
Zigbee-Meldungen, die Tagestick-Zeit und der Verbindungs-Trigger. Fuehler-Aenderungen posten EV_LOCAL_CHECK(False); die
Tagestick-Zeit ebenfalls (wie der Zeit-Trigger in HA)."""
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from smartheat_core import wallclock
from smartheat_gateway.bus import Bus
from smartheat_gateway.zigbee import ZigbeeMirror
from smartheat_runtime.debounce import ROOM_TARGET_DEBOUNCE_SECONDS
from smartheat_runtime.runtime import EV_LOCAL_CHECK, EV_SOURCE_CONNECTED
from smartheat_runtime.soll_eingang import EV_SOLL_MODUS, EV_TARGET_INPUT, SollEingang
from smartheat_runtime.soll_speicher import SOURCE_THERMOSTAT, TargetStore
from smartheat_runtime.worker import Event, RegulationWorker

logger = logging.getLogger(__name__)

EV_DAILY = "shg_daily"


def local_zone(now: datetime | None = None) -> tzinfo:
    """Zeitzone fuer den Tagestick: TZ (Compose setzt sie), sonst der Offset von now (fester Offset, ohne
    Sommerzeit-Wechsel)."""
    name = os.environ.get("TZ", "")
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return (now.tzinfo if now is not None else None) or UTC


def seconds_until(daily_time: str, now: datetime, zone: tzinfo | None = None) -> float:
    """Sekunden bis zur naechsten Tageszeit HH:MM in der Ortszeit der Zone; rechnet ueber UTC, damit der Wechsel
    Sommer-/Winterzeit stimmt (Plan G2b-1, Restpunkt 6)."""
    zone = zone or local_zone(now)
    hour, minute = (int(part) for part in daily_time.split(":")[:2])
    local = now.astimezone(zone)
    target = datetime.combine(local.date(), time(hour, minute), tzinfo=zone)
    if target <= local:
        target = datetime.combine(local.date() + timedelta(days=1), time(hour, minute), tzinfo=zone)
    return (target.astimezone(UTC) - now.astimezone(UTC)).total_seconds()


class _ZigbeeQuelle:
    """Das Thermostat ueber Zigbee2MQTT als Soll-Quelle (SollQuelle)."""

    def __init__(self, mirror: ZigbeeMirror, ieee: str) -> None:
        self._mirror, self._ieee = mirror, ieee

    def schreiben(self, value: float) -> bool:
        self._mirror.write_setpoint(self._ieee, value)
        return True

    def angezeigt(self) -> float | None:
        value = self._mirror.payload(self._ieee).get("occupied_heating_setpoint")
        return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


class BusTriggerSource:
    def __init__(self, bus: Bus, worker: RegulationWorker, mirror: ZigbeeMirror, store: TargetStore, *,
                 thermostat: str | None, sensor_ieees: tuple[str, ...], daily_trigger_time: str | None,
                 now=wallclock.now, wunsch: Callable[[float], float] = lambda value: value,
                 soll_quelle_aus: Callable[[bool, float | None], None] = lambda aus, wirksam: None) -> None:
        self._bus, self._worker, self._mirror, self._store = bus, worker, mirror, store
        self._thermostat = thermostat
        self._sensor_ieees = frozenset(sensor_ieees)
        self._daily = daily_trigger_time
        self._now = now
        self._eingang = SollEingang(worker, store, _ZigbeeQuelle(mirror, thermostat) if thermostat is not None else None,
                                    wunsch=lambda value, herkunft, durch: wunsch(value),
                                    soll_quelle_aus=soll_quelle_aus, entprellen_s=ROOM_TARGET_DEBOUNCE_SECONDS)
        worker.register(EV_DAILY, self._on_daily)

    @property
    def connected(self) -> bool:
        return self._bus.connected

    def start(self) -> None:
        self._mirror.on_device_message(self._on_device_message)
        self._bus.on_connected(self._on_connected)
        if self._bus.connected:
            self._on_connected()
        if self._daily:
            self._worker.schedule(seconds_until(self._daily, self._now()), Event(EV_DAILY))

    def stop(self) -> None:
        pass  # der Bus gehoert dem Host

    # --- Bus-Thread ---

    def _on_connected(self) -> None:
        self._worker.post_coalesced(EV_LOCAL_CHECK, room_target_fired=True)
        self._worker.post_coalesced(EV_SOURCE_CONNECTED)

    def _on_device_message(self, ieee: str, payload: dict) -> None:
        if ieee == self._thermostat:
            if "system_mode" in payload:
                self._worker.post(Event(EV_SOLL_MODUS, {"mode": payload["system_mode"]}))
            if "occupied_heating_setpoint" in payload:
                # Jede Meldung geht in den Worker, kein Entprellen hier: der Soll-Speicher verwirft den aktuellen Wert,
                # und eine im Echo-Fenster verworfene Aenderung gilt mit der naechsten Meldung des Thermostats.
                value = payload["occupied_heating_setpoint"]
                self._worker.post(Event(EV_TARGET_INPUT, {"value": value, "source": SOURCE_THERMOSTAT}))
        if ieee in self._sensor_ieees:
            self._worker.post_coalesced(EV_LOCAL_CHECK, room_target_fired=False)

    # --- Worker-Thread ---

    def _on_daily(self, event: Event) -> None:
        self._worker.post_coalesced(EV_LOCAL_CHECK, room_target_fired=False)
        if self._daily:
            self._worker.schedule(seconds_until(self._daily, self._now()) or 86400, Event(EV_DAILY))
