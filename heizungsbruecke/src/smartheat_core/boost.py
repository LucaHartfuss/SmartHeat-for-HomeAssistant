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

    Der Start verlangt zusaetzlich, dass der Raum noch nicht angekommen ist
    (`room_actual < room_target - arrival_threshold_k`, dieselbe Bedingung wie fuers
    Weiterlaufen): bei warmem Raum gaebe es einen Schein-Start, der beim naechsten Check
    endet und den echten Boost ueber das Startbudget (30 min Abstand) sperrt.
    """
    not_arrived = room_actual < room_target - arrival_threshold_k
    if boost_was_active:
        return not_arrived
    rose = previous_room_target is not None and room_target > previous_room_target + _TARGET_RISE_EPSILON_K
    return rose and not_arrived


# Nutzer-Entscheidung, Freigabe nach Regel 4 fuer genau diese Werte: der Comfort-Boost ist
# "kurzzeitig" -- er endet spaetestens nach 4 h und wenn der Raumfuehler dreimal in Folge nicht lesbar ist.
COMFORT_BOOST_MAX_SECONDS = 4 * 3600
UNREADABLE_ROOM_CHECKS = 3


def comfort_boost_expired(since, now, mono_seit: float | None = None) -> bool:
    """since: Beginn des laufenden Comfort-Boosts (aware datetime) oder None (unbekannt: nicht abgelaufen).
    mono_seit: Sekunden auf der monotonen Prozessuhr seit dem Start in diesem Prozess oder None (Start in einem
    frueheren Lauf). Der Boost endet auch, wenn die Wanduhr ihn in der Zukunft beginnen laesst (Uhr zurueckgesprungen)
    oder die monotone Uhr die Hoechstdauer zaehlt: nie spaeter als die Wanduhr allein, hoechstens frueher."""
    if mono_seit is not None and mono_seit >= COMFORT_BOOST_MAX_SECONDS:
        return True
    if since is None:
        return False
    dauer = (now - since).total_seconds()
    return dauer < 0 or dauer >= COMFORT_BOOST_MAX_SECONDS
