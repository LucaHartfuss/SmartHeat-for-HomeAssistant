"""Stellgroessen der Anlage lesen und schreiben (TP11, Spec 3.1/5.2). Steigung und Mindestvorlauf
sind number-Entities, die Parallelverschiebung eine number- oder Climate-Entity (Zonen-Wunschtemperatur;
bei mypyllant nur im Modus "Manuell" = HA heat_cool dauerhaft, sonst entstuende ein Quick-Veto).
Ohne eigenen Zustand; wer schreibt, entscheidet override.py."""
import logging
import math

from smartheat_core.clamping import target_value

logger = logging.getLogger(__name__)

MANUAL_HVAC_MODE = "heat_cool"
# mypyllant meldet als Wunschtemperatur 0, wenn die Zone gerade nicht heizt (Eco, Heizgrenze).
# Darunter ist der Wert kein Sollwert, sondern "Zone inaktiv" (Plan-Praezisierung 11).
SHIFT_READ_MIN = 5.0
# Server-Plausibilitaetsbereich fuer shift_current (heizungsserver generic/messages.py
# PLAUSIBLE_RANGES). Darueber ist der Wert kein plausibler Sollwert mehr, sondern ein Lesefehler --
# er wird nie an den Server gemeldet.
SHIFT_READ_MAX = 35.0
# Schrittweiten der Anlage (mypyllant: heating_curve 0.05, Zonen-Sollwert 0.5, min_flow 0.1, Heizgrenze 0.1).
STEPS = {"curve_current": 0.05, "shift_current": 0.5, "min_flow": 0.1, "heat_limit": 0.1}


def entity_of(ref: str) -> str:
    return ref.partition("::")[0]


def is_climate(ref: str) -> bool:
    return entity_of(ref).startswith("climate.")


def ensure_manual_mode(ha_api, ref: str) -> bool:
    """Stellt eine Climate-Zone auf heat_cool. True, wenn umgestellt wurde. Wirft bei Fehlern."""
    if not is_climate(ref):
        return False
    entity_id = entity_of(ref)
    if ha_api.get_raw_state(entity_id) == MANUAL_HVAC_MODE:
        return False
    ha_api.set_hvac_mode(entity_id, MANUAL_HVAC_MODE)
    logger.warning("Zone %s auf Manuell (%s) gestellt", entity_id, MANUAL_HVAC_MODE)
    return True


def write(
    ha_api, role: str, ref: str, value: float, minimum: float, maximum: float, ensure_mode: bool = True
) -> float:
    """Begrenzt, rundet auf die Schrittweite der Rolle und schreibt. Gibt den geschriebenen Wert
    zurueck. Wirft bei Fehlern (der Aufrufer macht daraus DeviceWriteError), auch wenn die Entity
    nicht verfuegbar ist (unavailable/unknown): dann wird nichts geschrieben.

    `ensure_mode=False` ueberspringt die Modus-Pruefung/-Umstellung (Ruling #3): spiegelt die
    Hersteller-Cloud die Umstellung bei mypyllants Refresh kurz danach noch nicht wider, zeigt HA den
    alten Modus bis zum naechsten Poll (bis ~30 min), ein zweiter Check wuerde sonst ein
    zweites `set_hvac_mode` senden und unnoetig Cloud-Kontingent verbrauchen. Aufrufer, die den
    Modus selbst schon umgestellt haben (z.B. beim Erstkontakt), rufen mit False."""
    target = target_value(value, minimum, maximum, STEPS[role])
    # AU-016: HA ueberspringt eine nicht verfuegbare Entity im Service-Aufruf still (HTTP 200), der
    # Wert gaelte sonst als geschrieben. get_raw_state wirft bei unavailable/unknown (lokaler
    # HA-Read, kein Cloud-Aufruf).
    ha_api.get_raw_state(entity_of(ref))
    if is_climate(ref):
        if ensure_mode:
            ensure_manual_mode(ha_api, ref)
        ha_api.set_climate_temperature(entity_of(ref), target)
    else:
        ha_api.set_number_value(entity_of(ref), target)
    return target


def read_shift(ha_api, ref: str) -> float | None:
    """Live-Parallelverschiebung; None, wenn die Zone inaktiv meldet. Wirft bei Lesefehlern,
    auch bei einem Wert ueber SHIFT_READ_MAX (unplausibel, wird nie als Snapshot-Wert gesendet)."""
    value = ha_api.get_state(ref)
    if not math.isfinite(value):
        return None
    if value > SHIFT_READ_MAX:
        raise ValueError(f"Parallelverschiebung {value} liegt ueber dem plausiblen Maximum {SHIFT_READ_MAX}")
    if value < SHIFT_READ_MIN:
        return None
    return value


def current_shift(ha_api, ref: str, fallback: float | None) -> float | None:
    """Fuer den Snapshot: Live-Wert, sonst `fallback` (zuletzt geschriebener Wert). Der Server protokolliert den
    gemeldeten Wert beim Erstkontakt nur und vergleicht ihn an jedem Tagestick mit seinem zuletzt gesendeten
    Wert ("Anlage folgt nicht"); der Rueckfall auf den Schreibwert verhindert den Fehlalarm, solange die ruhende
    Zone 0 liest."""
    try:
        live = read_shift(ha_api, ref)
    except Exception as error:
        logger.warning("Parallelverschiebung nicht lesbar (%s), verwende gespeicherten Wert %s", error, fallback)
        return fallback
    return live if live is not None else fallback
