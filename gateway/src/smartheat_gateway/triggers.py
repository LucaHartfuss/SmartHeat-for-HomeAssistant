"""Trigger-Eingang des Gateways (Vertrag smartheat_runtime.ports.TriggerSource).
Bus-Thread: stellt nur Ereignisse ein. Soll-Eingang: die Bedienung vom Server (GatewayDeviceHost, Quelle
portal) und das Thermostat ueber Zigbee gehen als EV_TARGET_INPUT in den Worker; die Soll-Logik (Soll-Speicher,
Entprellen, Wunsch, Rueckschreiben, Soll-Quelle aus/wieder an) ist hostneutral und liegt in
smartheat_runtime.soll_eingang (SollEingang); das Thermostat ist ihre SollQuelle (_ZigbeeQuelle). Hier bleiben die
Zigbee-Meldungen, die Tagestick-Zeit und der Verbindungs-Trigger. Fuehler-Aenderungen posten EV_LOCAL_CHECK(False); die
Tagestick-Zeit ebenfalls (wie der Zeit-Trigger in HA). Nichts, was ueber die Soll-Quelle geht (Angleichen beim Start,
Bedienung des Servers, Rueckschreiben eines Wunschs, wieder an), erreicht ein Thermostat, dessen letzter bekannter
Zustand (auch retained, jedes Alter) aus oder Frostschutz zeigt (Nutzer-Entscheidung E5). Beim Start gilt
dieser Zustand fuer "aus" des Soll-Eingangs, nie als Wunsch; das Angleichen laeuft danach im Worker (EV_ANGLEICHEN),
ohne bekannten Zustand erst mit der ersten Live-Meldung, vor deren Ereignissen. Ein Thermostat mit Profil
(thermostat_profil, Aqara W100) bekommt beim Start Thermostatmodus und Anzeige, das Angleichen erst nach gemeldetem
thermostat_mode ON; nach erneutem Koppeln die Einrichtung (Modus sofort; Soll aus dem Soll-Speicher und heizen
im Worker, sobald das Thermostat thermostat_mode ON gemeldet hat, auch wenn es aus ist: sonst heizte ein neu gekoppeltes
W100 nie); ein fremder Modus (cool, auto) geht zurueck auf heat, eine fremde Aktion setzt Modus und Anzeige neu
(je hoechstens alle PFLEGE_ABSTAND_S); ein ganzzahliges Thermostat bekommt das Soll gerundet, und seine Meldung des
gerundeten Solls gilt im Worker als das geltende Soll."""
import logging
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta, tzinfo
from time import monotonic
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from smartheat_core import wallclock
from smartheat_core.zahlen import ist_endliche_zahl
from smartheat_gateway.bus import Bus
from smartheat_gateway.thermostat_profil import ThermostatProfil, modus_setzen, profil, soll_und_heizen
from smartheat_gateway.zigbee import ZigbeeMirror
from smartheat_runtime.debounce import ROOM_TARGET_DEBOUNCE_SECONDS
from smartheat_runtime.runtime import EV_LOCAL_CHECK, EV_SOURCE_CONNECTED
from smartheat_runtime.soll_eingang import EV_SOLL_MODUS, EV_TARGET_INPUT, FROSTSCHUTZ_BIS, MODUS_AUS, SollEingang
from smartheat_runtime.soll_speicher import SOURCE_THERMOSTAT, TargetStore
from smartheat_runtime.worker import Event, RegulationWorker

logger = logging.getLogger(__name__)

