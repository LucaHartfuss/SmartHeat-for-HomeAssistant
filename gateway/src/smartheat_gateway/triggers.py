"""Trigger-Eingang des Gateways (Spec SHG G2 3.4, Vertrag smartheat_runtime.ports.TriggerSource; Spec 5b 3.2/3.3,
Plan D2). Bus-Thread: stellt nur Ereignisse ein. Soll-Eingang: die Bedienung vom Server (GatewayDeviceHost, Quelle
portal) und das Thermostat ueber Zigbee gehen als EV_TARGET_INPUT in den Worker; dort schreibt der Soll-Speicher, und
der Debouncer (10 s) beendet die Eingabe. Ein Thermostat-Wunsch geht erst dann, und nur einmal, an den Geraetekern
(wunsch: begrenzt auf den Bedienbereich); der begrenzte Wert gilt sofort und wird ans Thermostat zurueckgeschrieben,
danach EV_LOCAL_CHECK(room_target_fired=True). Soll-Quelle aus (system_mode off oder ein Soll bis FROSTSCHUTZ_BIS, Spec
3.2): kein Wunsch, Meldung ueber soll_quelle_aus, eine neue Bedienung schreibt das Thermostat nicht. Fuehler-Aenderungen
posten EV_LOCAL_CHECK(False); die Tagestick-Zeit ebenfalls (wie der Zeit-Trigger in HA)."""
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from smartheat_core import wallclock
from smartheat_gateway import topics
from smartheat_gateway.bus import Bus, decode
from smartheat_gateway.target_store import SOURCE_PORTAL, SOURCE_THERMOSTAT, TargetStore
from smartheat_gateway.zigbee import ZigbeeMirror
from smartheat_runtime.debounce import ROOM_TARGET_DEBOUNCE_SECONDS, Debouncer
from smartheat_runtime.runtime import EV_LOCAL_CHECK, EV_SOURCE_CONNECTED
from smartheat_runtime.worker import Event, RegulationWorker

logger = logging.getLogger(__name__)

EV_TARGET_INPUT = "shg_target_input"
EV_DAILY = "shg_daily"
EV_SOLL_MODUS = "shg_soll_modus"
DEBOUNCE_KIND = "shg_target_debounce"
#: Spec 5b 3.2: ein Soll bis zu diesem Wert ist die Frostschutz-Stellung des Thermostats, kein Bedienwunsch.
FROSTSCHUTZ_BIS = 7.0
MODUS_AUS = "off"


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
        self._wunsch, self._soll_quelle_aus = wunsch, soll_quelle_aus
        self._aus: set[str] = set()  # Gruende: "modus", "frost"
        self._wunsch_offen = False
        self._debouncer = Debouncer(worker, DEBOUNCE_KIND, ROOM_TARGET_DEBOUNCE_SECONDS, self._entprellt)
        worker.register(EV_TARGET_INPUT, self._on_target_input)
        worker.register(EV_SOLL_MODUS, self._on_mode)
        worker.register(EV_DAILY, self._on_daily)

    @property
    def connected(self) -> bool:
        return self._bus.connected

    def start(self) -> None:
        self._mirror.on_device_message(self._on_device_message)
        self._bus.subscribe(topics.CMD_ROOM_TARGET, self._on_room_target_command)
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

    def _on_room_target_command(self, topic: str, raw: bytes, retain: bool) -> None:
        body = decode(raw)
        if retain or not isinstance(body, dict) or "value" not in body:
            return
        self._worker.post(Event(EV_TARGET_INPUT, {"value": body["value"], "source": SOURCE_PORTAL}))

    # --- Worker-Thread ---

    def _setze_aus(self, grund: str, aus: bool) -> None:
        vorher = bool(self._aus)
        if aus:
            self._aus.add(grund)
        else:
            self._aus.discard(grund)
        if bool(self._aus) != vorher:
            self._soll_quelle_aus(bool(self._aus), self._store.value if self._aus else None)

    def _on_mode(self, event: Event) -> None:
        self._setze_aus("modus", event.data.get("mode") == MODUS_AUS)

    def _on_target_input(self, event: Event) -> None:
        value, source = event.data["value"], event.data["source"]
        if source == SOURCE_PORTAL:
            vorher = self._store.value
            if not self._store.apply_portal(value):
                return
            self._wunsch_offen = False  # die Bedienung des Servers gilt; ein halber Wunsch geht nicht mehr hoch
            if self._thermostat is not None and not self._aus and self._store.value != vorher:
                self._mirror.write_setpoint(self._thermostat, self._store.value)
                self._store.note_own_write()
        else:
            if isinstance(value, int | float) and not isinstance(value, bool) and value <= FROSTSCHUTZ_BIS:
                self._setze_aus("frost", True)
                return
            self._setze_aus("frost", False)
            if self._aus or not self._store.apply_thermostat(value):
                return
            self._wunsch_offen = True
        self._debouncer.poke()

    def _entprellt(self) -> None:
        if self._wunsch_offen:
            self._wunsch_offen = False
            try:
                begrenzt = self._wunsch(self._store.value)
            except ValueError:
                begrenzt = self._store.value
            if begrenzt != self._store.value:
                self._store.begrenzen(begrenzt)
                if self._thermostat is not None:
                    self._mirror.write_setpoint(self._thermostat, begrenzt)
                    self._store.note_own_write()
        self._worker.post_coalesced(EV_LOCAL_CHECK, room_target_fired=True)

    def _on_daily(self, event: Event) -> None:
        self._worker.post_coalesced(EV_LOCAL_CHECK, room_target_fired=False)
        if self._daily:
            self._worker.schedule(seconds_until(self._daily, self._now()) or 86400, Event(EV_DAILY))
