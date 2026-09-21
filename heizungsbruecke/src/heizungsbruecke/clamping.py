import math


def clamp(value: float, minimum: float, maximum: float) -> float:
    """Clamps `value` into [minimum, maximum].

    Whole-branch review finding I2: `max(min(value, maximum), minimum)` correctly
    clamps +-Infinity to the respective bound (comparisons against +-inf behave like
    any other float), but NaN comparisons are always False, so `min`/`max` degenerate
    and NaN sails through unchanged instead of being clamped or rejected. `json.loads()`
    accepts the bare `NaN` token and `float()` happily parses the string "nan", so a
    NaN can reach this function from an HA entity state or an MQTT down-message
    payload. Rather than silently pick a fallback (which bound? clamped-to-NaN is not
    a safe value either), a NaN `value` is rejected outright -- callers in bridge.py
    already run inside a try/except that logs and safely drops the offending write
    instead of crashing. +-Infinity is deliberately NOT rejected here: that is
    pre-existing, correct clamp-to-bound behaviour this fix must not change.
    """
    if math.isnan(value):
        raise ValueError(f"clamp() erhielt einen nicht-endlichen Wert (NaN): {value!r}")
    return max(min(value, maximum), minimum)
