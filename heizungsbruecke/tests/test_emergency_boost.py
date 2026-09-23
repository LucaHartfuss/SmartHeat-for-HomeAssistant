from heizungsbruecke.emergency_boost import decide_emergency_boost


def test_decide_emergency_boost_inactive_when_room_within_trigger_threshold():
    decision = decide_emergency_boost(
        room_actual=19.2, room_target=20.0, emergency_was_active=False,
        exit_threshold_k=0.5, max_curve_value=1.5, max_offset_value=30.0,
    )
    assert decision.active is False


def test_decide_emergency_boost_inactive_exactly_at_trigger_threshold():
    # room_actual == room_target - 1.0 ist noch NICHT "mehr als 1.0 K drunter".
    decision = decide_emergency_boost(
        room_actual=19.0, room_target=20.0, emergency_was_active=False,
        exit_threshold_k=0.5, max_curve_value=1.5, max_offset_value=30.0,
    )
    assert decision.active is False


def test_decide_emergency_boost_triggers_when_room_more_than_1k_below_target():
    decision = decide_emergency_boost(
        room_actual=18.9, room_target=20.0, emergency_was_active=False,
        exit_threshold_k=0.5, max_curve_value=1.5, max_offset_value=30.0,
    )
    assert decision.active is True
    assert decision.curve_value == 1.5
    assert decision.offset_value == 30.0


def test_decide_emergency_boost_stays_active_above_exit_threshold():
    decision = decide_emergency_boost(
        room_actual=19.4, room_target=20.0, emergency_was_active=True,
        exit_threshold_k=0.5, max_curve_value=1.5, max_offset_value=30.0,
    )
    assert decision.active is True


def test_decide_emergency_boost_exits_once_within_exit_threshold():
    decision = decide_emergency_boost(
        room_actual=19.6, room_target=20.0, emergency_was_active=True,
        exit_threshold_k=0.4, max_curve_value=1.5, max_offset_value=30.0,
    )
    assert decision.active is False


def test_decide_emergency_boost_retriggers_after_a_prior_exit():
    # Solange Notbetrieb laeuft, kann der Zyklus (Ausloesen -> Erreichen -> erneutes
    # Auskuehlen -> erneutes Ausloesen) beliebig oft wiederholen (Design-Spec 2026-09-23).
    first = decide_emergency_boost(
        room_actual=19.6, room_target=20.0, emergency_was_active=True,
        exit_threshold_k=0.4, max_curve_value=1.5, max_offset_value=30.0,
    )
    assert first.active is False

    second = decide_emergency_boost(
        room_actual=18.8, room_target=20.0, emergency_was_active=False,
        exit_threshold_k=0.4, max_curve_value=1.5, max_offset_value=30.0,
    )
    assert second.active is True
