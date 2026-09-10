def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(min(value, maximum), minimum)
