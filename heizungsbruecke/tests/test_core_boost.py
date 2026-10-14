from datetime import UTC, datetime, timedelta

import pytest

from smartheat_core import boost
from smartheat_core.boost import COMFORT_BOOST_MAX_SECONDS, comfort_boost_expired, decide_boost


def test_decide_boost_inactive_on_first_ever_tick_no_previous_target():
    decision = decide_boost(
        room_actual=18.0, room_target=20.0, previous_room_target=None,
        boost_was_active=False, arrival_threshold_k=0.5,
    )
    assert decision is False


def test_decide_boost_inactive_when_target_unchanged_even_if_room_cold():
    # Kernverhalten der Neudefinition: Kaelte OHNE Zielwert-Erhoehung loest KEIN
    # Boost mehr aus (Design-Spec Phase 5, Punkt 15 -- bewusst entfernte
    # Sicherheitsnetz-Eigenschaft).
    decision = decide_boost(
        room_actual=15.0, room_target=20.0, previous_room_target=20.0,
        boost_was_active=False, arrival_threshold_k=0.5,
    )
    assert decision is False


def test_decide_boost_activates_when_target_raised():
    decision = decide_boost(
        room_actual=19.0, room_target=21.0, previous_room_target=20.0,
        boost_was_active=False, arrival_threshold_k=0.5,
    )
    assert decision is True


def test_decide_boost_does_not_activate_for_tiny_float_noise_change():
    decision = decide_boost(
        room_actual=19.0, room_target=20.005, previous_room_target=20.0,
        boost_was_active=False, arrival_threshold_k=0.5,
    )
    assert decision is False


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
    )
    assert decision is True


def test_decide_boost_exits_once_within_arrival_threshold():
    decision = decide_boost(
        room_actual=19.6, room_target=20.0, previous_room_target=20.0,
        boost_was_active=True, arrival_threshold_k=0.4,
    )
    assert decision is False


def test_decide_boost_stays_active_across_a_second_target_raise():
    # Zweite Erhoehung waehrend Boost bereits aktiv ist: kein Sonderfall noetig,
    # Exit-Pruefung vergleicht ohnehin gegen den AKTUELLEN room_target.
    decision = decide_boost(
        room_actual=20.5, room_target=22.0, previous_room_target=21.0,
        boost_was_active=True, arrival_threshold_k=0.5,
    )
    assert decision is True


@pytest.mark.parametrize(("vorher", "nachher", "boost"), [
    (20.5, 21.0, True),     # ein Thermostat-Schritt 0,5 K
    (20.5, 20.6, True),     # 0,1-K-Thermostat
    (20.5, 20.509, False),  # knapp unter der Schwelle 0,01 K
    (20.5, 20.5, False),
    (20.5, 20.0, False),    # Senkung
])
def test_kleinste_reale_erhoehung_startet_den_boost(vorher, nachher, boost):
    """A5-07."""
    assert decide_boost(room_actual=20.0, room_target=nachher, previous_room_target=vorher,
                        boost_was_active=False, arrival_threshold_k=0.5) is boost


# --- Mutationsprobe Stufe 3 S3 (Task 11) ------------------------------------------------------------------------

def test_a_rise_of_exactly_the_epsilon_does_not_start_the_boost():
    """Mutationsprobe (Mutant 8): die Schwelle gilt strikt (>), eine Erhoehung genau um den Epsilon-Wert startet nichts."""
    vorher = 20.0
    nachher = vorher + boost._TARGET_RISE_EPSILON_K
    assert decide_boost(room_actual=19.0, room_target=nachher, previous_room_target=vorher,
                        boost_was_active=False, arrival_threshold_k=0.5) is False


_BEGINN = datetime(2026, 10, 8, 6, 0, tzinfo=UTC)


@pytest.mark.parametrize(("dauer", "abgelaufen"), [
    (timedelta(0), False),
    (timedelta(seconds=COMFORT_BOOST_MAX_SECONDS - 1), False),
    (timedelta(seconds=COMFORT_BOOST_MAX_SECONDS), True),  # genau 4 h: abgelaufen (>=)
    (timedelta(seconds=COMFORT_BOOST_MAX_SECONDS + 1), True),
])
def test_comfort_boost_expires_exactly_at_the_maximum_duration(dauer, abgelaufen):
    """Mutationsprobe (Mutanten 14, 17, 18): Vorzeichen, Grenze (>=) und Rueckgabe der Hoechstdauer."""
    assert comfort_boost_expired(_BEGINN, _BEGINN + dauer) is abgelaufen


def test_an_unknown_start_never_expires():
    assert comfort_boost_expired(None, _BEGINN) is False
