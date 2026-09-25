WINDOW_SECONDS = 86400


def sanitize_history(raw: object) -> list[list[float]]:
    """Validates a `target_history` value loaded from the backup file.

    A malformed value (missing, `None`, not a list, or entries that aren't
    2-element numeric `[timestamp, value]` pairs) would otherwise make
    `record_change`/`time_weighted_mean` raise on every local check, stopping
    boost and snapshots entirely. Returns `raw` unchanged (as a list of
    `[ts, value]` lists) when it's well-formed, or `[]` otherwise -- callers
    treat `[]` the same as a fresh install and simply re-seed it.
    """
    if not isinstance(raw, list):
        return []
    result: list[list[float]] = []
    for entry in raw:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            return []
        ts, value = entry
        if isinstance(ts, bool) or isinstance(value, bool):
            return []
        if not isinstance(ts, (int, float)) or not isinstance(value, (int, float)):
            return []
        result.append([ts, value])
    return result


def record_change(history: list[list[float]], now: float, value: float) -> list[list[float]]:
    if history and history[-1][1] == value:
        return history
    if history:
        # Guard against a backward wall-clock jump (e.g. NTP resync): appending
        # an entry with ts < last recorded ts would leave the history unsorted,
        # which time_weighted_mean() relies on.
        now = max(now, history[-1][0])
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
        segment_start = min(segment_start, now)
        segment_end = min(segment_end, now)
        duration = segment_end - segment_start
        if duration > 0:
            weighted_sum += value * duration
            total += duration
    if total == 0:
        return history[-1][1]
    return weighted_sum / total
