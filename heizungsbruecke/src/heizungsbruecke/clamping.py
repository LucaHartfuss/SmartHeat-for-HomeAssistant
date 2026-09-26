import math


def clamp(value: float, minimum: float, maximum: float) -> float:
    """Begrenzt value auf [minimum, maximum]. NaN wird abgelehnt statt still durchgereicht (min/max vergleichen mit NaN immer False): JSON und float() akzeptieren "NaN", und weder eine der Grenzen noch NaN ist ein sicherer Ersatzwert. ±Infinity wird wie jede Zahl auf die Grenze geklemmt."""
    if math.isnan(value):
        raise ValueError(f"clamp() erhielt einen nicht-endlichen Wert (NaN): {value!r}")
    return max(min(value, maximum), minimum)
