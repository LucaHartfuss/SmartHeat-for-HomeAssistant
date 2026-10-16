"""HA-Trigger im Geraete-Pfad (Spec 5c 1.3, Vertrag smartheat_runtime.ports.TriggerSource): ein subscribe_trigger auf
die Soll-Entity (mit for: 10 s, wie bis 0.36), ihren HVAC-Modus (Climate), das Raumfuehler-Mittel, das Ereignis
smartheat_wunsch der Integration und den Tagestick. Der WS-Thread stellt nur Ereignisse in den Worker ein; dort
entscheidet der Soll-Eingang (smartheat_runtime.soll_eingang, entprellen 0 s): Wunsch mit Herkunft (soll_entity bzw.
smartheat_entity) und durch (nutzer/automation aus dem HA-Kontext), Thermostat aus/Frostschutz, Angleichen der
Soll-Entity (nur Climate ist schreibbar) ohne Echo."""
import logging
import math
import time
from typing import TypeGuard

from heizungsbruecke.ha_trigger_client import HaTriggerClient
from smartheat_runtime.debounce import ROOM_TARGET_DEBOUNCE_SECONDS
from smartheat_runtime.dokument_config import ConfigError
from smartheat_runtime.runtime import EV_LOCAL_CHECK, EV_SOURCE_CONNECTED
from smartheat_runtime.soll_eingang import EV_SOLL_MODUS, EV_TARGET_INPUT, FROSTSCHUTZ_BIS, SollEingang
from smartheat_runtime.soll_speicher import SOURCE_LOKAL, SOURCE_THERMOSTAT, TargetStore
from smartheat_runtime.worker import Event, RegulationWorker

logger = logging.getLogger(__name__)

#: Intern (Worker): ein Bericht der Soll-Entity bzw. das (Nach-)Lesen ihres Zustands.
EV_SOLL_BERICHT = "ha_soll_bericht"
EV_SOLL_LESEN = "ha_soll_lesen"
EV_SOLL_NACHLESEN = "ha_soll_nachlesen"
#: Intern: ein gezeigter Wert, waehrend eine gescheiterte Angleichung offen ist (build: Gescheiterte Angleichung).
EV_SOLL_NACHHOLEN = "ha_soll_nachholen"
#: Ersatz fuer den Trigger, solange der WebSocket getrennt ist (wie der Watchdog der Laufzeit): im Takt des lokalen Checks
#: Modus und Soll der Soll-Entity lesen.
EV_SOLL_WACHE = "ha_soll_wache"
#: Modi, die kein Wechsel des Thermostats sind (Entity gerade nicht erreichbar): weder aus noch wieder an.
_MODI_OHNE_AUSSAGE = ("unavailable", "unknown")
NACHLESEN_PUFFER_SECONDS = 1.0
#: Bericht der Soll-Entity ohne Zahl (unavailable): hoechstens so oft eine Info-Zeile, und nur nach einer Zahl dazwischen.
OHNE_ZAHL_LOG_SECONDS = 3600.0

EREIGNIS_WUNSCH = "smartheat_wunsch"
HERKUNFT_SOLL = "soll_entity"
HERKUNFT_SMARTHEAT = "smartheat_entity"


def durch_aus_kontext(context) -> str | None:
    """HA-Kontext des Ausloesers: Benutzer gesetzt = nutzer, sonst uebergeordneter Ausloeser = automation."""
    if not isinstance(context, dict):
        return None
    if context.get("user_id"):
        return "nutzer"
    if context.get("parent_id"):
        return "automation"
    return None


def _split(ref: str) -> tuple[str, str | None]:
    """`climate.wz::temperature` -> (`climate.wz`, `temperature`): ein Trigger braucht die echte Entity-ID,
    `::attribut` ist eine Konvention dieses Add-ons (siehe ha_api.get_state)."""
    entity_id, _, attribute = ref.partition("::")
    return entity_id, attribute or None


def _is_number(value) -> TypeGuard[int | float]:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _zustandswert(value) -> float | None:
    """Zahl aus einem Zustand (Text) oder Attribut (Zahl); unavailable, unknown und Unlesbares sind None."""
    if _is_number(value):
        return float(value)
    try:
        return float(value) if isinstance(value, str) else None
    except ValueError:
        return None


def pruefe_soll_ref(soll_ref: str | None) -> None:
    """Eine Climate-Soll-Entity braucht das Attribut (`climate.x::temperature`): ohne waere der Zustand der HVAC-Modus,
    kein Soll. Der Server sendet das nie (room_target ist climate::temperature)."""
    if soll_ref and soll_ref.startswith("climate.") and _split(soll_ref)[1] is None:
        raise ConfigError(f"Soll-Entity {soll_ref} ohne Attribut: bei einer Climate-Entity ist "
                          f"{soll_ref}::temperature erforderlich")


