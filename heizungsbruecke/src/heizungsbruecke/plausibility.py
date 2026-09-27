"""Physikalische Plausibilitaetsgrenzen (Spec TP6, Abschnitt 4 und 8). Muessen mit
heizungsserver generic/messages.py::PLAUSIBLE_RANGES (dart/dat) und der Integration
(const.PLAUSIBLE_RANGES) uebereinstimmen; tools/contract_check.py prueft das."""
import math

ROOM_TEMP_RANGE = (5.0, 35.0)
OUTDOOR_TEMP_RANGE = (-40.0, 45.0)


def is_plausible(value, bounds: tuple[float, float]) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return False
    low, high = bounds
    return low <= value <= high
