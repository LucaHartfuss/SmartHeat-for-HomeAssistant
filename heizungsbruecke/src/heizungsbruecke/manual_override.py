"""Manueller Eingriff an Kurve/Offset (R6, Spec TP7 3.6). Weicht die Anlage vom
Wiederherstellungspunkt ab, ohne dass ein Boost laeuft oder ein Datenfehler vorliegt, hat
jemand von Hand verstellt. SmartHeat ueberschreibt das weiterhin beim naechsten Regelschritt,
meldet den Eingriff aber (nicht kritisch, abschaltbar) und schickt ihn als KPI mit dem naechsten
Snapshot an den Server. Erst nach DETECTION_ROUNDS Runden in Folge, und nie innerhalb von
OWN_WRITE_SETTLE_SECONDS nach einem eigenen erfolgreichen Schreiben auf Kurve/Offset (Serverwerte,
Boost-Start/-Ende, Wiederherstellung): der HA-Zustand kann so lange nachhinken. Die Rueckkehr
hebt den Hinweis still auf, auch innerhalb dieses Fensters.

Jeder offene Datenfehler pausiert die Erkennung, nicht nur ein Schreibfehler: ein Schreibfehler
kann durch einen lokalen oder Server-Datenfehler abgeloest werden (delivery._read_invalid,
delivery._ack bei einer Ablehnung), waehrend die Anlage noch auf den alten Werten steht (der
Wiederherstellungspunkt wird vor dem Schreiben gespeichert, siehe override.apply_server_values) --
eine Erkennung wuerde dann faelschlich anschlagen, obwohl niemand von Hand eingegriffen hat."""
import logging
import math
from datetime import datetime

from heizungsbruecke.notifier import STATE_OK

logger = logging.getLogger(__name__)

KEY = "manueller_eingriff"
DETECTION_ROUNDS = 2
# mypyllant fragt die myVAILLANT-Cloud nur alle 30 min ab (DEFAULT_UPDATE_INTERVAL); der Refresh
# 5 s nach einem set_* kann noch den alten Cloud-Wert liefern. HA zeigt nach eigenem Schreiben
# also bis zu 30 min den alten Wert -- ohne Pause meldete R6 danach (z. B. nach jedem
# Comfort-Boost-Ende) faelschlich einen manuellen Eingriff. 35 min = 30 min Poll + Reserve.
OWN_WRITE_SETTLE_SECONDS = 2100
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
        or fault is not None
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
    since_write = rt.override.seconds_since_last_write()
    if since_write is not None and since_write < OWN_WRITE_SETTLE_SECONDS:
        # Abweichung kurz nach eigenem Schreiben: HA hinkt vermutlich nach, keine Erkennung.
        _set_misses(rt, 0)
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
