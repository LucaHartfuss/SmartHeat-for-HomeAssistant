"""Physikalische Plausibilitaetsgrenzen. Muessen mit
heizungsserver regelung/messages.py::PLAUSIBLE_RANGES (room_actual/room_target/outdoor_temp) uebereinstimmen; den
Lesebereich des Raum-Solls der Anlage (room_setpoint) deckt `ha_binding` ab. tools/contract_check.py prueft das
(Vertrag V1b); die Integration prueft seit 1.0.0 keine Werte mehr.

Raum-Ist und Raum-Soll haben getrennte Bereiche: ein Raum kann kaelter als 5 Grad sein (Frostnaehe), ein Soll unter
5 Grad ist dagegen kein Wunsch."""
from typing import TypeGuard

from smartheat_core.zahlen import ist_endliche_zahl

ROOM_TEMP_RANGE = (5.0, 35.0)
ROOM_ACTUAL_RANGE = (-10.0, 35.0)
OUTDOOR_TEMP_RANGE = (-40.0, 45.0)


def is_plausible(value, bounds: tuple[float, float]) -> TypeGuard[float]:
    if not ist_endliche_zahl(value):
        return False
    low, high = bounds
    return low <= value <= high
