import math


def clamp(value: float, minimum: float, maximum: float) -> float:
    """Begrenzt value auf [minimum, maximum]. NaN wird abgelehnt statt still durchgereicht (min/max vergleichen mit NaN immer False): JSON und float() akzeptieren "NaN", und weder eine der Grenzen noch NaN ist ein sicherer Ersatzwert. ±Infinity wird wie jede Zahl auf die Grenze geklemmt."""
    if math.isnan(value):
        raise ValueError(f"clamp() erhielt einen nicht-endlichen Wert (NaN): {value!r}")
    return max(min(value, maximum), minimum)


def round_to_step(value: float, step: float) -> float:
    return round(round(value / step) * step, 6)


def target_value(value: float, minimum: float, maximum: float, step: float) -> float:
    """Begrenzt, rundet auf die Schrittweite und begrenzt danach erneut (falls das Runden ueber den Rand traegt).
    Gemeinsame Zielwert-Berechnung fuer Schreiben, Quota-Check und Durchsetzen, damit alle Seiten denselben Wert fuer
    "unveraendert" halten."""
    return clamp(round_to_step(clamp(value, minimum, maximum), step), minimum, maximum)
