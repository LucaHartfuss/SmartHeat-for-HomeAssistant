"""Sicherheitswarnungen je Verteilsystem fuer einen Probe-Kandidaten (Spec SHG G3 2.4): ein Hebel ist zu warnen, wenn
sein aktueller Anlagenwert ausserhalb der lokalen Sicherheitswerte liegt oder der Anlagenbereich die lokalen Grenzen
nicht abdeckt (dann kann der Notfall-Boost bzw. das Clamp-Ziel nicht geschrieben werden). Die lokalen Werte bleiben auf
dem Client (smartheat_core.safety)."""
from smartheat_core.safety import LOCAL_SAFETY


def compute(candidate: dict) -> dict[str, list[str]]:
    lever_set = candidate.get("lever_set")
    if lever_set is None:
        return {}
    result: dict[str, list[str]] = {}
    for (set_id, verteilsystem), safety in sorted(LOCAL_SAFETY.items()):
        if set_id != lever_set:
            continue
        flagged = []
        for lever, (low, high) in sorted(safety.ranges.items()):
            plant = candidate.get("hebel", {}).get(lever)
            if plant is None:
                continue
            outside = not low <= plant["wert"] <= high
            uncovered = plant["min"] > low or plant["max"] < high
            if outside or uncovered:
                flagged.append(lever)
        result[verteilsystem] = flagged
    return result
