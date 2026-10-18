import pytest
from geraet_helpers import ankunftsschwelle_der_laufzeit

from smartheat_core.emergency_boost import decide_emergency_boost


def test_decide_emergency_boost_inactive_when_room_within_trigger_threshold():
    decision = decide_emergency_boost(
        room_actual=19.2, room_target=20.0, emergency_was_active=False,
        exit_threshold_k=0.5,
    )
    assert decision is False


def test_decide_emergency_boost_inactive_exactly_at_trigger_threshold():
    # room_actual == room_target - 1.0 ist noch NICHT "mehr als 1.0 K drunter".
    decision = decide_emergency_boost(
        room_actual=19.0, room_target=20.0, emergency_was_active=False,
        exit_threshold_k=0.5,
    )
    assert decision is False


def test_decide_emergency_boost_triggers_when_room_more_than_1k_below_target():
    decision = decide_emergency_boost(
        room_actual=18.9, room_target=20.0, emergency_was_active=False,
        exit_threshold_k=0.5,
    )
    assert decision is True


def test_decide_emergency_boost_stays_active_above_exit_threshold():
    decision = decide_emergency_boost(
        room_actual=19.4, room_target=20.0, emergency_was_active=True,
        exit_threshold_k=0.5,
    )
    assert decision is True


def test_decide_emergency_boost_exits_once_within_exit_threshold():
    decision = decide_emergency_boost(
        room_actual=19.6, room_target=20.0, emergency_was_active=True,
        exit_threshold_k=0.4,
    )
    assert decision is False


@pytest.mark.parametrize(("raum_ist", "bleibt_aktiv"), [
    pytest.param(20.51, False, id="abstand-0.49"), pytest.param(20.5, False, id="abstand-0.5"),
    pytest.param(20.49, True, id="abstand-0.51"),
])
def test_notfall_boost_endet_an_der_echten_austrittsschwelle(tmp_path, raum_ist, bleibt_aktiv):
    """Austrittsschwelle = Ankunftsschwelle aus der echten RuntimeConfig (Vaillant x Heizkoerper, 0,5 K; so reicht sie
    regulation.run_local_check weiter): beim Soll 21,0 bleibt ein laufender Notfall-Boost nur bei ist < soll - 0,5
    aktiv; bei 0,49 und genau 0,5 K Abstand endet er."""
    decision = decide_emergency_boost(
        room_actual=raum_ist, room_target=21.0, emergency_was_active=True,
        exit_threshold_k=ankunftsschwelle_der_laufzeit(tmp_path),
    )
    assert decision is bleibt_aktiv


def test_decide_emergency_boost_retriggers_after_a_prior_exit():
    # Solange Notbetrieb laeuft, kann der Zyklus (Ausloesen -> Erreichen -> erneutes
    # Auskuehlen -> erneutes Ausloesen) beliebig oft wiederholen (Design-Spec 2026-09-23).
    first = decide_emergency_boost(
        room_actual=19.6, room_target=20.0, emergency_was_active=True,
        exit_threshold_k=0.4,
    )
    assert first is False

    second = decide_emergency_boost(
        room_actual=18.8, room_target=20.0, emergency_was_active=False,
        exit_threshold_k=0.4,
    )
    assert second is True
