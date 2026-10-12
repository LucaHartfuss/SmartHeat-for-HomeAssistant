"""HA-Trigger im Geraete-Pfad (Spec 5c 1.3, Vertrag smartheat_runtime.ports.TriggerSource): ein subscribe_trigger auf
die Soll-Entity (mit for: 10 s, wie bis 0.36), ihren HVAC-Modus (Climate), das Raumfuehler-Mittel, das Ereignis
smartheat_wunsch der Integration und den Tagestick. Der WS-Thread stellt nur Ereignisse in den Worker ein; dort
entscheidet der Soll-Eingang (smartheat_runtime.soll_eingang, entprellen 0 s): Wunsch mit Herkunft (soll_entity bzw.
smartheat_entity) und durch (nutzer/automation aus dem HA-Kontext), Thermostat aus/Frostschutz, Angleichen der
Soll-Entity (nur Climate ist schreibbar) ohne Echo."""
import logging
import math
from typing import TypeGuard

from heizungsbruecke.config import ConfigError
from heizungsbruecke.ha_trigger_client import HaTriggerClient
from smartheat_runtime.debounce import ROOM_TARGET_DEBOUNCE_SECONDS
from smartheat_runtime.runtime import EV_LOCAL_CHECK, EV_SOURCE_CONNECTED
from smartheat_runtime.soll_eingang import EV_SOLL_MODUS, EV_TARGET_INPUT, SollEingang
from smartheat_runtime.soll_speicher import SOURCE_LOKAL, SOURCE_THERMOSTAT, TargetStore
from smartheat_runtime.worker import Event, RegulationWorker

logger = logging.getLogger(__name__)

#: Intern (Worker): ein Bericht der Soll-Entity bzw. das (Nach-)Lesen ihres Zustands.
EV_SOLL_BERICHT = "ha_soll_bericht"
EV_SOLL_LESEN = "ha_soll_lesen"
EV_SOLL_NACHLESEN = "ha_soll_nachlesen"
#: Modi, die kein Wechsel des Thermostats sind (Entity gerade nicht erreichbar): weder aus noch wieder an.
_MODI_OHNE_AUSSAGE = ("unavailable", "unknown")
NACHLESEN_PUFFER_SECONDS = 1.0

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
    """Die Soll-Entity als Soll-Quelle (SollQuelle): schreibbar nur als Climate-Entity (Attribut temperature)."""

    def __init__(self, ha_api, soll_ref: str) -> None:
        self._ha, self._ref = ha_api, soll_ref
        self._entity, _ = _split(soll_ref)

    def schreiben(self, value: float) -> bool:
        if not self._entity.startswith("climate."):
            return False
        try:
            self._ha.set_climate_temperature(self._entity, value)
        except Exception as error:
            logger.warning("Soll-Entity nicht angeglichen (%s)", type(error).__name__)
            return False
        return True

    def angezeigt(self) -> float | None:
        try:
            value = self._ha.get_state(self._ref)
        except Exception:
            return None
        return float(value) if _is_number(value) else None


def build(manifest, options: dict, ha_api, worker: RegulationWorker, store: TargetStore, soll_ref: str | None, *,
          wunsch, soll_quelle_aus) -> HaTriggerClient:
    """soll_ref: die echte Soll-Entity aus der Konfiguration (nicht manifest.refs["room_target"], das ist SOLL_REF).
    Der Bericht der Soll-Entity (EV_SOLL_BERICHT) geht als Eingabe der Soll-Quelle in den Soll-Eingang. Faellt er in
    das Echo-Fenster nach dem eigenen Schreiben, verwirft ihn der Soll-Speicher; HA meldet einen unveraenderten Wert
    nie wieder. Deshalb liest EV_SOLL_NACHLESEN die Entity kurz nach dem Fenster einmal erneut und gibt den Wert
    durch denselben Weg (Pending-/Stale-Logik entscheidet). Bei jeder (Re-)Verbindung liest EV_SOLL_LESEN Modus und Soll
    (Aenderungen waehrend der Trennung, Thermostat schon aus beim Start). Nur HA; das Gateway bleibt unberuehrt."""
    pruefe_soll_ref(soll_ref)
    quelle = HaSollQuelle(ha_api, soll_ref) if soll_ref else None
    SollEingang(worker, store, quelle, wunsch=wunsch, soll_quelle_aus=soll_quelle_aus, entprellen_s=0)
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
    if options.get("daily_trigger_time"):
        triggers.append({"platform": "time", "at": options["daily_trigger_time"]})

    letzter_bericht: list = [None, None]  # (Wert, durch) des zuletzt verworfenen Berichts im Echo-Fenster
    nachlesen_offen = [False]

    def eingabe(wert: float, durch: str | None) -> None:
        worker.post(Event(EV_TARGET_INPUT, {"value": wert, "source": SOURCE_THERMOSTAT, "herkunft": HERKUNFT_SOLL,
                                            "durch": durch}))

    def modus(mode) -> None:
        if isinstance(mode, str) and mode not in _MODI_OHNE_AUSSAGE:
            worker.post(Event(EV_SOLL_MODUS, {"mode": mode}))

    def on_bericht(event: Event) -> None:
        if "mode" in event.data:  # Modus und Soll gehen durch dieselbe Warteschlange: Reihenfolge der Meldungen bleibt
            modus(event.data["mode"])
            return
        wert, durch = event.data["value"], event.data["durch"]
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
        if wert is not None:
            eingabe(wert, None)

    worker.register(EV_SOLL_BERICHT, on_bericht)
    worker.register(EV_SOLL_NACHLESEN, on_nachlesen)
    worker.register(EV_SOLL_LESEN, on_lesen)

    def on_soll_entity(trigger: dict) -> bool:
        """True, wenn der Trigger die Soll-Entity betraf (Wunsch oder Modus); ein anderes Attribut derselben Entity
        (etwa current_temperature als Raumfuehler) ist ein lokaler Check."""
        attribute = trigger.get("attribute") or None
        to_state = trigger.get("to_state") or {}
        if attribute == soll_attribute:
            value = (to_state.get("attributes") or {}).get(attribute) if attribute else to_state.get("state")
            number = _zustandswert(value)
            if number is not None:
                worker.post(Event(EV_SOLL_BERICHT, {"value": number,
                                                    "durch": durch_aus_kontext(to_state.get("context"))}))
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

    return HaTriggerClient(ws_url=ha_api.websocket_url(), token=ha_api.token, triggers=triggers,
                           on_trigger_event=on_trigger, on_connected=on_connected)
