from dataclasses import dataclass


@dataclass(frozen=True)
class BoostDecision:
    active: bool
    curve_value: float | None
    offset_value: float | None


def decide_boost(
    room_actual: float,
    room_target: float,
    threshold_k: float,
    boost_curve_value: float,
    boost_offset_value: float,
) -> BoostDecision:
    if room_actual < room_target - threshold_k:
        return BoostDecision(active=True, curve_value=boost_curve_value, offset_value=boost_offset_value)
    return BoostDecision(active=False, curve_value=None, offset_value=None)