EV_DAILY = "shg_daily"
EV_ZIGBEE_SOLL = "shg_zigbee_soll"  # Meldung eines ganzzahligen Thermostats, wird im Worker abgebildet
EV_THERMOSTAT_EINRICHTEN = "shg_thermostat_einrichten"  # gebundenes Thermostat neu gekoppelt
EV_THERMOSTAT_SOLL = "shg_thermostat_soll"  # ... und hat thermostat_mode ON gemeldet: Soll und heizen
EV_ANGLEICHEN = "shg_angleichen"  # Angleichen beim Start, sobald der Zustand des Thermostats bekannt ist
PFLEGE_ABSTAND_S = 60.0


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
    Sommer-/Winterzeit stimmt (Restpunkt 6). Immer groesser 0: zur ersten Ortszeit der Tageszeit (in der
    doppelten Stunde der Rueckstellung feuert der Tick nur beim ersten Durchlauf), danach erst am Folgetag."""
    zone = zone or local_zone(now)
    hour, minute = (int(part) for part in daily_time.split(":")[:2])
    local = now.astimezone(zone)
    target = datetime.combine(local.date(), time(hour, minute), tzinfo=zone)
    if target.astimezone(UTC) <= now.astimezone(UTC):  # in UTC: gleich gezonte Zeiten verglichen Python naiv (fold-blind)
        target = datetime.combine(local.date() + timedelta(days=1), time(hour, minute), tzinfo=zone)
    return (target.astimezone(UTC) - now.astimezone(UTC)).total_seconds()


def _zustand_bekannt(payload: dict) -> bool:
    """Zeigt die Meldung des Thermostats brauchbar, ob es aus ist: ein Modus (Text) oder ein endliches Soll. Ein null-,
    Text- oder fehlendes Feld macht den Zustand nicht bekannt (fail closed: das Angleichen wartet weiter)."""
    return isinstance(payload.get("system_mode"), str) or ist_endliche_zahl(payload.get("occupied_heating_setpoint"))


class _ZigbeeQuelle:
    """Das Thermostat ueber Zigbee2MQTT als Soll-Quelle (SollQuelle). Ein ganzzahliges Thermostat zeigt das Soll
    gerundet; zeigt es genau den gerundeten Wert, gilt das als das geltende Soll (nur im Worker: liest den Speicher)."""

    def __init__(self, mirror: ZigbeeMirror, ieee: str, store: TargetStore) -> None:
        self._mirror, self._ieee, self._store = mirror, ieee, store

    def profil(self) -> ThermostatProfil | None:
        device = self._mirror.device(self._ieee)
        return profil(device.model) if device is not None else None

    def aus(self) -> bool:
        """Der letzte bekannte Zustand (auch retained, jedes Alter) zeigt system_mode off oder ein Soll bis
        FROSTSCHUTZ_BIS."""
        payload = self._mirror.payload(self._ieee)
        soll = payload.get("occupied_heating_setpoint")
        return payload.get("system_mode") == MODUS_AUS or (ist_endliche_zahl(soll) and soll <= FROSTSCHUTZ_BIS)

    def schreiben(self, value: float) -> bool:
        if self.aus():  # Pruefung vor jedem Schreiben, auch wenn der Soll-Eingang "aus" noch nicht kennt
            logger.info("Thermostat %s zeigt aus oder Frostschutz: Soll nicht geschrieben", self._ieee)
            return False
        p = self.profil()
        self._mirror.write_setpoint(self._ieee, p.anzeige(value) if p is not None else value)
        return True

    def angezeigt(self) -> float | None:
        value = self._mirror.payload(self._ieee).get("occupied_heating_setpoint")
        return float(self.eingang(value)) if ist_endliche_zahl(value) else None

    def eingang(self, value):
        p = self.profil()
        if p is not None and p.ganzzahlig and ist_endliche_zahl(value) and value == p.anzeige(self._store.value):
            return self._store.value
        return value


class BusTriggerSource:
    def __init__(self, bus: Bus, worker: RegulationWorker, mirror: ZigbeeMirror, store: TargetStore, *,
                 thermostat: str | None, sensor_ieees: tuple[str, ...], daily_trigger_time: str | None,
                 now=wallclock.now, wunsch: Callable[[float], float] = lambda value: value,
                 soll_quelle_aus: Callable[[bool, float | None], None] = lambda aus, wirksam: None,
                 angleichen: bool = False, clock: Callable[[], float] = monotonic) -> None:
        """angleichen: beim Start das geltende Soll ans Thermostat, nach Thermostatmodus und Anzeige, sobald
        der Zustand bekannt und nicht aus ist, ein Thermostat mit Profil nach gemeldetem ON."""
        self._bus, self._worker, self._mirror, self._store = bus, worker, mirror, store
        self._thermostat = thermostat
        self._sensor_ieees = frozenset(sensor_ieees)
        self._daily = daily_trigger_time
        self._now = now
        self._clock = clock
        self._angleichen = angleichen
        self._gepflegt: dict[str, float] = {}  # Art der Pflege -> letzte Zeit (nur im Bus-Thread)
        self._lock = threading.Lock()
        self._angleichen_offen = False  # ohne bekannten Zustand: die erste Live-Meldung des Thermostats gleicht an
        self._quelle = _ZigbeeQuelle(mirror, thermostat, store) if thermostat is not None else None
        self._eingang = SollEingang(worker, store, self._quelle,
                                    wunsch=lambda value, herkunft, durch: wunsch(value),
                                    soll_quelle_aus=soll_quelle_aus, entprellen_s=ROOM_TARGET_DEBOUNCE_SECONDS)
        worker.register(EV_DAILY, self._on_daily)
        worker.register(EV_ZIGBEE_SOLL, self._on_zigbee_soll)
        worker.register(EV_THERMOSTAT_EINRICHTEN, self._on_einrichten)
        worker.register(EV_THERMOSTAT_SOLL, self._on_thermostat_soll)
        worker.register(EV_ANGLEICHEN, self._on_angleichen)

    @property
    def connected(self) -> bool:
        return self._bus.connected

    def start(self) -> None:
        self._mirror.on_device_message(self._on_device_message)
        if self._quelle is not None:
            assert self._thermostat is not None
            self._mirror.pflege.binden(self._thermostat,
                                       lambda: self._worker.post(Event(EV_THERMOSTAT_EINRICHTEN)))
            p = self._quelle.profil()
            if p is not None:
                modus_setzen(self._mirror, self._thermostat, p)
            if self._angleichen and p is None:
                with self._lock:  # erst merken, dann pruefen: eine Meldung dazwischen gleicht an oder wird gesehen
                    self._angleichen_offen = True
            zustand = self._mirror.payload(self._thermostat)
            if _zustand_bekannt(zustand):
                self._zustand_melden(zustand)
            if self._angleichen and p is not None:
                self._mirror.pflege.nach_an(self._thermostat, lambda: self._worker.post(Event(EV_ANGLEICHEN)))
            elif _zustand_bekannt(zustand) and self._angleichen_nehmen():
                self._worker.post(Event(EV_ANGLEICHEN))
        self._bus.on_connected(self._on_connected)
        if self._bus.connected:
            self._on_connected()
        if self._daily:
            self._worker.schedule(seconds_until(self._daily, self._now()), Event(EV_DAILY))

    def stop(self) -> None:
        self._mirror.off_device_message(self._on_device_message)
        if self._thermostat is not None:  # der Bus gehoert dem Host
            self._mirror.pflege.loesen(self._thermostat)

    # --- Bus-Thread ---

    def _on_connected(self) -> None:
        self._worker.post_coalesced(EV_LOCAL_CHECK, room_target_fired=True)
        self._worker.post_coalesced(EV_SOURCE_CONNECTED)

    def _zustand_melden(self, zustand: dict) -> None:
        """Beim Start: der letzte bekannte Zustand fuer "aus" des Soll-Eingangs (Modus; ein Soll bis FROSTSCHUTZ_BIS
        setzt dort nur die Frostschutz-Stellung), nie ein Wunsch."""
        if "system_mode" in zustand:
            self._worker.post(Event(EV_SOLL_MODUS, {"mode": zustand["system_mode"]}))
        soll = zustand.get("occupied_heating_setpoint")
        if ist_endliche_zahl(soll) and soll <= FROSTSCHUTZ_BIS:
            self._worker.post(Event(EV_TARGET_INPUT, {"value": soll, "source": SOURCE_THERMOSTAT}))

    def _angleichen_nehmen(self) -> bool:
        with self._lock:
            offen, self._angleichen_offen = self._angleichen_offen, False
        return offen

    def _on_device_message(self, ieee: str, payload: dict) -> None:
        if ieee == self._thermostat:
            assert self._quelle is not None
            if _zustand_bekannt(payload) and self._angleichen_nehmen():
                self._worker.post(Event(EV_ANGLEICHEN))  # vor den Ereignissen der Meldung: ihr Soll ist kein Wunsch
            p = self._quelle.profil()
            if "system_mode" in payload:
                self._worker.post(Event(EV_SOLL_MODUS, {"mode": payload["system_mode"]}))
                if p is not None and payload["system_mode"] in p.fremde_modi and self._pflegen("heizen"):
                    self._mirror.set(ieee, p.heizen)
            action = payload.get("action")
            if p is not None and isinstance(action, str) and action not in p.eigene_aktionen and self._pflegen("modus"):
                logger.info("Thermostat %s sendet Tastenaktionen: Thermostatmodus neu gesetzt", ieee)
                modus_setzen(self._mirror, ieee, p)
            if "occupied_heating_setpoint" in payload:
                # Jede Meldung geht in den Worker, kein Entprellen hier: der Soll-Speicher verwirft den aktuellen Wert,
                # und eine im Echo-Fenster verworfene Aenderung gilt mit der naechsten Meldung des Thermostats.
                value = payload["occupied_heating_setpoint"]
                kind = EV_ZIGBEE_SOLL if p is not None and p.ganzzahlig else EV_TARGET_INPUT
                self._worker.post(Event(kind, {"value": value, "source": SOURCE_THERMOSTAT}))
        if ieee in self._sensor_ieees:
            self._worker.post_coalesced(EV_LOCAL_CHECK, room_target_fired=False)

    def _pflegen(self, art: str) -> bool:
        jetzt, zuletzt = self._clock(), self._gepflegt.get(art)
        if zuletzt is not None and jetzt - zuletzt < PFLEGE_ABSTAND_S:
            return False
        self._gepflegt[art] = jetzt
        return True

    # --- Worker-Thread ---

    def _on_zigbee_soll(self, event: Event) -> None:
        assert self._quelle is not None
        self._worker.post(Event(EV_TARGET_INPUT, {**event.data, "value": self._quelle.eingang(event.data["value"])}))

    def _on_einrichten(self, event: Event) -> None:
        assert self._quelle is not None and self._thermostat is not None
        p = self._quelle.profil()
        if p is None:
            return
        logger.info("Thermostat %s neu gekoppelt: als Thermostat eingestellt", self._thermostat)
        # Modus sofort; Soll und heizen erst nach der Meldung thermostat_mode ON, dann mit dem Soll von dann
        self._mirror.pflege.einrichten(self._thermostat, p,
                                       lambda: self._worker.post(Event(EV_THERMOSTAT_SOLL)))

    def _on_angleichen(self, event: Event) -> None:
        assert self._quelle is not None
        if self._eingang.aus:
            logger.info("Thermostat %s aus oder in Frostschutz-Stellung: kein Angleichen beim Start", self._thermostat)
            return
        angezeigt = self._quelle.angezeigt()
        if self._quelle.schreiben(self._store.value):
            self._store.note_own_write(veraltet=angezeigt)

    def _on_thermostat_soll(self, event: Event) -> None:
        assert self._quelle is not None and self._thermostat is not None
        p = self._quelle.profil()
        if p is None:
            return
        soll_und_heizen(self._mirror, self._thermostat, p, self._store.value)
        self._store.note_own_write()

    def _on_daily(self, event: Event) -> None:
        self._worker.post_coalesced(EV_LOCAL_CHECK, room_target_fired=False)
        if self._daily:
            self._worker.schedule(seconds_until(self._daily, self._now()), Event(EV_DAILY))
