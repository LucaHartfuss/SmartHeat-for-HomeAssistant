"""Stand der Integration aus ihrem Lebenszeichen smartheat_integration: Version, Frist ohne Ereignis, Lesefenster."""
import pytest

from heizungsbruecke import geraet_status
from heizungsbruecke.integration_stand import FRIST_SECONDS, IntegrationStand

STATUS = geraet_status.EVENT_SCHEMA


def ereignis(version="0.4.2", schemas=(STATUS,), schema=1, **extra):
    return {"schema": schema, "version": version, "v5_schemas": list(schemas), **extra}


def test_nothing_heard_means_no_version():
    stand = IntegrationStand()
    assert stand.version(0.0) is None and not stand.veraltet(0.0)


def test_an_event_gives_the_version_until_the_deadline():
    stand = IntegrationStand()
    assert stand.ereignis(ereignis("0.4.2"), 100.0) is True
    assert stand.version(100.0) == "0.4.2"
    assert stand.version(100.0 + FRIST_SECONDS - 0.5) == "0.4.2"


def test_after_900_seconds_without_an_event_the_version_is_none():
    stand = IntegrationStand()
    stand.ereignis(ereignis(), 100.0)
    assert FRIST_SECONDS == 900
    assert stand.version(100.0 + FRIST_SECONDS) is None


def test_the_same_event_after_600_seconds_changes_nothing_but_restarts_the_deadline():
    stand = IntegrationStand()
    stand.ereignis(ereignis("0.4.2"), 0.0)
    assert stand.ereignis(ereignis("0.4.2"), 600.0) is False
    assert stand.version(600.0) == "0.4.2"
    assert stand.version(1499.0) == "0.4.2"  # 900 s ab dem zweiten Ereignis, nicht ab dem ersten
    assert stand.version(1500.0) is None


def test_a_new_version_is_a_change_and_an_event_after_expiry_too():
    stand = IntegrationStand()
    stand.ereignis(ereignis("0.4.2"), 0.0)
    assert stand.ereignis(ereignis("0.5.0"), 10.0) is True and stand.version(10.0) == "0.5.0"
    assert stand.ereignis(ereignis("0.5.0"), 5000.0) is True  # dazwischen war der Stand None
    assert stand.version(5000.0) == "0.5.0"


@pytest.mark.parametrize("daten", [
    None, [], "x", {}, ereignis(version=5), ereignis(version=None), ereignis(version=""), ereignis(version="a" * 33),
    ereignis(version="1.0 <b>"), ereignis(schema="1"), ereignis(schema=True), ereignis(schema=0),
    {"schema": 1, "version": "1.0"}, ereignis(schemas=()), ereignis(schemas=("4",)), ereignis(schemas=(True,)),
    ereignis(schemas=(0,)), ereignis(schemas=tuple(range(1, 40))),
])
def test_an_unreadable_event_changes_nothing(daten):
    stand = IntegrationStand()
    assert stand.ereignis(daten, 0.0) is False and stand.version(0.0) is None
    stand.ereignis(ereignis("0.4.2"), 1.0)
    assert stand.ereignis(daten, 2.0) is False
    assert stand.version(2.0) == "0.4.2"
    assert stand.version(1.0 + FRIST_SECONDS) is None  # und verlaengert die Frist nicht


def test_unknown_fields_and_a_foreign_event_schema_are_read_by_their_core():
    stand = IntegrationStand()
    assert stand.ereignis(ereignis("0.9.0", schema=7, neu={"x": 1}), 0.0) is True
    assert stand.version(0.0) == "0.9.0"


def test_integration_is_current_when_it_reads_the_schema_the_addon_sends():
    stand = IntegrationStand()
    for schemas in ((STATUS,), (STATUS - 1, STATUS), (STATUS, STATUS + 1), (STATUS - 2, STATUS)):
        stand.ereignis(ereignis(schemas=schemas), 0.0)
        assert not stand.veraltet(0.0), schemas


def test_an_integration_reading_only_the_previous_schema_cannot_read_the_addon_and_is_flagged():
    stand = IntegrationStand()
    stand.ereignis(ereignis(schemas=(STATUS - 1,)), 0.0)
    assert stand.veraltet(0.0)


def test_a_newer_integration_is_not_flagged():
    stand = IntegrationStand()
    for schemas in ((STATUS + 1,), (STATUS + 1, STATUS + 2)):
        stand.ereignis(ereignis(schemas=schemas), 0.0)
        assert not stand.veraltet(0.0), schemas


def test_an_integration_older_than_the_window_is_flagged():
    stand = IntegrationStand()
    stand.ereignis(ereignis(schemas=(STATUS - 2,)), 0.0)
    assert stand.veraltet(0.0) and stand.version(0.0) == "0.4.2"
    stand.ereignis(ereignis(schemas=(STATUS - 3, STATUS - 2)), 1.0)
    assert stand.veraltet(1.0)


def test_the_flag_follows_the_newest_event_and_ends_with_the_deadline():
    stand = IntegrationStand()
    stand.ereignis(ereignis(schemas=(STATUS - 2,)), 0.0)
    assert stand.ereignis(ereignis(schemas=(STATUS - 1, STATUS)), 10.0) is True
    assert not stand.veraltet(10.0)
    stand.ereignis(ereignis(schemas=(STATUS - 2,)), 20.0)
    assert not stand.veraltet(20.0 + FRIST_SECONDS)


def test_a_change_of_only_the_flag_is_a_change():
    stand = IntegrationStand()
    stand.ereignis(ereignis("0.4.2", schemas=(STATUS,)), 0.0)
    assert stand.ereignis(ereignis("0.4.2", schemas=(STATUS - 2,)), 1.0) is True