def _wunschwert(value) -> float | None:
    """raum_soll aus dem Ereignis smartheat_wunsch: eine endliche Zahl oder ein Text, der eine ist (HA-YAML sendet
    oft Text, etwa \"22.5\"); alles andere None."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


class HaSollQuelle:
    """Die Soll-Entity als Soll-Quelle (SollQuelle): schreibbar nur als Climate-Entity (Attribut temperature).
    angleichen_offen: das letzte Schreiben an die Climate-Entity ist gescheitert (HA Core startet, HA lehnt ab), die
    Entity zeigt also nicht das geltende Soll; bis zum naechsten gelungenen Schreiben holt build() die Angleichung nach,
    statt einen gezeigten Wert als Eingabe zu nehmen (Final-Review I1). Nur Worker-Thread (und der Start davor)."""

    def __init__(self, ha_api, soll_ref: str) -> None:
        self._ha, self._ref = ha_api, soll_ref
        self._entity, _ = _split(soll_ref)
        self.angleichen_offen = False

    def schreiben(self, value: float) -> bool:
        if not self._entity.startswith("climate."):
            return False
        try:
            self._ha.set_climate_temperature(self._entity, value)
        except Exception as error:
            logger.warning("Soll-Entity nicht angeglichen (%s)", type(error).__name__)
            self.angleichen_offen = True
            return False
        self.angleichen_offen = False
        return True

    def angezeigt(self) -> float | None:
        try:
            value = self._ha.get_state(self._ref)
        except Exception:
            return None
        return float(value) if _is_number(value) else None


def build(manifest, ha_api, worker: RegulationWorker, store: TargetStore, soll_ref: str | None, *,
          tagestick: str | None, wache_s: float, wunsch, soll_quelle_aus,
          quelle: HaSollQuelle | None = None) -> HaTriggerClient:
    """soll_ref: die echte Soll-Entity aus der Konfiguration (nicht manifest.refs["room_target"], das ist SOLL_REF).
    tagestick: Uhrzeit des taeglichen Ticks (HH:MM) oder None; wache_s: Takt des lokalen Checks (RuntimeConfig).
    Der Bericht der Soll-Entity (EV_SOLL_BERICHT) geht als Eingabe der Soll-Quelle in den Soll-Eingang. Faellt er in
    das Echo-Fenster nach dem eigenen Schreiben, verwirft ihn der Soll-Speicher; HA meldet einen unveraenderten Wert
    nie wieder. Deshalb liest EV_SOLL_NACHLESEN die Entity kurz nach dem Fenster einmal erneut und gibt den Wert
    durch denselben Weg (Pending-/Stale-Logik entscheidet). Bei jeder (Re-)Verbindung liest EV_SOLL_LESEN Modus und Soll
    (Aenderungen waehrend der Trennung, Thermostat schon aus beim Start). Solange der WebSocket getrennt ist, liest
    EV_SOLL_WACHE im Takt des lokalen Checks dasselbe (bis 0.36 las der Watchdog den Raum-Soll direkt aus HA; jetzt kommt
    er nur ueber den Soll-Eingang in den Soll-Speicher). Nur HA; das Gateway bleibt unberuehrt.

    Gescheiterte Angleichung (quelle.angleichen_offen: Bedienung vom Server, Angleichen beim Start, Zurueckschreiben
    eines Wunschs): jeder gezeigte Wert (Bericht, Nachlesen, Lesen beim Verbinden und in der Wache; die Wache liest dann
    auch bei verbundenem WebSocket) holt sie nach (EV_SOLL_NACHHOLEN), statt Eingabe zu werden: das geltende Soll an
    die Entity, der gezeigte Wert gilt als veraltet. Erst nach dem gelungenen Schreiben zaehlt eine Bedienung an der
    Entity wieder als Wunsch; eine Drehung am Thermostat waehrend desselben Ausfalls verliert gegen die neuere
    Bedienung. Ausnahme Soll-Quelle aus (Modus off, Frostschutz-Stellung): der Wert ist Eingabe wie bisher, beim
    Wiedereinschalten gleicht der Soll-Eingang an. quelle: die Soll-Quelle, die auch das Angleichen beim Start nutzt
    (host.HaHost); ohne baut build sie selbst."""
    pruefe_soll_ref(soll_ref)
    if quelle is None and soll_ref:
        quelle = HaSollQuelle(ha_api, soll_ref)
    eingang = SollEingang(worker, store, quelle, wunsch=wunsch, soll_quelle_aus=soll_quelle_aus, entprellen_s=0)
    soll_entity, soll_attribute = _split(soll_ref) if soll_ref else (None, None)
    climate = soll_entity is not None and soll_entity.startswith("climate.")
    triggers: list[dict] = []
    if soll_entity is not None:
        trigger = {"platform": "state", "entity_id": soll_entity}
        if soll_attribute:
            trigger["attribute"] = soll_attribute
        triggers.append({**trigger, "for": {"seconds": ROOM_TARGET_DEBOUNCE_SECONDS}})
        if climate:
            # HVAC-Modus: nur Zustandswechsel (to: null), nicht jede Attributaenderung des Thermostats.
            triggers.append({"platform": "state", "entity_id": soll_entity, "to": None})
    room_actual = manifest.refs.get("room_actual")
    if room_actual:
        entity_id, attribute = _split(room_actual)
        trigger = {"platform": "state", "entity_id": entity_id}
        if attribute:
            trigger["attribute"] = attribute
        triggers.append(trigger)
    triggers.append({"platform": "event", "event_type": EREIGNIS_WUNSCH})
    if tagestick:
        triggers.append({"platform": "time", "at": tagestick})

    letzter_bericht: list = [None, None]  # (Wert, durch) des zuletzt verworfenen Berichts im Echo-Fenster
    nachlesen_offen = [False]

    def eingabe(wert: float, durch: str | None) -> None:
        worker.post(Event(EV_TARGET_INPUT, {"value": wert, "source": SOURCE_THERMOSTAT, "herkunft": HERKUNFT_SOLL,
                                            "durch": durch}))

    def gezeigt(wert: float, durch: str | None) -> bool:
        """Ein gezeigter Wert der Soll-Entity; True, wenn eine offene Angleichung ihn uebernimmt (dann in der
        Warteschlange nach einem schon gemeldeten Modus), sonst False (der Aufrufer nimmt ihn als Eingabe)."""
        if quelle is None or not quelle.angleichen_offen:
            return False
        worker.post(Event(EV_SOLL_NACHHOLEN, {"value": wert, "durch": durch}))
        return True

    def on_nachholen(event: Event) -> None:
        wert, durch = event.data["value"], event.data["durch"]
        assert quelle is not None  # nur aus gezeigt()
        if not quelle.angleichen_offen:  # inzwischen angeglichen: der Wert ist eine Eingabe wie jeder andere
            eingabe(wert, durch)
            return
        if eingang.aus or wert <= FROSTSCHUTZ_BIS:  # Soll-Quelle aus: wie bisher, der Soll-Eingang gleicht beim
            quelle.angleichen_offen = False  # Wiedereinschalten an; Frostschutz bleibt unangetastet (Spec 5b 3.2)
            eingabe(wert, durch)
            return
        if wert == store.value:  # zeigt das Soll doch (HA hat es trotz Fehler uebernommen)
            quelle.angleichen_offen = False
            return
        if quelle.schreiben(store.value):
            store.note_own_write(veraltet=wert)

    def modus(mode) -> None:
        if isinstance(mode, str) and mode not in _MODI_OHNE_AUSSAGE:
            worker.post(Event(EV_SOLL_MODUS, {"mode": mode}))

    def on_bericht(event: Event) -> None:
        if "mode" in event.data:  # Modus und Soll gehen durch dieselbe Warteschlange: Reihenfolge der Meldungen bleibt
            modus(event.data["mode"])
            return
        wert, durch = event.data["value"], event.data["durch"]
        if gezeigt(wert, durch):
            return
        eingabe(wert, durch)
        rest = store.echo_rest()
        if rest > 0:
            letzter_bericht[:] = [wert, durch]
            if not nachlesen_offen[0]:
                nachlesen_offen[0] = True
                worker.schedule(rest + NACHLESEN_PUFFER_SECONDS, Event(EV_SOLL_NACHLESEN))

    def on_nachlesen(event: Event) -> None:
        rest = store.echo_rest()
        if rest > 0:  # inzwischen ein neues eigenes Schreiben: nach dessen Fenster
            worker.schedule(rest + NACHLESEN_PUFFER_SECONDS, Event(EV_SOLL_NACHLESEN))
            return
        nachlesen_offen[0] = False
        wert = quelle.angezeigt() if quelle is not None else None
        if wert is not None:
            durch = letzter_bericht[1] if letzter_bericht[0] == wert else None
            if not gezeigt(wert, durch):
                eingabe(wert, durch)
        letzter_bericht[:] = [None, None]

    def on_lesen(event: Event) -> None:
        if soll_entity is None or quelle is None:
            return
        if climate:
            try:
                modus(ha_api.get_raw_state(soll_entity))
            except Exception as error:
                logger.warning("HVAC-Modus der Soll-Entity nicht gelesen (%s)", type(error).__name__)
        wert = quelle.angezeigt()
        if wert is not None and not gezeigt(wert, None):
            eingabe(wert, None)

    worker.register(EV_SOLL_BERICHT, on_bericht)
    worker.register(EV_SOLL_NACHHOLEN, on_nachholen)
    worker.register(EV_SOLL_NACHLESEN, on_nachlesen)
    worker.register(EV_SOLL_LESEN, on_lesen)

    ohne_zahl: list = [False, None]  # (gerade ohne Zahl, Zeitpunkt der letzten Info-Zeile); nur im WS-Thread

    def _ohne_zahl_melden() -> None:
        """Eine nicht verfuegbare Soll-Entity bleibt sichtbar: Info-Zeile beim Wechsel von Zahl zu keiner Zahl, hoechstens
        alle OHNE_ZAHL_LOG_SECONDS; ohne den gemeldeten Inhalt."""
        jetzt = time.monotonic()
        if not ohne_zahl[0] and (ohne_zahl[1] is None or jetzt - ohne_zahl[1] >= OHNE_ZAHL_LOG_SECONDS):
            logger.info("Soll-Entity %s meldet keinen Zahlenwert (etwa nicht verfuegbar), kein Bedienwunsch", soll_entity)
            ohne_zahl[1] = jetzt
        ohne_zahl[0] = True

    def on_soll_entity(trigger: dict) -> bool:
        """True, wenn der Trigger die Soll-Entity betraf (Wunsch oder Modus); ein anderes Attribut derselben Entity
        (etwa current_temperature als Raumfuehler) ist ein lokaler Check."""
        attribute = trigger.get("attribute") or None
        to_state = trigger.get("to_state") or {}
        if attribute == soll_attribute:
            value = (to_state.get("attributes") or {}).get(attribute) if attribute else to_state.get("state")
            number = _zustandswert(value)
            if number is not None:
                ohne_zahl[0] = False
                worker.post(Event(EV_SOLL_BERICHT, {"value": number,
                                                    "durch": durch_aus_kontext(to_state.get("context"))}))
            else:
                _ohne_zahl_melden()
            return True
        if attribute is None and climate:
            worker.post(Event(EV_SOLL_BERICHT, {"mode": to_state.get("state")}))
            return True
        return False

    def on_trigger(trigger: dict) -> None:
        platform = trigger.get("platform")
        if platform == "event":
            event = trigger.get("event") or {}
            value = (event.get("data") or {}).get("raum_soll")
            number = _wunschwert(value)
            if number is None:
                logger.warning("Ereignis %s ohne gueltige raum_soll (Typ %s) verworfen", EREIGNIS_WUNSCH,
                               type(value).__name__)
                return
            worker.post(Event(EV_TARGET_INPUT, {"value": number, "source": SOURCE_LOKAL,
                                                "herkunft": HERKUNFT_SMARTHEAT,
                                                "durch": durch_aus_kontext(event.get("context"))}))
            return
        if platform == "state" and soll_entity is not None and trigger.get("entity_id") == soll_entity \
                and on_soll_entity(trigger):
            return
        worker.post_coalesced(EV_LOCAL_CHECK, room_target_fired=False)

    def on_connected() -> None:
        # Bei jeder (Re-)Verbindung, auch der ersten: Soll und HVAC-Modus der Soll-Entity lesen (eine Aenderung
        # waehrend der Trennung, etwa eines HA-Neustarts, meldet HA nicht nach), den lokalen Check mit frischem
        # room_target-Flag und den vollen Status neu senden (nach einem HA-Neustart hat die Integration ihn nicht mehr).
        worker.post(Event(EV_SOLL_LESEN))
        worker.post_coalesced(EV_LOCAL_CHECK, room_target_fired=True)
        worker.post_coalesced(EV_SOURCE_CONNECTED)

    client = HaTriggerClient(ws_url=ha_api.websocket_url(), token=ha_api.token, triggers=triggers,
                             on_trigger_event=on_trigger, on_connected=on_connected)

    def on_wache(event: Event) -> None:
        worker.schedule(wache_s, Event(EV_SOLL_WACHE))  # zuerst: der Takt darf nie abreissen
        if not client.connected or (quelle is not None and quelle.angleichen_offen):
            on_lesen(event)

    worker.register(EV_SOLL_WACHE, on_wache)
    worker.schedule(wache_s, Event(EV_SOLL_WACHE))
    return client
