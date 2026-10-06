from smartheat_gateway.agent.safety_warnings import compute


def _candidate(**levers):
    return {"lever_set": "viessmann_vicare", "hebel": {
        lever: {"wert": value, "min": low, "max": high, "schritt": 0.1} for lever, (value, low, high) in levers.items()
    }}


def test_value_outside_or_range_too_small_is_warned_per_distribution_system():
    candidate = _candidate(curve=(1.6, 0.2, 3.5), level=(0.0, -13.0, 40.0), room_setpoint=(20.0, 18.0, 30.0))
    warnings = compute(candidate)
    # Heizkoerper: curve 1,6 > 1,4; room_setpoint-Bereich der Anlage beginnt erst bei 18 (lokal 15)
    assert warnings["Heizkoerper"] == ["curve", "room_setpoint"]
    assert "curve" in warnings["Fussbodenheizung"]


def test_rejected_candidate_has_no_warnings():
    assert compute({"lever_set": None, "hebel": {}}) == {}
