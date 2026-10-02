_TARGET_RISE_EPSILON_K = 0.01


def decide_boost(
    room_actual: float,
    room_target: float,
    previous_room_target: float | None,
    boost_was_active: bool,
    arrival_threshold_k: float,
) -> bool:
    """Boost aktiviert ausschliesslich als Reaktion auf eine Erhoehung der
    Wunschtemperatur (Komfort-Beschleunigung), nicht mehr bei Kaelte aus anderer
    Ursache -- bewusste funktionale Neudefinition (inkl. dokumentiertem Tradeoff: Boost
    ist damit kein serverunabhaengiges Sicherheitsnetz mehr).

    `previous_room_target=None` (erster Tick ueberhaupt, kein Vorwert bekannt) loest nie
    einen Trigger aus -- es gibt keine "Erhoehung" ohne Vorwert.
    """
    if boost_was_active:
        return room_actual < room_target - arrival_threshold_k
    return previous_room_target is not None and room_target > previous_room_target + _TARGET_RISE_EPSILON_K
