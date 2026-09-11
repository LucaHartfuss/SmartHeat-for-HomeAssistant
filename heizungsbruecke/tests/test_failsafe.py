from heizungsbruecke.failsafe import (
    FailsafeState,
    build_discovery_config,
    build_state_payload,
    enter_failsafe_if_stale,
    record_valid_message,
)


def test_enter_failsafe_if_stale_activates_when_stale():
    current = FailsafeState(active=False, recovery_count=0)

    new_state = enter_failsafe_if_stale(current, seconds_since_last_valid=100.0, stale_after_seconds=90.0)

    assert new_state.active is True
    assert new_state.recovery_count == 0


def test_enter_failsafe_if_stale_activates_exactly_at_threshold():
    current = FailsafeState(active=False, recovery_count=0)

    new_state = enter_failsafe_if_stale(current, seconds_since_last_valid=90.0, stale_after_seconds=90.0)

    assert new_state.active is True


def test_enter_failsafe_if_stale_stays_inactive_when_fresh():
    current = FailsafeState(active=False, recovery_count=0)

    new_state = enter_failsafe_if_stale(current, seconds_since_last_valid=10.0, stale_after_seconds=90.0)

    assert new_state == current


def test_enter_failsafe_if_stale_stays_inactive_when_never_received():
    # No valid message ever received yet -- nothing to consider "stale".
    current = FailsafeState(active=False, recovery_count=0)

    new_state = enter_failsafe_if_stale(current, seconds_since_last_valid=None, stale_after_seconds=90.0)

    assert new_state == current


def test_enter_failsafe_if_stale_does_not_change_already_active_state():
    # Recovery only happens via record_valid_message, never here.
    current = FailsafeState(active=True, recovery_count=1)

    new_state = enter_failsafe_if_stale(current, seconds_since_last_valid=99999.0, stale_after_seconds=90.0)

    assert new_state == current


def test_record_valid_message_increments_recovery_count_on_first_message():
    current = FailsafeState(active=True, recovery_count=0)

    new_state = record_valid_message(current)

    assert new_state.active is True
    assert new_state.recovery_count == 1


def test_record_valid_message_exits_failsafe_after_two_consecutive_messages():
    current = FailsafeState(active=True, recovery_count=1)

    new_state = record_valid_message(current)

    assert new_state.active is False
    assert new_state.recovery_count == 0


def test_record_valid_message_is_noop_when_already_inactive():
    current = FailsafeState(active=False, recovery_count=0)

    new_state = record_valid_message(current)

    assert new_state == current


def test_build_discovery_config_returns_expected_shape():
    config = build_discovery_config("client1")

    assert config["unique_id"] == "heizungsbruecke_client1_failsafe"
    assert config["state_topic"] == "smartheat/client1/status/failsafe"
    assert config["payload_on"] == "ON"
    assert config["payload_off"] == "OFF"
    assert config["device_class"] == "problem"
    assert config["device"]["identifiers"] == ["heizungsbruecke_client1"]


def test_build_state_payload_on_when_active():
    assert build_state_payload(True) == "ON"


def test_build_state_payload_off_when_inactive():
    assert build_state_payload(False) == "OFF"
