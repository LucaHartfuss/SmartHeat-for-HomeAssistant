"""Template-Texte der Hilfssensoren (Spec TP6 3.2), gerendert mit reinem Jinja2 plus
nachgebauten HA-Funktionen states()/state_attr(). Das echte HA-Verhalten (none -> unknown)
prueft tests/test_ha_api_real_ha_integration.py."""
import pytest

from heizungsbruecke.helper_templates import outdoor_temperature_template, room_temperature_template

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

    result = _render(template, {"sensor.a": "unavailable", "sensor.b": "0.0", "sensor.c": "22.4"})

    assert result == "22.4"


def test_room_template_is_none_when_no_sensor_is_valid():
    template = room_temperature_template(["sensor.a", "sensor.b"])

    assert _render(template, {"sensor.a": "unknown", "sensor.b": "99"}) == "None"


def test_room_template_with_a_single_sensor():
    assert _render(room_temperature_template(["sensor.a"]), {"sensor.a": "19.5"}) == "19.5"


@pytest.mark.parametrize("value,expected", [(3.2, "3.2"), (-41.0, "None"), (None, "None"), ("x", "None")])
def test_outdoor_template_reads_weather_temperature_within_range(value, expected):
    template = outdoor_temperature_template("weather.forecast_home")

    assert _render(template, {}, {("weather.forecast_home", "temperature"): value}) == expected


def test_room_template_rejects_empty_sensor_list():
    with pytest.raises(ValueError):
        room_temperature_template([])
