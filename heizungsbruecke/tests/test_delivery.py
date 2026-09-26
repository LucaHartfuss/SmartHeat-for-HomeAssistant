from dataclasses import replace

import pytest

from heizungsbruecke.delivery import (
    NOTIFY_DATENFEHLER_LOCAL,
    NOTIFY_DATENFEHLER_RESOLVED,
    NOTIFY_DATENFEHLER_SERVER,
    NOTIFY_NOTBETRIEB_OFF,
    NOTIFY_NOTBETRIEB_ON,
    PHASE_AWAITING_ACK,
    PHASE_QUERYING_ENTITLEMENT,
    PHASE_SENDING,
    PHASE_WAITING_RETRY,
    SOURCE_LOCAL,
    SOURCE_SERVER,
    Ack,
    AckTimeout,
    Attempt,
    Boot,
    DataFault,
    DeliveryState,
    EndEmergencyBoost,
    EnterAboInactive,
    EntitlementChecked,
    Notify,
    PendingTick,
    PublishFailsafe,
    Published,
    QueryEntitlement,
    ReadInvalid,
    RetryDue,
    ScheduleAckTimeout,
    ScheduleRetry,
    TickDue,
    accepts_ack,
    build_discovery_config,
    build_state_payload,
    from_persisted,
    notification_text,
    step,
    to_persisted,
)


def _run(state, *events):
    """Wendet mehrere Ereignisse nacheinander an, gibt Endzustand und die Aktionen des
    LETZTEN Ereignisses zurueck."""
    actions = []
    for event in events:
        state, actions = step(state, event)
    return state, actions


def _published(seq="s1", trigger="daily", **state_kwargs):
    """Zustand direkt nach dem ersten Publish eines Ticks (gen=1, Ack-Timeout geplant)."""
    return _run(DeliveryState(**state_kwargs), TickDue(seq, trigger), Published(seq))


def _in_notbetrieb(seq="s1"):
    """Publish -> Timeout -> Sofort-Retry -> Publish -> Timeout -> Abo aktiv = Notbetrieb."""
    state, _ = _published(seq)
    state, _ = _run(
        state,
        AckTimeout(seq, 1), RetryDue(seq, 2), Published(seq), AckTimeout(seq, 3),
    )
    return _run(state, EntitlementChecked(seq, "active"))


# --- Tick entsteht / wird ersetzt ---

def test_tick_due_creates_pending_and_attempts():
    state, actions = step(DeliveryState(), TickDue("s1", "daily"))

    assert state.pending == PendingTick(seq="s1", trigger="daily")
    assert actions == [Attempt("s1", "daily")]


def test_tick_due_replaces_open_tick_but_keeps_failures_notbetrieb_and_fault():
    fault = DataFault(SOURCE_LOCAL, ("dat",))
    old = DeliveryState(
        pending=PendingTick("s1", "daily", stage=3, phase=PHASE_AWAITING_ACK, gen=7),
        server_failures=4, notbetrieb=True, datenfehler=fault,
    )

    state, actions = step(old, TickDue("s2", "target_change"))

    assert state == DeliveryState(
        pending=PendingTick("s2", "target_change"), server_failures=4, notbetrieb=True, datenfehler=fault,
    )
    assert actions == [Attempt("s2", "target_change")]


def test_replaced_tick_makes_old_timeouts_retries_and_acks_ineffective():
    state, _ = _published("s1")
    state, _ = step(state, TickDue("s2", "target_change"))

    for event in (AckTimeout("s1", 1), RetryDue("s1", 1), Ack("s1", "ok")):
        new_state, actions = step(state, event)
        assert (new_state, actions) == (state, [])


# --- Publish und Ack ---

def test_published_arms_ack_timeout():
    state, actions = _published("s1")

    assert state.pending == PendingTick("s1", "daily", stage=0, phase=PHASE_AWAITING_ACK, gen=1)
    assert actions == [ScheduleAckTimeout("s1", 1, 30)]


def test_published_for_foreign_seq_is_ignored():
    state, _ = step(DeliveryState(), TickDue("s1", "daily"))

    assert step(state, Published("fremd")) == (state, [])


