"""Kundentexte vom Host: die HA-Standardwerte sind die bisherigen Texte (wortgleich)."""
from dataclasses import fields

from smartheat_runtime import delivery
from smartheat_runtime.texts import HA_TEXTS, HostTexts


def test_ha_defaults_are_the_previous_texts():
    assert HA_TEXTS.delivery_prefix == "Heizungsbrücke"
    assert HA_TEXTS.source_unavailable_log == "Home Assistant nicht erreichbar"


def test_delivery_texts_use_the_prefix():
    gateway = HostTexts(delivery_prefix="SmartHeat-Gateway")
    on = delivery.notification_text(delivery.NOTIFY_NOTBETRIEB_ON, (), {}, gateway)
    assert on.startswith("SmartHeat-Gateway: Server antwortet nicht")
    assert delivery.notification_text(delivery.NOTIFY_NOTBETRIEB_ON, (), {}) == on.replace(
        "SmartHeat-Gateway", "Heizungsbrücke"
    )


def test_every_text_has_a_default():
    assert all(isinstance(getattr(HA_TEXTS, field.name), str) for field in fields(HostTexts))

