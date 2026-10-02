EMERGENCY_TRIGGER_K = 1.0


def decide_emergency_boost(
    room_actual: float,
    room_target: float,
    emergency_was_active: bool,
    exit_threshold_k: float,
) -> bool:
    """Reine Hysterese-Logik (die des Comfort-Boosts VOR dessen funktionaler
    Neudefinition am 2026-09-15) -- bewusst nur ausgewertet, waehrend Notbetrieb
    aktiv ist (regulation.run_local_check), nicht mehr generell wie damals.

    Trigger (inaktiv -> aktiv): der Raum ist mehr als EMERGENCY_TRIGGER_K (1.0 K) unter
    dem Sollwert. Exit (aktiv -> inaktiv): der Raum hat sich bis auf `exit_threshold_k`
    an den Sollwert angenaehert -- derselbe Wert wie die bestehende
    `boost_threshold_k`-Option des Comfort-Boosts, kein eigenes Config-Feld.

    Die Boost-Werte stehen nicht hier, sondern in LocalSafety.emergency_boost_levers.
    """
    if emergency_was_active:
        return room_actual < room_target - exit_threshold_k
    return room_actual < room_target - EMERGENCY_TRIGGER_K
