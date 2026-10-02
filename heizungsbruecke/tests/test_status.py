"""Status-Kanal des Add-ons (Spec TP7 1.1, 3.1)."""
import logging
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from heizungsbruecke import status
from heizungsbruecke.delivery import DataFault, DeliveryState
from heizungsbruecke.state import BridgeState
from heizungsbruecke.status import ADDON_VERSION, Flags, StatusReporter, build_event, overall_status

SINCE = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
OVERRIDE = {"levers": {"curve": 1.3, "room_setpoint": 24.5, "heat_limit": 16.0}, "erkannt": "2026-10-01T08:00:00+02:00"}


def test_contract_values_match_the_integration():
    # Gleiche Werte wie const.py der Integration (Contract-Check 14).
    assert (status.EVENT_TYPE, status.EVENT_SCHEMA, status.HEARTBEAT_SECONDS) == ("smartheat_status", 1, 300)
    assert status.STATUS_VALUES == (
        "startet", "regelt", "konfigurationsfehler", "zugang_abgelehnt", "abo_beendet", "abo_inaktiv",
        "notbetrieb", "datenfehler", "abgemeldet",
    )
    assert status.BOOST_VALUES == ("keiner", "komfort", "notfall")
    assert status.ABO_VALUES == ("aktiv", "inaktiv", "beendet", "unbekannt")
    assert status.DATENFEHLER_ARTEN == ("lokal", "server", "anlage")
    assert status.HINT_FIELDS == ("raumfuehler_ausgefallen", "batterie_niedrig", "manueller_eingriff", "waerme_fehlt")
    assert status.EVENT_FIELDS == (
        "schema", "tenant_id", "setup_id", "addon_version", "status", "grund", "notbetrieb", "datenfehler",
        "boost", "letzte_serverantwort", "kurve", "parallelverschiebung", "mindestvorlauf",
        "heizgrenze", "abo", "abo_frist_ende", "hinweise",
    )


@pytest.mark.parametrize("flags,state,expected", [
    (Flags(abgemeldet=True, konfigurationsfehler=True, zugang_abgelehnt=True, abo_beendet=True, gestartet=True),
     BridgeState(delivery=DeliveryState(notbetrieb=True)), "abgemeldet"),
    (Flags(konfigurationsfehler=True, zugang_abgelehnt=True, abo_beendet=True), BridgeState(), "konfigurationsfehler"),
    (Flags(zugang_abgelehnt=True, abo_beendet=True), BridgeState(), "zugang_abgelehnt"),
    (Flags(zugang_abgelehnt=True, gestartet=True), BridgeState(abo_inactive_since=SINCE), "abo_inaktiv"),
    (Flags(abo_beendet=True), BridgeState(), "abo_beendet"),
    (Flags(), BridgeState(abo_finished=True, abo_inactive_since=SINCE), "abo_beendet"),
    (Flags(), BridgeState(abo_inactive_since=SINCE, delivery=DeliveryState(notbetrieb=True)), "abo_inaktiv"),
    # Notbetrieb/Datenfehler vor dem ersten MQTT-Connect (Tunnel nie aufgebaut): sichtbar statt
    # "startet" (TP7-Gates 2026-09-29).
    (Flags(), BridgeState(delivery=DeliveryState(notbetrieb=True)), "notbetrieb"),
    (Flags(), BridgeState(delivery=DeliveryState(datenfehler=DataFault("local", ("dat",)))), "datenfehler"),
    (Flags(), BridgeState(), "startet"),
    (Flags(gestartet=True), BridgeState(delivery=DeliveryState(
        notbetrieb=True, datenfehler=DataFault("local", ("dat",)))), "notbetrieb"),
    (Flags(gestartet=True), BridgeState(delivery=DeliveryState(datenfehler=DataFault("server", ("x",)))), "datenfehler"),
    (Flags(gestartet=True), BridgeState(), "regelt"),
])
def test_overall_status_takes_the_first_matching_state(flags, state, expected):
    assert overall_status(flags, state) == expected


def test_event_carries_every_field():
    state = BridgeState(
        restore_point={"curve": 0.9, "room_setpoint": 22.0}, emergency_boost_active=True,
        last_ack_at="2026-10-01T12:00:05+02:00", manual_override=OVERRIDE,
        notify_states={
            "raumfuehler:sensor.b": "ausgefallen", "raumfuehler:sensor.a": "ausgefallen",
            "batterie:sensor.x": "niedrig", "notbetrieb": "aktiv",
        },
    )

    event = build_event("client1", "abc", Flags(gestartet=True, abo="aktiv"), state)

    assert tuple(event) == status.EVENT_FIELDS
    assert event == {
        "schema": 1, "tenant_id": "client1", "setup_id": "abc", "addon_version": status.ADDON_VERSION,
        "status": "regelt", "grund": None, "notbetrieb": False, "datenfehler": None, "boost": "notfall",
        "letzte_serverantwort": "2026-10-01T12:00:05+02:00", "kurve": 0.9, "parallelverschiebung": 22.0,
        "mindestvorlauf": None, "heizgrenze": None, "abo": "aktiv", "abo_frist_ende": None,
        "hinweise": {
            "raumfuehler_ausgefallen": ["sensor.a", "sensor.b"], "batterie_niedrig": ["sensor.x"],
            "manueller_eingriff": {
                "kurve": 1.3, "parallelverschiebung": 24.5, "erkannt": "2026-10-01T08:00:00+02:00",
            },
            "waerme_fehlt": None,
        },
    }


