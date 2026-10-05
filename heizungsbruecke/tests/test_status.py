"""Status-Kanal des Add-ons (Spec TP7 1.1, 3.1)."""
import logging
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from heizungsbruecke.ha_sinks import EVENT_TYPE, HaStatusSink
from heizungsbruecke.version import ADDON_VERSION
from smartheat_core.levers import LEVER_SETS
from smartheat_runtime import status
from smartheat_runtime.delivery import DataFault, DeliveryState
from smartheat_runtime.state import BridgeState
from smartheat_runtime.status import Flags, StatusReporter, build_event, overall_status

VAILLANT = LEVER_SETS["vaillant_vrc720"]
VIESSMANN = LEVER_SETS["viessmann_vicare"]

SINCE = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
OVERRIDE = {"levers": {"curve": 1.3, "room_setpoint": 24.5, "heat_limit": 16.0}, "erkannt": "2026-10-01T08:00:00+02:00"}


def test_contract_values_match_the_integration():
    # Gleiche Werte wie const.py der Integration (Contract-Check 14).
    assert (EVENT_TYPE, status.EVENT_SCHEMA, status.HEARTBEAT_SECONDS) == ("smartheat_status", 2, 300)
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
        "boost", "letzte_serverantwort", "hebelsatz", "hebel", "gelernt", "abo", "abo_frist_ende", "hinweise",
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

    event = build_event("client1", "abc", Flags(gestartet=True, abo="aktiv"), state, VAILLANT, ADDON_VERSION)

    assert tuple(event) == status.EVENT_FIELDS
    assert event == {
        "schema": 2, "tenant_id": "client1", "setup_id": "abc", "addon_version": ADDON_VERSION,
        "status": "regelt", "grund": None, "notbetrieb": False, "datenfehler": None, "boost": "notfall",
        "letzte_serverantwort": "2026-10-01T12:00:05+02:00", "hebelsatz": "vaillant_vrc720",
        "hebel": {"curve": 0.9, "room_setpoint": 22.0, "heat_limit": None, "min_flow": None}, "gelernt": None,
        "abo": "aktiv", "abo_frist_ende": None,
        "hinweise": {
            "raumfuehler_ausgefallen": ["sensor.a", "sensor.b"], "batterie_niedrig": ["sensor.x"],
            "manueller_eingriff": {
                "hebel": {"curve": 1.3, "room_setpoint": 24.5, "heat_limit": 16.0}, "erkannt": "2026-10-01T08:00:00+02:00",
            },
            "waerme_fehlt": None,
        },
    }


def test_event_carries_levers_of_the_lever_set_and_derived_min_flow(make_store):
    store = make_store()
    store.update(restore_point={"curve": 1.05, "room_setpoint": 21.0, "heat_limit": 16.0},
                 learned={"curve": 1.05, "heat_limit": 16.2}, min_flow_current=20.5)
    event = build_event("t1", None, Flags(), store.state, VAILLANT, ADDON_VERSION)
    assert event["hebelsatz"] == "vaillant_vrc720"
    assert event["hebel"] == {"curve": 1.05, "room_setpoint": 21.0, "heat_limit": 16.0, "min_flow": 20.5}
    assert event["gelernt"] == {"curve": 1.05, "heat_limit": 16.2}
    assert set(event) == set(status.EVENT_FIELDS)
    assert not {"kurve", "parallelverschiebung", "mindestvorlauf", "heizgrenze"} & set(event)


def test_event_without_learned_values_reports_none(make_store):
    store = make_store()
    store.update(restore_point={"curve": 1.0, "level": -2.0, "room_setpoint": 20.0})
    event = build_event("t1", None, Flags(), store.state, VIESSMANN, ADDON_VERSION)
    assert event["hebelsatz"] == "viessmann_vicare"
    assert event["hebel"] == {"curve": 1.0, "level": -2.0, "room_setpoint": 20.0}
    assert event["gelernt"] is None


def test_manual_hint_names_levers(make_store):
    store = make_store(backup={
        "restore_point": {"curve": 1.05, "room_setpoint": 21.0},
        "manual_override": {"levers": {"curve": 1.3, "room_setpoint": 22.0, "heat_limit": 16.0},
                            "erkannt": "2026-10-03T11:00:00+02:00", "signatur": "x"},
    })
    hint = build_event("t", None, Flags(), store.state, VAILLANT, ADDON_VERSION)["hinweise"]["manueller_eingriff"]
    assert hint == {"hebel": {"curve": 1.3, "room_setpoint": 22.0, "heat_limit": 16.0},
                    "erkannt": "2026-10-03T11:00:00+02:00"}


