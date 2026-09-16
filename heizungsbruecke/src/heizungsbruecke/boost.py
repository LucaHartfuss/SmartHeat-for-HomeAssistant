from dataclasses import dataclass

_TARGET_RISE_EPSILON_K = 0.01


@dataclass(frozen=True)
class BoostDecision:
    active: bool
    curve_value: float | None
    offset_value: float | None


def decide_boost(
    room_actual: float,
    room_target: float,
    previous_room_target: float | None,
    boost_was_active: bool,
    arrival_threshold_k: float,
    boost_curve_value: float,
    boost_offset_value: float,
) -> BoostDecision:
    """Boost aktiviert ausschliesslich als Reaktion auf eine Erhoehung der
    Wunschtemperatur (Komfort-Beschleunigung), nicht mehr bei Kaelte aus anderer
    Ursache -- bewusste funktionale Neudefinition, siehe Design-Spec Phase 5, Punkt 15
    (inkl. dokumentiertem Tradeoff: Boost ist damit kein serverunabhaengiges
    Sicherheitsnetz mehr).

    `previous_room_target=None` (erster Tick ueberhaupt, kein Vorwert bekannt) loest nie
    einen Trigger aus -- es gibt keine "Erhoehung" ohne Vorwert.
    """
    if boost_was_active:
        active = room_actual < room_target - arrival_threshold_k
    else:
        active = (
            previous_room_target is not None
            and room_target > previous_room_target + _TARGET_RISE_EPSILON_K
        )

    if active:
        return BoostDecision(active=True, curve_value=boost_curve_value, offset_value=boost_offset_value)
    return BoostDecision(active=False, curve_value=None, offset_value=None)
