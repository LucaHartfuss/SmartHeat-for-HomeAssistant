"""HA-Sinks fuer Status und Meldungen (Plan SHG G1, Praezisierungen 4, 5)."""
from fakes import FailingServiceHa, FakeHa

from heizungsbruecke.ha_sinks import EVENT_TYPE, HaNotifySink, HaStatusSink, notification_id


def test_status_sink_fires_the_smartheat_status_event():
    ha = FakeHa()
    HaStatusSink(ha).publish({"status": "regelt"})
    assert ha.events == [(EVENT_TYPE, {"status": "regelt"})]
    assert EVENT_TYPE == "smartheat_status"


def test_push_goes_to_every_service_even_if_one_fails():
    ha = FailingServiceHa()
    HaNotifySink(ha, ["notify.kaputt", "notify.handy"]).push("abo", "Text")
    assert ha.pushes == ["Text"]


def test_show_and_withdraw_use_the_stable_notification_id():
    ha = FakeHa()
    sink = HaNotifySink(ha, [])
    sink.show("raumfuehler:sensor.x", "Text")
    sink.withdraw("raumfuehler:sensor.x")
    assert ha.persistent == [("smartheat_raumfuehler_sensor_x", "Text")]
    assert ha.dismissed == ["smartheat_raumfuehler_sensor_x"]
    assert notification_id("Abo") == "smartheat_abo"