@pytest.mark.parametrize("status", ["ok", "skipped_summer"])
def test_successful_ack_clears_pending_without_actions(status):
    state, _ = _published("s1", server_failures=1)

    state, actions = step(state, Ack("s1", status))

    assert state == DeliveryState()
    assert actions == []


def test_ack_with_foreign_seq_or_without_pending_is_ignored():
    state, _ = _published("s1")

    assert step(state, Ack("fremd", "ok")) == (state, [])
    assert step(DeliveryState(), Ack("s1", "ok")) == (DeliveryState(), [])


def test_accepts_ack_only_for_open_seq():
    state, _ = _published("s1")

    assert accepts_ack(state, "s1") is True
    assert accepts_ack(state, "s2") is False
    assert accepts_ack(DeliveryState(), "s1") is False


# --- Server-Timeouts, Retries, Notbetrieb ---

def test_first_ack_timeout_retries_immediately_without_notbetrieb():
    state, _ = _published("s1")

    state, actions = step(state, AckTimeout("s1", 1))

    assert state.server_failures == 1
    assert state.notbetrieb is False
    assert state.pending == PendingTick("s1", "daily", stage=1, phase=PHASE_WAITING_RETRY, gen=2)
    assert actions == [ScheduleRetry("s1", 2, 0)]


def test_stale_ack_timeout_is_ignored():
    state, _ = _published("s1")

    assert step(state, AckTimeout("s1", 0)) == (state, [])


def test_retry_due_attempts_same_seq():
    state, _ = _published("s1")
    state, _ = step(state, AckTimeout("s1", 1))

    new_state, actions = step(state, RetryDue("s1", 2))

    assert new_state.pending == replace(state.pending, phase=PHASE_SENDING)
    assert actions == [Attempt("s1", "daily")]


def test_stale_retry_due_is_ignored():
    state, _ = _published("s1")
    state, _ = step(state, AckTimeout("s1", 1))

    assert step(state, RetryDue("s1", 1)) == (state, [])
    assert step(state, RetryDue("fremd", 2)) == (state, [])


def test_second_consecutive_timeout_queries_entitlement_before_notbetrieb():
    state, _ = _published("s1")

    state, actions = _run(state, AckTimeout("s1", 1), RetryDue("s1", 2), Published("s1"), AckTimeout("s1", 3))

    assert state.server_failures == 2
    assert state.notbetrieb is False
    assert actions == [QueryEntitlement("s1")]


@pytest.mark.parametrize("status", ["active", "unknown"])
def test_entitlement_not_inactive_starts_notbetrieb_and_retries_after_five_minutes(status):
    state, _ = _published("s1")
    state, _ = _run(state, AckTimeout("s1", 1), RetryDue("s1", 2), Published("s1"), AckTimeout("s1", 3))

    state, actions = step(state, EntitlementChecked("s1", status))

    assert state.notbetrieb is True
    assert state.pending.stage == 2
    assert actions == [
        PublishFailsafe(True), Notify(NOTIFY_NOTBETRIEB_ON), ScheduleRetry("s1", state.pending.gen, 300),
    ]


def test_entitlement_inactive_enters_abo_mode_without_retry():
    state, _ = _published("s1")
    state, _ = _run(state, AckTimeout("s1", 1), RetryDue("s1", 2), Published("s1"), AckTimeout("s1", 3))

    state, actions = step(state, EntitlementChecked("s1", "inactive"))

    assert state.pending is None
    assert state.notbetrieb is True
    assert actions == [EnterAboInactive()]


def test_entitlement_result_for_foreign_seq_is_ignored():
    state, _ = _published("s1")

    assert step(state, EntitlementChecked("fremd", "active")) == (state, [])


def test_ack_after_five_minute_retry_ends_notbetrieb():
    state, _ = _in_notbetrieb("s1")
    gen = state.pending.gen

    state, actions = _run(state, RetryDue("s1", gen), Published("s1"), Ack("s1", "ok"))

    assert state == DeliveryState()
    assert actions == [PublishFailsafe(False), EndEmergencyBoost(), Notify(NOTIFY_NOTBETRIEB_OFF)]


