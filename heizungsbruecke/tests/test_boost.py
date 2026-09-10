from heizungsbruecke.boost import decide_boost


def test_decide_boost_activates_when_room_too_cold():
    decision = decide_boost(
        room_actual=19.0, room_target=20.0, threshold_k=0.5,
        boost_curve_value=1.5, boost_offset_value=30.0,
    )
    assert decision.active is True
    assert decision.curve_value == 1.5
    assert decision.offset_value == 30.0


def test_decide_boost_inactive_within_threshold():
    decision = decide_boost(
        room_actual=19.6, room_target=20.0, threshold_k=0.5,
        boost_curve_value=1.5, boost_offset_value=30.0,
    )
    assert decision.active is False
    assert decision.curve_value is None
    assert decision.offset_value is None


def test_decide_boost_inactive_exactly_at_threshold():
    decision = decide_boost(
        room_actual=19.5, room_target=20.0, threshold_k=0.5,
        boost_curve_value=1.5, boost_offset_value=30.0,
    )
    assert decision.active is False
