"""Mindestvorlauftemperatur = Raum-Soll des Kunden (TP11; client-abgeleiteter Hebel min_flow, Spec 3.3). Die
Mindestvorlauftemperatur ist bei Vaillant eine Untergrenze, keine Parallelverschiebung; sie wird lokal nachgefuehrt, der
Server kennt sie nicht. Geschrieben wird nur bei Abweichung vom Live-Wert (lokaler HA-Aufruf,
kein Cloud-Aufruf), also beim Start und nach einer Soll-Aenderung."""
import logging
from typing import Any, Protocol

from smartheat_core.clamping import clamp, round_to_step

logger = logging.getLogger(__name__)


class DerivedRuntime(Protocol):
    """Was der abgeleitete Hebel braucht: Zustand (store) und Hebel-Pipeline mit Binding (override)."""

    store: Any
    override: Any


TOLERANCE = 0.05


def expected(rt: DerivedRuntime) -> float | None:
    """Sollwert des Mindestvorlaufs; None ohne Raum-Soll oder wenn der Hebelsatz keinen Mindestvorlauf ableitet
    (Plan 3b: Weishaupt, Viessmann)."""
    if "min_flow" not in rt.override.binding.description.lever_set.client_derived:
        return None
    target = rt.store.state.stable_target
    if target is None:
        return None
    low, high = rt.override.safety.ranges["min_flow"]
    return round_to_step(clamp(target, low, high), rt.override.binding.description.steps["min_flow"])


def sync(rt: DerivedRuntime) -> None:
    """Schreibt den Mindestvorlauf, wenn er nicht dem Raum-Soll entspricht. Vergleichswert ist der
    Live-Wert aus HA -- ausser kurz nach einem eigenen Schreiben (LeverPipeline.settled): dann kann HA
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
            current = rt.override.binding.read("min_flow")
        except Exception as error:
            logger.warning("Mindestvorlauf nicht lesbar (%s), wird neu geschrieben", error)
            current = None
    if current is not None and abs(current - value) <= TOLERANCE:
        rt.store.update(min_flow_current=value)
        return
    try:
        written = rt.override.write_lever("min_flow", value)
    except Exception:
        logger.exception("Mindestvorlauf konnte nicht geschrieben werden, naechster Versuch beim naechsten Anlass")
        return
    rt.store.update(min_flow_current=written)
    logger.info("Mindestvorlauf auf Raum-Soll gesetzt: %s", written)