def test_server_retry_delays_follow_five_fifteen_sixty_then_hourly():
    state, _ = _in_notbetrieb("s1")
    delays = []
    for _ in range(4):
        gen = state.pending.gen
        state, _ = _run(state, RetryDue("s1", gen), Published("s1"))
        state, actions = step(state, AckTimeout("s1", state.pending.gen))
        delays.append(actions[-1].delay_s)

    assert delays == [900, 3600, 3600, 3600]  # 300 s kam schon beim Notbetriebsbeginn
    assert state.server_failures == 6


def test_timeout_during_notbetrieb_does_not_query_entitlement_again():
    state, _ = _in_notbetrieb("s1")
    state, _ = _run(state, RetryDue("s1", state.pending.gen), Published("s1"))

    state, actions = step(state, AckTimeout("s1", state.pending.gen))

    assert [type(action) for action in actions] == [ScheduleRetry]


def test_late_ack_during_retry_wait_ends_tick_and_old_retry_becomes_stale():
    # Review Focus 3: Antwort des ersten Versuchs kommt kurz nach dem Ack-Timeout.
    state, _ = _published("s1")
    state, _ = step(state, AckTimeout("s1", 1))

    state, actions = step(state, Ack("s1", "ok"))

    assert state == DeliveryState()
    assert actions == []
    assert step(state, RetryDue("s1", 2)) == (state, [])


def test_late_ack_after_notbetrieb_start_ends_notbetrieb():
    state, _ = _in_notbetrieb("s1")
    pending_gen = state.pending.gen

    state, actions = step(state, Ack("s1", "ok"))

    assert state.notbetrieb is False
    assert state.pending is None
    assert Notify(NOTIFY_NOTBETRIEB_OFF) in actions
    assert step(state, RetryDue("s1", pending_gen)) == (state, [])


# --- Datenfehler lokal ---

def test_read_invalid_sets_local_fault_notifies_and_retries_after_thirty_seconds():
    state, _ = step(DeliveryState(server_failures=1), TickDue("s1", "daily"))

    state, actions = step(state, ReadInvalid("s1", ("room_actual", "dat")))

    assert state.datenfehler == DataFault(SOURCE_LOCAL, ("dat", "room_actual"))
    assert state.server_failures == 1  # nur eine Server-Antwort setzt zurueck
    assert actions == [Notify(NOTIFY_DATENFEHLER_LOCAL, ("dat", "room_actual")), ScheduleRetry("s1", 1, 30)]


def test_read_invalid_with_same_roles_does_not_notify_again():
    state, _ = _run(DeliveryState(), TickDue("s1", "daily"), ReadInvalid("s1", ("dat",)))

    state, actions = _run(state, RetryDue("s1", 1), ReadInvalid("s1", ("dat",)))

    assert actions == [ScheduleRetry("s1", 2, 300)]


def test_read_invalid_with_changed_roles_notifies_again():
    state, _ = _run(DeliveryState(), TickDue("s1", "daily"), ReadInvalid("s1", ("dat",)))

    state, actions = _run(state, RetryDue("s1", 1), ReadInvalid("s1", ("dat", "dart")))

    assert actions[0] == Notify(NOTIFY_DATENFEHLER_LOCAL, ("dart", "dat"))


def test_read_invalid_for_foreign_seq_is_ignored():
    state, _ = step(DeliveryState(), TickDue("s1", "daily"))

    assert step(state, ReadInvalid("fremd", ("dat",))) == (state, [])


def test_fault_resolved_on_successful_ack():
    state, _ = _run(DeliveryState(), TickDue("s1", "daily"), ReadInvalid("s1", ("dat",)))

    state, actions = _run(state, RetryDue("s1", 1), Published("s1"), Ack("s1", "ok"))

    assert state == DeliveryState()
    assert actions == [Notify(NOTIFY_DATENFEHLER_RESOLVED)]


def test_local_fault_during_notbetrieb_keeps_notbetrieb():
    state, _ = _in_notbetrieb("s1")

    state, _ = _run(state, TickDue("s2", "daily"), ReadInvalid("s2", ("dat",)))

    assert state.notbetrieb is True
    assert state.datenfehler == DataFault(SOURCE_LOCAL, ("dat",))


# --- Datenfehler vom Server (rejected) ---

