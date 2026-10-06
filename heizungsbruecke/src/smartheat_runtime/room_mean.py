"""Raummittel aus mehreren Fuehlern (Spec SHG G2 2.6), dieselben Regeln wie das HA-Template
heizungsbruecke.helper_templates.room_temperature_template: ungueltige und unplausible Werte zaehlen nicht, Mittel der
gueltigen auf 2 Nachkommastellen, ohne gueltigen Wert None. Aequivalenztest: tests/test_room_mean.py."""
from collections.abc import Iterable

from smartheat_runtime.plausibility import ROOM_TEMP_RANGE, is_plausible


def room_mean(values: Iterable[object]) -> float | None:
    valid = [float(value) for value in values if is_plausible(value, ROOM_TEMP_RANGE)]  # type: ignore[arg-type]
    if not valid:
        return None
    return round(sum(valid) / len(valid), 2)
