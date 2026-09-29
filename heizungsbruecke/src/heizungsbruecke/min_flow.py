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
    """Schreibt den Mindestvorlauf, wenn er nicht dem Raum-Soll entspricht. Vergleichswert ist der
    Live-Wert aus HA -- ausser kurz nach einem eigenen Schreiben (Override.settled): dann kann HA
    bei mypyllant bis zum naechsten Poll (bis ~30 min) noch den alten Wert zeigen, und massgeblich
    ist der eigene letzte Schreibwert. Sonst wuerde eine Rueckkehr zum alten Wert innerhalb dieser Zeit uebersprungen
    (die Anlage bliebe auf dem neuen) und jeder weitere Anlass schriebe denselben Wert erneut.
    Ohne eigenes Schreiben seit dem Start bleibt nur der Live-Wert. Wirft nie."""
    value = expected(rt)
    if value is None:
        return
    last = rt.override.last_written("min_flow")
    if not rt.override.settled("min_flow") and last is not None:
        current = last
    else:
        try:
            current = rt.ha_api.get_state(rt.manifest.entity_ids["min_flow"])
        except Exception as error:
            logger.warning("Mindestvorlauf nicht lesbar (%s), wird neu geschrieben", error)
            current = None
    if current is not None and abs(current - value) <= TOLERANCE:
        rt.store.update(min_flow_current=value)
        return
    try:
        written = rt.override.write_min_flow(value)
    except Exception:
        logger.exception("Mindestvorlauf konnte nicht geschrieben werden, naechster Versuch beim naechsten Anlass")
        return
    rt.store.update(min_flow_current=written)
    logger.info("Mindestvorlauf auf Raum-Soll gesetzt: %s", written)