def test_rejected_ack_is_server_fault_ends_notbetrieb_and_retries():
    state, _ = _in_notbetrieb("s1")
    state, _ = _run(state, RetryDue("s1", state.pending.gen), Published("s1"))
    gen = state.pending.gen
    reason = "unplausibler Wert für dat: 99 (erlaubt -40–45)"

    state, actions = step(state, Ack("s1", "rejected", reason))

    assert state.notbetrieb is False
    assert state.server_failures == 0
    assert state.datenfehler == DataFault(SOURCE_SERVER, (reason,))
    assert actions == [
        PublishFailsafe(False), EndEmergencyBoost(), Notify(NOTIFY_NOTBETRIEB_OFF),
        Notify(NOTIFY_DATENFEHLER_SERVER, (reason,)), ScheduleRetry("s1", gen + 1, 900),
    ]


def test_rejected_with_same_reason_does_not_notify_again():
    state, _ = _published("s1")
    state, _ = step(state, Ack("s1", "rejected", "fehlende Rolle: dat"))

    state, actions = _run(state, RetryDue("s1", 2), Published("s1"), Ack("s1", "rejected", "fehlende Rolle: dat"))

    assert actions == [ScheduleRetry("s1", 4, 300)]


def test_rejected_with_only_the_value_changed_does_not_notify_or_change_state_again():
    """Der R4-Grund des Servers enthaelt den Messwert; ein driftender Wert ist dieselbe
    Stoerung. Keine zweite Meldung, und der gespeicherte Fehler (erste Begruendung)
    bleibt unveraendert -- sonst wuerde failsafe_state.json bei jedem Retry neu
    geschrieben."""
    first = "unplausibler Wert für dat: 99 (erlaubt -40–45)"
    state, _ = _published("s1")
    state, first_actions = step(state, Ack("s1", "rejected", first))
    persisted = to_persisted(state)

    state, actions = _run(
        state, RetryDue("s1", 2), Published("s1"), Ack("s1", "rejected", "unplausibler Wert für dat: 99.5 (erlaubt -40–45)"),
    )

    assert first_actions[0] == Notify(NOTIFY_DATENFEHLER_SERVER, (first,))
    assert actions == [ScheduleRetry("s1", 4, 300)]
    assert state.datenfehler == DataFault(SOURCE_SERVER, (first,))
    assert to_persisted(state) == persisted


def test_rejected_for_another_role_notifies_with_full_reason():
    state, _ = _published("s1")
    state, _ = step(state, Ack("s1", "rejected", "unplausibler Wert für dat: 99 (erlaubt -40–45)"))
    other = "unplausibler Wert für dart: 60 (erlaubt 5–35)"

    state, actions = _run(state, RetryDue("s1", 2), Published("s1"), Ack("s1", "rejected", other))

    assert actions == [Notify(NOTIFY_DATENFEHLER_SERVER, (other,)), ScheduleRetry("s1", 4, 300)]
    assert state.datenfehler == DataFault(SOURCE_SERVER, (other,))


def test_rejected_after_local_fault_with_same_text_is_a_new_fault():
    state, _ = _run(DeliveryState(), TickDue("s1", "daily"), ReadInvalid("s1", ("dat",)))

    state, actions = _run(state, RetryDue("s1", 1), Published("s1"), Ack("s1", "rejected", "dat"))

    assert actions[0] == Notify(NOTIFY_DATENFEHLER_SERVER, ("dat",))


def test_rejected_without_reason_uses_placeholder():
    state, _ = _published("s1")

    state, actions = step(state, Ack("s1", "rejected"))

    assert state.datenfehler == DataFault(SOURCE_SERVER, ("ohne Begründung",))
    assert actions[0] == Notify(NOTIFY_DATENFEHLER_SERVER, ("ohne Begründung",))


def test_rejected_retry_first_waits_thirty_seconds():
    state, _ = _published("s1")

    _, actions = step(state, Ack("s1", "rejected", "x"))

    assert actions[-1] == ScheduleRetry("s1", 2, 30)


# --- Phasen und doppelte/verspaetete Antworten (F2) ---

def _querying_entitlement(seq="s1"):
    """Zwei Ack-Timeouts in Folge: die Abo-Abfrage laeuft."""
    state, _ = _published(seq)
    return _run(state, AckTimeout(seq, 1), RetryDue(seq, 2), Published(seq), AckTimeout(seq, 3))


