from dataclasses import dataclass

EMERGENCY_TRIGGER_K = 1.0


@dataclass(frozen=True)
class EmergencyBoostDecision:
    active: bool
    curve_value: float | None
    offset_value: float | None


def decide_emergency_boost(
    room_actual: float,
    room_target: float,
    emergency_was_active: bool,
    exit_threshold_k: float,
    max_curve_value: float,
    max_offset_value: float,
) -> EmergencyBoostDecision:
    """Reine Hysterese-Logik (die des Comfort-Boosts VOR dessen funktionaler
    Neudefinition am 2026-09-15) -- bewusst nur ausgewertet, waehrend Notbetrieb
    aktiv ist (regulation.run_local_check), nicht mehr generell wie damals.

    Trigger (inaktiv -> aktiv): der Raum ist mehr als EMERGENCY_TRIGGER_K (1.0 K) unter
    dem Sollwert. Exit (aktiv -> inaktiv): der Raum hat sich bis auf `exit_threshold_k`
    an den Sollwert angenaehert -- derselbe Wert wie die bestehende
    `boost_threshold_k`-Option des Comfort-Boosts, kein eigenes Config-Feld.

    `max_curve_value`/`max_offset_value` sind die Profil-Clamp-Obergrenzen
    (curve_max/offset_max), keine neuen, vom Profil unabhaengigen Werte.
    """
    if emergency_was_active:
        active = room_actual < room_target - exit_threshold_k
    else:
        active = room_actual < room_target - EMERGENCY_TRIGGER_K

    if active:
        return EmergencyBoostDecision(active=True, curve_value=max_curve_value, offset_value=max_offset_value)
    return EmergencyBoostDecision(active=False, curve_value=None, offset_value=None)
