WINDOW_SECONDS = 86400


def record_change(history: list[list[float]], now: float, value: float) -> list[list[float]]:
    if history and history[-1][1] == value:
        return history
    updated = [*history, [now, value]]
    window_start = now - WINDOW_SECONDS
    before = [entry for entry in updated if entry[0] <= window_start]
    inside = [entry for entry in updated if entry[0] > window_start]
    return before[-1:] + inside


def time_weighted_mean(history: list[list[float]], now: float, window_s: float = WINDOW_SECONDS) -> float | None:
    if not history:
        return None
    window_start = now - window_s
    weighted_sum = 0.0
    total = 0.0
    for index, (ts, value) in enumerate(history):
        segment_end = history[index + 1][0] if index + 1 < len(history) else now
        segment_start = max(ts, window_start)
        duration = segment_end - segment_start
        if duration > 0:
            weighted_sum += value * duration
            total += duration
    if total == 0:
        return history[-1][1]
    return weighted_sum / total