def test_pending_tick_moves_through_phases():
    state, _ = step(DeliveryState(), TickDue("s1", "daily"))
    assert state.pending.phase == PHASE_SENDING

    state, _ = step(state, Published("s1"))
    assert state.pending.phase == PHASE_AWAITING_ACK

    state, _ = step(state, AckTimeout("s1", 1))
    assert state.pending.phase == PHASE_WAITING_RETRY

    state, _ = step(state, RetryDue("s1", 2))
    assert state.pending.phase == PHASE_SENDING


def test_second_timeout_waits_for_entitlement_in_its_own_phase():
    state, actions = _querying_entitlement()

    assert state.pending.phase == PHASE_QUERYING_ENTITLEMENT
    assert actions == [QueryEntitlement("s1")]


def test_duplicate_rejected_answer_plans_no_second_retry():
    state, _ = _published("s1")
    state, _ = step(state, Ack("s1", "rejected", "x"))

    assert step(state, Ack("s1", "rejected", "x")) == (state, [])


def test_late_rejected_answer_during_server_retry_wait_keeps_the_planned_retry():
    state, _ = _published("s1")
    state, _ = step(state, AckTimeout("s1", 1))  # Retry geplant, server_failures = 1

    new_state, actions = step(state, Ack("s1", "rejected", "x"))

    assert actions == [Notify(NOTIFY_DATENFEHLER_SERVER, ("x",))]
    assert new_state.pending == state.pending
    assert new_state.server_failures == 0


def test_late_rejected_answer_after_notbetrieb_start_ends_notbetrieb_without_new_retry():
    state, _ = _in_notbetrieb("s1")

    new_state, actions = step(state, Ack("s1", "rejected", "x"))

    assert new_state.notbetrieb is False
    assert actions == [
        PublishFailsafe(False), EndEmergencyBoost(), Notify(NOTIFY_NOTBETRIEB_OFF),
        Notify(NOTIFY_DATENFEHLER_SERVER, ("x",)),
    ]
    assert new_state.pending == state.pending


@pytest.mark.parametrize("status", ["active", "unknown", "inactive"])
def test_answer_during_entitlement_query_discards_the_query_result(status):
    state, _ = _querying_entitlement()
    state, actions = step(state, Ack("s1", "rejected", "x"))

    assert actions == [Notify(NOTIFY_DATENFEHLER_SERVER, ("x",))]
    assert state.pending.phase == PHASE_QUERYING_ENTITLEMENT

    state, actions = step(state, EntitlementChecked("s1", status))

    assert state.notbetrieb is False
    assert state.pending is not None
    assert actions == [ScheduleRetry("s1", 4, 300)]


@pytest.mark.parametrize("events", [
    (TickDue("s1", "daily"),),                                   # sending
    (TickDue("s1", "daily"), Published("s1")),                   # awaiting_ack
])
def test_retry_due_outside_waiting_retry_is_ignored(events):
    state, _ = _run(DeliveryState(), *events)

    assert step(state, RetryDue("s1", state.pending.gen)) == (state, [])


# --- Neustart ---

def test_boot_with_persisted_pending_attempts_same_seq_from_stage_zero():
    state = DeliveryState(pending=PendingTick("alt-1", "daily"), notbetrieb=True)

    new_state, actions = step(state, Boot())

    assert new_state.pending == PendingTick("alt-1", "daily")
    assert actions == [Attempt("alt-1", "daily")]


def test_boot_without_pending_does_nothing():
    assert step(DeliveryState(notbetrieb=True), Boot()) == (DeliveryState(notbetrieb=True), [])


def test_unknown_event_raises_type_error():
    with pytest.raises(TypeError):
        step(DeliveryState(), object())


# --- Persistenz ---

def test_persisted_view_contains_only_durable_fields():
    state = DeliveryState(
        pending=PendingTick("s1", "target_change", stage=3, phase=PHASE_AWAITING_ACK, gen=9),
        server_failures=5, notbetrieb=True, datenfehler=DataFault(SOURCE_LOCAL, ("dat", "dart")),
    )

    assert to_persisted(state) == {
        "failsafe_active": True,
        "datenfehler": {"source": "local", "detail": ["dat", "dart"]},
        "pending": {"seq": "s1", "trigger": "target_change"},
    }


