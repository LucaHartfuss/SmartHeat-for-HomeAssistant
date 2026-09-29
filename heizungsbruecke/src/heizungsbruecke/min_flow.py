"""Mindestvorlauftemperatur = Raum-Soll des Kunden (TP11, Spec 2/5.2). Die Mindestvorlauftemperatur
ist bei Vaillant eine Untergrenze, keine Parallelverschiebung; sie wird lokal nachgefuehrt, der
Server kennt sie nicht. Geschrieben wird nur bei Abweichung vom Live-Wert (lokaler HA-Aufruf,
kein Cloud-Aufruf), also beim Start und nach einer Soll-Aenderung."""
import logging

from heizungsbruecke.clamping import clamp
from heizungsbruecke.plant import STEPS, round_to_step

logger = logging.getLogger(__name__)

TOLERANCE = 0.05


def expected(rt) -> float | None:
    target = rt.store.state.stable_target
    if target is None:
        return None
    options = rt.options
    return round_to_step(clamp(target, options["min_flow_min"], options["min_flow_max"]), STEPS["min_flow"])


def sync(rt) -> None:
    value = expected(rt)
    if value is None:
        return
    try:
        live = rt.ha_api.get_state(rt.manifest.entity_ids["min_flow"])
    except Exception as error:
        logger.warning("Mindestvorlauf nicht lesbar (%s), wird neu geschrieben", error)
        live = None
    if live is not None and abs(live - value) <= TOLERANCE:
        rt.store.update(min_flow_current=value)
        return
    try:
        written = rt.override.write_min_flow(value)
    except Exception:
        logger.exception("Mindestvorlauf konnte nicht geschrieben werden, naechster Versuch beim naechsten Anlass")
        return
    rt.store.update(min_flow_current=written)
    logger.info("Mindestvorlauf auf Raum-Soll gesetzt: %s", written)
