"""Stellgroessen der Anlage lesen und schreiben (TP11, Spec 3.1/5.2). Steigung und Mindestvorlauf
sind number-Entities, die Parallelverschiebung eine number- oder Climate-Entity (Zonen-Wunschtemperatur;
bei mypyllant nur im Modus "Manuell" = HA heat_cool dauerhaft, sonst entstuende ein Quick-Veto).
Ohne eigenen Zustand; wer schreibt, entscheidet override.py."""
import logging
import math

from heizungsbruecke.clamping import clamp

logger = logging.getLogger(__name__)

MANUAL_HVAC_MODE = "heat_cool"
# mypyllant meldet als Wunschtemperatur 0, wenn die Zone gerade nicht heizt (Eco, Heizgrenze).
# Darunter ist der Wert kein Sollwert, sondern "Zone inaktiv" (Plan-Praezisierung 11).
SHIFT_READ_MIN = 5.0
# Server-Plausibilitaetsbereich fuer shift_current (heizungsserver generic/messages.py
# PLAUSIBLE_RANGES). Darueber ist der Wert kein plausibler Sollwert mehr, sondern ein Lesefehler --
# er wird nie an den Server gemeldet.
SHIFT_READ_MAX = 35.0
# Schrittweiten der Anlage (mypyllant: heating_curve 0.05, Zonen-Sollwert 0.5, min_flow 0.1).
STEPS = {"curve_current": 0.05, "shift_current": 0.5, "min_flow": 0.1}


def entity_of(ref: str) -> str:
    return ref.partition("::")[0]


def is_climate(ref: str) -> bool:
    return entity_of(ref).startswith("climate.")


def round_to_step(value: float, step: float) -> float:
    return round(round(value / step) * step, 6)


def target_value(value: float, minimum: float, maximum: float, step: float) -> float:
    """Begrenzt, rundet auf die Schrittweite und begrenzt danach erneut (falls das Runden ueber
    den Rand traegt). Gemeinsame Zielwert-Berechnung fuer write() und override.py
    (expected_values/_write_role), damit beide Seiten immer denselben Wert fuer "unveraendert"
    halten."""
    return clamp(round_to_step(clamp(value, minimum, maximum), step), minimum, maximum)


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
    zurueck. Wirft bei Fehlern (der Aufrufer macht daraus DeviceWriteError).

    `ensure_mode=False` ueberspringt die Modus-Pruefung/-Umstellung (Ruling #3): spiegelt die
    Hersteller-Cloud die Umstellung bei mypyllants Refresh kurz danach noch nicht wider, zeigt HA den
    alten Modus bis zum naechsten Poll (bis ~30 min), ein zweiter Check wuerde sonst ein
    zweites `set_hvac_mode` senden und unnoetig Cloud-Kontingent verbrauchen. Aufrufer, die den
    Modus selbst schon umgestellt haben (z.B. beim Erstkontakt), rufen mit False."""
    target = target_value(value, minimum, maximum, STEPS[role])
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
    """Fuer den Snapshot: Live-Wert, sonst `fallback` (Wiederherstellungspunkt). Der Server nutzt
    den gemeldeten Wert nur beim Erstkontakt, danach seinen eigenen Stand."""
    try:
        live = read_shift(ha_api, ref)
    except Exception as error:
        logger.warning("Parallelverschiebung nicht lesbar (%s), verwende gespeicherten Wert %s", error, fallback)
        return fallback
    return live if live is not None else fallback
