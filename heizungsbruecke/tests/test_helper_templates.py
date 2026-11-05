"""Template-Texte der Hilfssensoren, gerendert mit reinem Jinja2 plus
nachgebauten HA-Funktionen states()/state_attr(). Das echte HA-Verhalten (none -> unknown)
prueft tests/test_ha_api_real_ha_integration.py."""
import pytest

from heizungsbruecke import helper_templates
from heizungsbruecke.helper_templates import room_temperature_template

jinja2 = pytest.importorskip("jinja2")


def _render(template: str, states: dict[str, str], attributes: dict[tuple[str, str], object] | None = None) -> str:
    env = jinja2.Environment()
    env.globals["states"] = lambda entity_id: states.get(entity_id, "unknown")
    env.globals["state_attr"] = lambda entity_id, attribute: (attributes or {}).get((entity_id, attribute))
    return env.from_string(template).render()


def test_room_template_averages_valid_sensors_to_two_decimals():
    template = room_temperature_template(["sensor.a", "sensor.b", "climate.c::current_temperature"])

    result = _render(template, {"sensor.a": "20.0", "sensor.b": "21.0"}, {("climate.c", "current_temperature"): 21.5})

    assert result == "20.83"


def test_room_template_ignores_dead_and_implausible_sensors():
    template = room_temperature_template(["sensor.a", "sensor.b", "sensor.c"])

    result = _render(template, {"sensor.a": "unavailable", "sensor.b": "-10.1", "sensor.c": "22.4"})

    assert result == "22.4"


def test_room_template_counts_values_below_five_degrees_up_to_the_actual_range_limits():
    template = room_temperature_template(["sensor.a", "sensor.b"])

    assert _render(template, {"sensor.a": "-10.0", "sensor.b": "35.0"}) == "12.5"
    assert _render(template, {"sensor.a": "-10.1", "sensor.b": "35.1"}) == "None"


def test_room_template_is_none_when_no_sensor_is_valid():
    template = room_temperature_template(["sensor.a", "sensor.b"])

    assert _render(template, {"sensor.a": "unknown", "sensor.b": "99"}) == "None"


def test_room_template_with_a_single_sensor():
    assert _render(room_temperature_template(["sensor.a"]), {"sensor.a": "19.5"}) == "19.5"


def test_there_is_no_template_for_the_outdoor_temperature():
    """Die Aussentemperatur kommt aus einem Sensor; ein Template-Helfer dafuer gibt es nicht mehr."""
    assert not hasattr(helper_templates, "outdoor_temperature_template")


def test_room_template_rejects_empty_sensor_list():
    with pytest.raises(ValueError):
        room_temperature_template([])
