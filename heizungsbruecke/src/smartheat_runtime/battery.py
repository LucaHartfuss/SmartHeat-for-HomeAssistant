"""Batterieueberwachung der Raumfuehler/Thermostate (Spec TP6 3.5). Die Batterie-Entities hat der
Wizard ermittelt (Option battery_entities). Prozent-Sensor: < 20 -> niedrig, >= 25 -> ok,
dazwischen bleibt der letzte Zustand. binary_sensor: on -> niedrig, off -> ok. unavailable/
unknown aendert nichts. Gemeldet wird nur beim Wechsel (notifier), nicht kritisch."""
import logging
import math

from smartheat_runtime.notifier import STATE_OK
from smartheat_runtime.ports import SignalNotFound, SourceUnavailable
from smartheat_runtime.runtime_config import BATTERY_LOW_FLAG

logger = logging.getLogger(__name__)

LOW_BELOW_PERCENT = 20
RECOVERED_FROM_PERCENT = 25
STATE_LOW = "niedrig"


def next_state(previous: str, art: str, raw: str) -> str:
    if art == BATTERY_LOW_FLAG:
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


def _message(label: str, art: str, state: str, raw: str) -> str:
    if state == STATE_OK:
        return f"SmartHeat: Batterie von {label} wieder in Ordnung."
    level = "" if art == BATTERY_LOW_FLAG else f" ({raw} %)"
    return f"SmartHeat: Batterie von {label} ist schwach{level}. Bitte bald wechseln."


def check_batteries(rt) -> None:
    for battery in rt.config.battery_refs:
        key = f"batterie:{battery.ref}"
        try:
            raw = rt.signals.get_raw_state(battery.ref)
        except SignalNotFound:
            logger.info("Batterie-Entity %s nicht gefunden (404), wird uebersprungen", battery.ref)
            continue
        except SourceUnavailable as error:
            logger.warning("Batteriepruefung abgebrochen, %s: %s", rt.texts.source_unavailable_log, error)
            return
        except ValueError:
            continue
        state = next_state(rt.notifier.state(key), battery.art, raw)
        rt.notifier.notify(
            key, state, _message(rt.signals.device_key(battery.ref), battery.art, state, raw), critical=False,
        )