def test_version():
    assert ADDON_VERSION == "0.33.0"


@pytest.mark.parametrize("fault,expected", [
    (DataFault("local", ("dart", "dat")), {"art": "lokal", "rollen": ["dart", "dat"]}),
    (DataFault("server", ("unplausibler Wert für dat: 99",)), {"art": "server", "rollen": []}),
    (DataFault("write", ("curve (number.x): Cloud weg",)), {"art": "anlage", "rollen": ["curve"]}),
])
def test_event_names_the_kind_and_roles_of_a_data_fault(fault, expected):
    state = BridgeState(delivery=DeliveryState(datenfehler=fault))

    assert build_event("t", None, Flags(gestartet=True), state, VAILLANT, ADDON_VERSION)["datenfehler"] == expected


def test_abo_inactive_carries_the_end_of_the_grace_period():
    event = build_event("t", None, Flags(), BridgeState(abo_inactive_since=SINCE), VAILLANT, ADDON_VERSION)

    assert (event["abo"], event["abo_frist_ende"]) == ("inaktiv", "2026-10-31")


@pytest.mark.parametrize("flags,state,expected", [
    (Flags(konfigurationsfehler=True, grund="Entity fehlt"), BridgeState(), "Entity fehlt"),
    (Flags(abgemeldet=True, grund="scheitert"), BridgeState(), "scheitert"),
    (Flags(gestartet=True, grund="alt"), BridgeState(), None),
    (Flags(abo_beendet=True, grund="Zuruecksetzen scheitert"), BridgeState(), "Zuruecksetzen scheitert"),
])
def test_reason_is_only_sent_with_a_state_that_has_one(flags, state, expected):
    assert build_event("t", None, flags, state, VAILLANT, ADDON_VERSION)["grund"] == expected


def test_reporter_publishes_changes_once_but_always_on_publish(make_store):
    ha_api = MagicMock()
    reporter = StatusReporter(HaStatusSink(ha_api), "client1", "abc", make_store(), VAILLANT, ADDON_VERSION)

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
    reporter = StatusReporter(HaStatusSink(ha_api), "client1", None, store, VAILLANT, ADDON_VERSION)
    reporter.publish()

    store.update(boost_active=True)
    reporter.publish_if_changed()

    assert ha_api.fire_event.call_args.args[1]["boost"] == "komfort"


def test_failed_send_is_logged_and_repeated_on_the_next_check(make_store, caplog):
    ha_api = MagicMock()
    ha_api.fire_event.side_effect = [RuntimeError("HA weg"), None]
    reporter = StatusReporter(HaStatusSink(ha_api), "client1", None, make_store(), VAILLANT, ADDON_VERSION)

    with caplog.at_level(logging.WARNING):
        reporter.publish_if_changed()
    reporter.publish_if_changed()

    assert "konnte nicht gesendet werden" in caplog.text
    assert ha_api.fire_event.call_count == 2


def test_empty_setup_id_is_sent_as_none(make_store):
    assert StatusReporter(HaStatusSink(MagicMock()), "t", "", make_store(), VAILLANT, ADDON_VERSION).event()["setup_id"] is None


def test_unwritable_disk_is_a_local_data_fault_ranked_after_notbetrieb():
    assert overall_status(Flags(gestartet=True), BridgeState(), storage_failed=True) == "datenfehler"
    assert overall_status(
        Flags(gestartet=True), BridgeState(delivery=DeliveryState(notbetrieb=True)), storage_failed=True,
    ) == "notbetrieb"
    event = build_event("t", None, Flags(gestartet=True), BridgeState(), VAILLANT, ADDON_VERSION, storage_failed=True)
    assert event["datenfehler"] == {"art": "lokal", "rollen": ["datentraeger"]}


def test_a_delivery_fault_wins_over_the_disk_in_the_event():
    state = BridgeState(delivery=DeliveryState(datenfehler=DataFault("server", ("x",))))
    event = build_event("t", None, Flags(gestartet=True), state, VAILLANT, ADDON_VERSION, storage_failed=True)
    assert event["datenfehler"] == {"art": "server", "rollen": []}


def test_hinweise_carry_the_waerme_fehlt_since_time():
    state = BridgeState(waerme_fehlt_seit="2026-09-30T05:11:00+02:00")
    event = build_event("t", None, Flags(gestartet=True), state, VAILLANT, ADDON_VERSION)
    assert event["hinweise"]["waerme_fehlt"] == "2026-09-30T05:11:00+02:00"
    assert build_event("t", None, Flags(gestartet=True), BridgeState(), VAILLANT, ADDON_VERSION)["hinweise"]["waerme_fehlt"] is None
