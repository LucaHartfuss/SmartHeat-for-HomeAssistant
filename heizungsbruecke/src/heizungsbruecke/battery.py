"""Batterieueberwachung der Raumfuehler/Thermostate (Spec TP6 3.5). Die Batterie-Entities hat der
Wizard ermittelt (Option battery_entities). Prozent-Sensor: < 20 -> niedrig, >= 25 -> ok,
dazwischen bleibt der letzte Zustand. binary_sensor: on -> niedrig, off -> ok. unavailable/
unknown aendert nichts. Gemeldet wird nur beim Wechsel (notifier), nicht kritisch."""
import logging
import math

import requests

from heizungsbruecke.notifier import STATE_OK

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
    for entity_id in rt.options.get("battery_entities", []):
        key = f"batterie:{entity_id}"
        try:
            raw = rt.ha_api.get_raw_state(entity_id)
        except requests.HTTPError as error:
            if error.response is not None and error.response.status_code == 404:
                logger.info("Batterie-Entity %s nicht gefunden (404), wird uebersprungen", entity_id)
                continue
            logger.warning("Batteriepruefung abgebrochen, Home Assistant nicht erreichbar: %s", error)
            return
        except requests.RequestException as error:
            logger.warning("Batteriepruefung abgebrochen, Home Assistant nicht erreichbar: %s", error)
            return
        except ValueError:
            continue
        state = next_state(rt.notifier.state(key), entity_id, raw)
        rt.notifier.notify(key, state, _message(entity_id, state, raw), critical=False)
