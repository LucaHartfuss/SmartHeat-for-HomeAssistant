from heizungsbruecke.failsafe import (
    FailsafeState,
    build_discovery_config,
    build_state_payload,
    enter_notbetrieb_on_ack_timeout,
    exit_notbetrieb_on_ack,
    register_publish_attempt,
)


def test_register_publish_attempt_sets_awaiting_seq():
    current = FailsafeState(active=False, awaiting_seq=None)

    new_state = register_publish_attempt(current, "seq-1")

    assert new_state == FailsafeState(active=False, awaiting_seq="seq-1")


def test_register_publish_attempt_supersedes_an_earlier_still_open_seq():
    current = FailsafeState(active=False, awaiting_seq="seq-1")

    new_state = register_publish_attempt(current, "seq-2")

    assert new_state.awaiting_seq == "seq-2"


def test_enter_notbetrieb_on_ack_timeout_activates_for_matching_seq():
    current = FailsafeState(active=False, awaiting_seq="seq-1")

    new_state = enter_notbetrieb_on_ack_timeout(current, "seq-1")

    # awaiting_seq stays populated so a late ack for this exact attempt still ends
    # Notbetrieb (final-review finding I1).
    assert new_state == FailsafeState(active=True, awaiting_seq="seq-1")


def test_late_ack_after_timeout_still_ends_notbetrieb():
    # Final-review finding I1: a transient stall just over the ack-timeout must not
    # leave a false Notbetrieb up until the next scheduled publish (up to ~24h).
    state = register_publish_attempt(FailsafeState(active=False, awaiting_seq=None), "seq-1")
    state = enter_notbetrieb_on_ack_timeout(state, "seq-1")
    assert state.active is True

    state = exit_notbetrieb_on_ack(state, "seq-1")

    assert state == FailsafeState(active=False, awaiting_seq=None)


def test_timer_firing_after_successful_ack_stays_noop():
    # Opposite race of the above: the ack resolved the attempt first (clearing
    # awaiting_seq), the timer for that same seq fires afterwards -- must not activate.
    state = register_publish_attempt(FailsafeState(active=False, awaiting_seq=None), "seq-1")
    state = exit_notbetrieb_on_ack(state, "seq-1")

    state = enter_notbetrieb_on_ack_timeout(state, "seq-1")

    assert state == FailsafeState(active=False, awaiting_seq=None)


def test_late_ack_for_superseded_attempt_after_timeout_is_ignored():
    # Only the latest attempt counts: once a newer publish replaced the timed-out seq,
    # a late ack for the old seq must not end Notbetrieb.
    state = register_publish_attempt(FailsafeState(active=False, awaiting_seq=None), "seq-1")
    state = enter_notbetrieb_on_ack_timeout(state, "seq-1")
    state = register_publish_attempt(state, "seq-2")

    state = exit_notbetrieb_on_ack(state, "seq-1")

    assert state == FailsafeState(active=True, awaiting_seq="seq-2")


def test_enter_notbetrieb_on_ack_timeout_noop_when_seq_already_acked():
    # awaiting_seq was already cleared by exit_notbetrieb_on_ack before the timer fired.
    current = FailsafeState(active=False, awaiting_seq=None)

    new_state = enter_notbetrieb_on_ack_timeout(current, "seq-1")

    assert new_state == current


def test_enter_notbetrieb_on_ack_timeout_noop_when_superseded_by_newer_publish():
    current = FailsafeState(active=False, awaiting_seq="seq-2")

    new_state = enter_notbetrieb_on_ack_timeout(current, "seq-1")

    assert new_state == current


def test_exit_notbetrieb_on_ack_deactivates_for_matching_seq():
    current = FailsafeState(active=True, awaiting_seq="seq-1")

    new_state = exit_notbetrieb_on_ack(current, "seq-1")

    assert new_state == FailsafeState(active=False, awaiting_seq=None)


def test_exit_notbetrieb_on_ack_noop_for_mismatched_seq():
    # A late ack for a superseded/old attempt must not resurrect or otherwise touch an
    # expectation that has already moved on to a newer seq.
    current = FailsafeState(active=True, awaiting_seq="seq-2")

    new_state = exit_notbetrieb_on_ack(current, "seq-1")

    assert new_state == current


def test_exit_notbetrieb_on_ack_still_clears_awaiting_seq_when_already_inactive():
    current = FailsafeState(active=False, awaiting_seq="seq-1")

    new_state = exit_notbetrieb_on_ack(current, "seq-1")

    assert new_state == FailsafeState(active=False, awaiting_seq=None)


def test_build_discovery_config_returns_expected_shape():
    config = build_discovery_config("client1")

    assert config["unique_id"] == "heizungsbruecke_client1_failsafe"
    assert config["state_topic"] == "smartheat/client1/status/failsafe"
    assert config["availability_topic"] == "smartheat/client1/status/availability"
    assert config["payload_on"] == "ON"
    assert config["payload_off"] == "OFF"
    assert config["device_class"] == "problem"
    assert config["device"]["identifiers"] == ["heizungsbruecke_client1"]


def test_build_state_payload_on_when_active():
    assert build_state_payload(True) == "ON"


def test_build_state_payload_off_when_inactive():
    assert build_state_payload(False) == "OFF"
