"""HA-Sinks fuer Status und Meldungen."""
import logging

from fakes import FREMDTEXT, FREMDTEXT_IM_LOG, FailingServiceHa, FakeHa, logzeilen

from heizungsbruecke.ha_sinks import HaNotifySink, notification_id


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


def test_a_failing_push_logs_a_key_with_foreign_text_only_escaped(caplog):
    """Der Schluessel `fehler:<Text>` traegt Text des Servers; scheitert ein Notify-Dienst, steht er in
    der Warnung nur als Escape."""
    with caplog.at_level(logging.WARNING, logger="heizungsbruecke.ha_sinks"):
        HaNotifySink(FailingServiceHa(), ["notify.kaputt"]).push(f"fehler:{FREMDTEXT}", "Text")
    assert logzeilen(caplog) == [
        f"Push-Benachrichtigung 'fehler:{FREMDTEXT_IM_LOG}' an notify.kaputt konnte nicht gesendet werden"]