def test_event_carries_parallel_shift_and_min_flow(make_store):
    store = make_store(backup={"restore_point": {"curve": 1.05, "room_setpoint": 21.0}})
    store.update(min_flow_current=20.5, restore_point={**store.state.restore_point, "heat_limit": 16.0})
    event = build_event("t", None, Flags(), store.state)
    assert (event["kurve"], event["parallelverschiebung"], event["mindestvorlauf"]) == (1.05, 21.0, 20.5)
    assert event["heizgrenze"] == 16.0
    assert "offset" not in event


def test_manual_hint_shape(make_store):
    store = make_store(backup={
        "restore_point": {"curve": 1.05, "room_setpoint": 21.0},
        "manual_override": {"levers": {"curve": 1.3, "room_setpoint": 22.0, "heat_limit": 16.0},
                            "erkannt": "2026-10-03T11:00:00+02:00", "signatur": "x"},
    })
    hint = build_event("t", None, Flags(), store.state)["hinweise"]["manueller_eingriff"]
    assert hint == {"kurve": 1.3, "parallelverschiebung": 22.0, "erkannt": "2026-10-03T11:00:00+02:00"}


def test_version():
    assert ADDON_VERSION == "0.30.0"


@pytest.mark.parametrize("fault,expected", [
    (DataFault("local", ("dart", "dat")), {"art": "lokal", "rollen": ["dart", "dat"]}),
    (DataFault("server", ("unplausibler Wert für dat: 99",)), {"art": "server", "rollen": []}),
    (DataFault("write", ("curve (number.x): Cloud weg",)), {"art": "anlage", "rollen": ["curve"]}),
])
def test_event_names_the_kind_and_roles_of_a_data_fault(fault, expected):
    state = BridgeState(delivery=DeliveryState(datenfehler=fault))

    assert build_event("t", None, Flags(gestartet=True), state)["datenfehler"] == expected


def test_abo_inactive_carries_the_end_of_the_grace_period():
    event = build_event("t", None, Flags(), BridgeState(abo_inactive_since=SINCE))

    assert (event["abo"], event["abo_frist_ende"]) == ("inaktiv", "2026-10-31")


@pytest.mark.parametrize("flags,state,expected", [
    (Flags(konfigurationsfehler=True, grund="Entity fehlt"), BridgeState(), "Entity fehlt"),
    (Flags(abgemeldet=True, grund="scheitert"), BridgeState(), "scheitert"),
    (Flags(gestartet=True, grund="alt"), BridgeState(), None),
    (Flags(abo_beendet=True, grund="Zuruecksetzen scheitert"), BridgeState(), "Zuruecksetzen scheitert"),
])
def test_reason_is_only_sent_with_a_state_that_has_one(flags, state, expected):
    assert build_event("t", None, flags, state)["grund"] == expected


def test_reporter_publishes_changes_once_but_always_on_publish(make_store):
    ha_api = MagicMock()
    reporter = StatusReporter(ha_api, "client1", "abc", make_store())

    reporter.publish_if_changed()
    reporter.publish_if_changed()
    assert ha_api.fire_event.call_count == 1

    reporter.update(gestartet=True)
    assert reporter.status == "regelt"
    assert ha_api.fire_event.call_args.args == ("smartheat_status", reporter.event())

    reporter.publish()
    assert ha_api.fire_event.call_count == 3


def test_reporter_sees_changes_in_the_store(make_store):
    ha_api = MagicMock()
    store = make_store()
    reporter = StatusReporter(ha_api, "client1", None, store)
    reporter.publish()

    store.update(boost_active=True)
    reporter.publish_if_changed()

    assert ha_api.fire_event.call_args.args[1]["boost"] == "komfort"


def test_failed_send_is_logged_and_repeated_on_the_next_check(make_store, caplog):
    ha_api = MagicMock()
    ha_api.fire_event.side_effect = [RuntimeError("HA weg"), None]
    reporter = StatusReporter(ha_api, "client1", None, make_store())

    with caplog.at_level(logging.WARNING):
        reporter.publish_if_changed()
    reporter.publish_if_changed()

    assert "smartheat_status" in caplog.text
    assert ha_api.fire_event.call_count == 2


def test_empty_setup_id_is_sent_as_none(make_store):
    assert StatusReporter(MagicMock(), "t", "", make_store()).event()["setup_id"] is None


def test_unwritable_disk_is_a_local_data_fault_ranked_after_notbetrieb():
    assert overall_status(Flags(gestartet=True), BridgeState(), storage_failed=True) == "datenfehler"
    assert overall_status(
        Flags(gestartet=True), BridgeState(delivery=DeliveryState(notbetrieb=True)), storage_failed=True,
    ) == "notbetrieb"
    event = build_event("t", None, Flags(gestartet=True), BridgeState(), storage_failed=True)
    assert event["datenfehler"] == {"art": "lokal", "rollen": ["datentraeger"]}


def test_a_delivery_fault_wins_over_the_disk_in_the_event():
    state = BridgeState(delivery=DeliveryState(datenfehler=DataFault("server", ("x",))))
    event = build_event("t", None, Flags(gestartet=True), state, storage_failed=True)
    assert event["datenfehler"] == {"art": "server", "rollen": []}


def test_hinweise_carry_the_waerme_fehlt_since_time():
    state = BridgeState(waerme_fehlt_seit="2026-09-30T05:11:00+02:00")
    event = build_event("t", None, Flags(gestartet=True), state)
    assert event["hinweise"]["waerme_fehlt"] == "2026-09-30T05:11:00+02:00"
    assert build_event("t", None, Flags(gestartet=True), BridgeState())["hinweise"]["waerme_fehlt"] is None
