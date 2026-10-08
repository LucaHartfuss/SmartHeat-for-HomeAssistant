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


# Audit 4, A4-09 (Nutzer-Entscheidung E2, Freigabe nach Regel 4 fuer genau diese Werte): der Comfort-Boost ist
# "kurzzeitig" (FR-3) -- er endet spaetestens nach 4 h und wenn der Raumfuehler dreimal in Folge nicht lesbar ist.
COMFORT_BOOST_MAX_SECONDS = 4 * 3600
UNREADABLE_ROOM_CHECKS = 3


def comfort_boost_expired(since, now) -> bool:
    """since: Beginn des laufenden Comfort-Boosts (aware datetime) oder None (unbekannt: nicht abgelaufen)."""
    return since is not None and (now - since).total_seconds() >= COMFORT_BOOST_MAX_SECONDS