def test_persisted_round_trip_resets_volatile_fields():
    state = DeliveryState(
        pending=PendingTick("s1", "daily", stage=2, phase=PHASE_AWAITING_ACK, gen=4),
        server_failures=3, notbetrieb=True, datenfehler=DataFault(SOURCE_SERVER, ("grund",)),
    )

    assert from_persisted(to_persisted(state)) == DeliveryState(
        pending=PendingTick("s1", "daily"), notbetrieb=True, datenfehler=DataFault(SOURCE_SERVER, ("grund",)),
    )


def test_old_file_with_only_failsafe_active_is_read():
    assert from_persisted({"failsafe_active": True}) == DeliveryState(notbetrieb=True)


@pytest.mark.parametrize("raw", [
    [], "x", None, {},
    {"failsafe_active": "yes"},
    {"pending": {"seq": "", "trigger": "daily"}},
    {"pending": {"seq": "s1", "trigger": "weekly"}},
    {"pending": {"seq": "s1", "trigger": ["daily"]}},
    {"pending": "s1"},
    {"datenfehler": {"source": "irgendwo", "detail": ["dat"]}},
    {"datenfehler": {"source": "local", "detail": "dat"}},
    {"datenfehler": {"source": "local", "detail": [1]}},
])
def test_garbage_persisted_content_falls_back_to_defaults(raw):
    # Review Focus 4: kaputte/fremde Datei darf den Start nie verhindern.
    assert from_persisted(raw) == DeliveryState()


# --- Meldungstexte ---

def test_notification_text_for_local_fault_lists_roles_with_entities():
    text = notification_text(
        NOTIFY_DATENFEHLER_LOCAL, ("dat", "room_actual"),
        {"dat": "sensor.dat", "room_actual": "sensor.wz"},
    )

    assert text == (
        "Heizungsbrücke: Sensor(en) ohne gültigen Wert: dat (sensor.dat), room_actual (sensor.wz). "
        "Die Heizkurve bleibt unverändert, bis die Werte wieder verfügbar sind (z. B. Batterie prüfen)."
    )


def test_notification_text_for_unmapped_role_says_so():
    assert "dat (nicht zugeordnet)" in notification_text(NOTIFY_DATENFEHLER_LOCAL, ("dat",), {})


def test_notification_texts_for_server_fault_and_state_changes():
    assert notification_text(NOTIFY_DATENFEHLER_SERVER, ("fehlende Rolle: dat",), {}) == (
        "Heizungsbrücke: Server hat die Messwerte abgelehnt (fehlende Rolle: dat). Die Heizkurve bleibt unverändert."
    )
    assert notification_text(NOTIFY_NOTBETRIEB_ON, (), {}) == (
        "Heizungsbrücke: Server antwortet nicht, Notbetrieb aktiv. Die Heizung wird bei Bedarf lokal abgesichert."
    )
    assert notification_text(NOTIFY_NOTBETRIEB_OFF, (), {}) == (
        "Heizungsbrücke: Serververbindung wiederhergestellt, Notbetrieb beendet."
    )
    assert notification_text(NOTIFY_DATENFEHLER_RESOLVED, (), {}) == (
        "Heizungsbrücke: Messwerte wieder gültig, Heizkurve wird wieder angepasst."
    )


# --- MQTT-Discovery (aus failsafe.py uebernommen) ---

def test_build_discovery_config_describes_problem_binary_sensor():
    config = build_discovery_config("t1")

    assert config["unique_id"] == "heizungsbruecke_t1_failsafe"
    assert config["state_topic"] == "smartheat/t1/status/failsafe"
    assert config["availability_topic"] == "smartheat/t1/status/availability"
    assert (config["payload_on"], config["payload_off"], config["device_class"]) == ("ON", "OFF", "problem")
    assert config["device"]["identifiers"] == ["heizungsbruecke_t1"]


def test_build_state_payload_maps_bool_to_on_off():
    assert build_state_payload(True) == "ON"
    assert build_state_payload(False) == "OFF"
