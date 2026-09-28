"""Manueller Eingriff an Kurve/Offset (R6, Spec TP7 3.6). Weicht die Anlage vom
Wiederherstellungspunkt ab, ohne dass ein Boost laeuft oder ein Schreibfehler vorliegt, hat
jemand von Hand verstellt. SmartHeat ueberschreibt das weiterhin beim naechsten Regelschritt,
meldet den Eingriff aber (nicht kritisch, abschaltbar) und schickt ihn als KPI mit dem naechsten
Snapshot an den Server. Erst nach DETECTION_ROUNDS Runden in Folge: nach eigenem Schreiben kann
der HA-Zustand eine Runde nachhinken. Die Rueckkehr hebt den Hinweis still auf."""
import logging
import math
from datetime import datetime

from heizungsbruecke import delivery
from heizungsbruecke.notifier import STATE_OK

logger = logging.getLogger(__name__)

KEY = "manueller_eingriff"
DETECTION_ROUNDS = 2
TOLERANCE = {"curve_current": 0.01, "offset_current": 0.1}
_EPSILON = 1e-9  # Gleitkomma-Rest (0.91 - 0.9) zaehlt nicht als Abweichung
RETURN_MESSAGE = "SmartHeat: Kurve und Offset stehen wieder auf den gelernten Werten."


def _message(curve: float, offset: float) -> str:
    return (
        f"SmartHeat: Die Heizkurve wurde manuell auf {curve:g} / Offset {offset:g} gestellt. "
        "SmartHeat regelt die Kurve selbst und setzt sie beim nächsten Regelschritt zurück."
    )


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _set_misses(rt, misses: int) -> None:
    if rt.store.state.manual_override_misses != misses:
        rt.store.update(manual_override_misses=misses)


def _read_live(rt) -> dict | None:
    live = {}
    for role in TOLERANCE:
        try:
            value = rt.ha_api.get_state(rt.manifest.entity_ids[role])
        except Exception as error:
            logger.info("Manueller Eingriff nicht pruefbar, %s nicht lesbar: %s", role, error)
            return None
        if not _is_number(value):
            return None
        live[role] = value
    return live


def check_manual_override(rt) -> None:
    state = rt.store.state
    fault = state.delivery.datenfehler
    point = {"curve_current": state.curve_current, "offset_current": state.offset_current}
    if (
        state.boost_active or state.emergency_boost_active
        or (fault is not None and fault.source == delivery.SOURCE_WRITE)
        or not all(_is_number(value) for value in point.values())
    ):
        _set_misses(rt, 0)
        return
    live = _read_live(rt)
    if live is None:
        _set_misses(rt, 0)
        return
    if not any(abs(live[role] - point[role]) > TOLERANCE[role] + _EPSILON for role in point):
        _set_misses(rt, 0)
        if state.manual_override is not None:
            rt.store.update(manual_override=None)
        rt.notifier.notify(KEY, STATE_OK, RETURN_MESSAGE, critical=False, silent_ok=True)
        return
    misses = min(state.manual_override_misses + 1, DETECTION_ROUNDS)
    _set_misses(rt, misses)
    if misses < DETECTION_ROUNDS:
        return
    curve, offset = live["curve_current"], live["offset_current"]
    current = state.manual_override
    if current is not None and (current["curve"], current["offset"]) == (curve, offset):
        return
    detected = {"curve": curve, "offset": offset, "erkannt": datetime.now().astimezone().isoformat(timespec="seconds")}
    rt.store.update(manual_override=detected, manual_override_pending=detected)
    rt.notifier.notify(KEY, f"{curve:g}/{offset:g}", _message(curve, offset), critical=False)
