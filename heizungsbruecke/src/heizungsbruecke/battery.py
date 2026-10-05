"""Batterieueberwachung der Raumfuehler/Thermostate (Spec TP6 3.5). Die Batterie-Entities hat der
Wizard ermittelt (Option battery_entities). Prozent-Sensor: < 20 -> niedrig, >= 25 -> ok,
dazwischen bleibt der letzte Zustand. binary_sensor: on -> niedrig, off -> ok. unavailable/
unknown aendert nichts. Gemeldet wird nur beim Wechsel (notifier), nicht kritisch."""
import logging
import math

from heizungsbruecke.notifier import STATE_OK
from smartheat_runtime.ports import SignalNotFound, SourceUnavailable

logger = logging.getLogger(__name__)

LOW_BELOW_PERCENT = 20
RECOVERED_FROM_PERCENT = 25
STATE_LOW = "niedrig"


def next_state(previous: str, entity_id: str, raw: str) -> str:
    if entity_id.startswith("binary_sensor."):
        return {"on": STATE_LOW, "off": STATE_OK}.get(raw, previous)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return previous
    if not math.isfinite(value):
        return previous
    if value < LOW_BELOW_PERCENT:
        return STATE_LOW
    if value >= RECOVERED_FROM_PERCENT:
        return STATE_OK
    return previous


def _message(entity_id: str, state: str, raw: str) -> str:
    if state == STATE_OK:
        return f"SmartHeat: Batterie von {entity_id} wieder in Ordnung."
    level = "" if entity_id.startswith("binary_sensor.") else f" ({raw} %)"
    return f"SmartHeat: Batterie von {entity_id} ist schwach{level}. Bitte bald wechseln."


def check_batteries(rt) -> None:
    for entity_id in rt.config.battery_refs:
        key = f"batterie:{entity_id}"
        try:
            raw = rt.signals.get_raw_state(entity_id)
        except SignalNotFound:
            logger.info("Batterie-Entity %s nicht gefunden (404), wird uebersprungen", entity_id)
            continue
        except SourceUnavailable as error:
            logger.warning("Batteriepruefung abgebrochen, Home Assistant nicht erreichbar: %s", error)
            return
        except ValueError:
            continue
        state = next_state(rt.notifier.state(key), entity_id, raw)
        rt.notifier.notify(key, state, _message(entity_id, state, raw), critical=False)
