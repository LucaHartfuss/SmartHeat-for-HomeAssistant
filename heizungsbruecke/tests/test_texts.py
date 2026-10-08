"""Kundentexte vom Host (Spec SHG G2 2.1): die HA-Standardwerte sind die bisherigen Texte (wortgleich)."""
from dataclasses import fields

from smartheat_runtime import delivery
from smartheat_runtime.texts import HA_TEXTS, HostTexts


def test_ha_defaults_are_the_previous_texts():
    assert HA_TEXTS.delivery_prefix == "Heizungsbrücke"
    assert "SmartHeat-Integration neu anmelden" in HA_TEXTS.relogin_log_rejected
    assert "%s" in HA_TEXTS.relogin_log_status
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


def test_accounts_url_hint_default_is_the_addon_text():
    import pytest

    from smartheat_runtime import options
    with pytest.raises(options.ConfigError) as error:
        options.resolve_accounts_api_base_url(None)
    assert str(error.value) == "Option 'accounts_api_base_url' fehlt - bitte die SmartHeat-Integration neu einrichten"
