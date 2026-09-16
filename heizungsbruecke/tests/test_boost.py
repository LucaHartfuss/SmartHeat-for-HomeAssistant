from heizungsbruecke.boost import decide_boost


def test_decide_boost_inactive_on_first_ever_tick_no_previous_target():
    decision = decide_boost(
        room_actual=18.0, room_target=20.0, previous_room_target=None,
        boost_was_active=False, arrival_threshold_k=0.5,
        boost_curve_value=1.5, boost_offset_value=30.0,
    )
    assert decision.active is False


def test_decide_boost_inactive_when_target_unchanged_even_if_room_cold():
    # Kernverhalten der Neudefinition: Kaelte OHNE Zielwert-Erhoehung loest KEIN
    # Boost mehr aus (Design-Spec Phase 5, Punkt 15 -- bewusst entfernte
    # Sicherheitsnetz-Eigenschaft).
    decision = decide_boost(
        room_actual=15.0, room_target=20.0, previous_room_target=20.0,
        boost_was_active=False, arrival_threshold_k=0.5,
        boost_curve_value=1.5, boost_offset_value=30.0,
    )
    assert decision.active is False


def test_decide_boost_activates_when_target_raised():
    decision = decide_boost(
        room_actual=19.0, room_target=21.0, previous_room_target=20.0,
        boost_was_active=False, arrival_threshold_k=0.5,
        boost_curve_value=1.5, boost_offset_value=30.0,
    )
    assert decision.active is True
    assert decision.curve_value == 1.5
    assert decision.offset_value == 30.0


def test_decide_boost_does_not_activate_for_tiny_float_noise_change():
    decision = decide_boost(
        room_actual=19.0, room_target=20.005, previous_room_target=20.0,
        boost_was_active=False, arrival_threshold_k=0.5,
        boost_curve_value=1.5, boost_offset_value=30.0,
    )
    assert decision.active is False


def test_decide_boost_stays_active_until_arrival_threshold_reached():
    # room_actual bewusst auf 19.4 gesetzt (nicht 19.6 wie im urspruenglichen Task-Brief):
    # mit arrival_threshold_k=0.5 muss der Abstand zum Ziel (hier 0.6) GROESSER als die
    # Schwelle sein, damit Boost noch nicht "angekommen" ist und aktiv bleibt -- bei 19.6
    # waere der Abstand (0.4) bereits kleiner als die Schwelle (0.5), das haette laut der
    # in Step 3 des Briefs vorgegebenen Formel (`room_actual < room_target -
    # arrival_threshold_k`) sofort zum Beenden gefuehrt und stand im Widerspruch zum
    # eigenen Testnamen sowie zu test_decide_boost_exits_once_within_arrival_threshold
    # direkt darunter.
    decision = decide_boost(
        room_actual=19.4, room_target=20.0, previous_room_target=20.0,
        boost_was_active=True, arrival_threshold_k=0.5,
        boost_curve_value=1.5, boost_offset_value=30.0,
    )
    assert decision.active is True


def test_decide_boost_exits_once_within_arrival_threshold():
    decision = decide_boost(
        room_actual=19.6, room_target=20.0, previous_room_target=20.0,
        boost_was_active=True, arrival_threshold_k=0.4,
        boost_curve_value=1.5, boost_offset_value=30.0,
    )
    assert decision.active is False


def test_decide_boost_stays_active_across_a_second_target_raise():
    # Zweite Erhoehung waehrend Boost bereits aktiv ist: kein Sonderfall noetig,
    # Exit-Pruefung vergleicht ohnehin gegen den AKTUELLEN room_target.
    decision = decide_boost(
        room_actual=20.5, room_target=22.0, previous_room_target=21.0,
        boost_was_active=True, arrival_threshold_k=0.5,
        boost_curve_value=1.5, boost_offset_value=30.0,
    )
    assert decision.active is True
