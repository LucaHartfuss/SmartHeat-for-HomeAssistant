"""Status-Entity des Add-ons (Spec TP6 3.6)."""
import logging
from unittest.mock import MagicMock

from heizungsbruecke.status import (
    ADDON_VERSION, STATUS_ATTR_GRUND, STATUS_ATTR_SETUP_ID, STATUS_BEREIT, STATUS_KONFIGURATIONSFEHLER,
    STATUS_STARTET, StatusReporter, status_entity_id,
)


def test_status_values_match_the_integration_contract():
    # Gleiche Werte wie const.py der Integration (Contract-Check, Task 15).
    assert (STATUS_STARTET, STATUS_BEREIT, STATUS_KONFIGURATIONSFEHLER) == ("startet", "bereit", "konfigurationsfehler")
    assert STATUS_ATTR_SETUP_ID == "setup_id"
    assert STATUS_ATTR_GRUND == "grund"


def test_status_entity_id_is_a_slug_of_the_tenant():
    assert status_entity_id("client1") == "sensor.smartheat_client1_status"
    assert status_entity_id("Smoke-Test 01") == "sensor.smartheat_smoke_test_01_status"


def test_set_posts_state_with_version_and_setup_id():
    ha_api = MagicMock()
    reporter = StatusReporter(ha_api, "client1", "abc123")

    reporter.set(STATUS_STARTET)

    ha_api.set_state.assert_called_once_with(
        "sensor.smartheat_client1_status", "startet",
        {"friendly_name": "SmartHeat Status", "addon_version": ADDON_VERSION, "setup_id": "abc123"},
    )


def test_error_state_carries_the_reason_and_no_setup_id_when_absent():
    ha_api = MagicMock()
    reporter = StatusReporter(ha_api, "client1", None)

    reporter.set(STATUS_KONFIGURATIONSFEHLER, grund="Konfiguration veraltet")

    attributes = ha_api.set_state.call_args.args[2]
    assert attributes["grund"] == "Konfiguration veraltet"
    assert "setup_id" not in attributes


def test_republish_resends_the_last_state_and_does_nothing_before_the_first():
    ha_api = MagicMock()
    reporter = StatusReporter(ha_api, "client1", "abc")
    reporter.republish()
    ha_api.set_state.assert_not_called()

    reporter.set(STATUS_BEREIT)
    reporter.republish()

    assert [c.args[1] for c in ha_api.set_state.call_args_list] == ["bereit", "bereit"]
    assert reporter.state == STATUS_BEREIT


def test_failing_ha_is_only_logged(caplog):
    ha_api = MagicMock()
    ha_api.set_state.side_effect = RuntimeError("HA weg")

    with caplog.at_level(logging.WARNING):
        StatusReporter(ha_api, "client1", "abc").set(STATUS_STARTET)

    assert "sensor.smartheat_client1_status" in caplog.text
