"""Raummittel des Gateways (room_mean.py) und HA-Template (helper_templates) nach derselben Falltabelle."""
import pytest

from heizungsbruecke.helper_templates import room_temperature_template
from smartheat_runtime.room_mean import room_mean

jinja2 = pytest.importorskip("jinja2")

CASES = [
    (["20.0", "21.0", "21.5"], 20.83),
    (["unavailable", "0.0", "22.4"], 22.4),       # tot und unplausibel fallen weg
    (["unknown", "99"], None),                     # kein gueltiger Wert
    (["21.0"], 21.0),
    (["5.0", "35.0"], 20.0),                       # Grenzen gehoeren dazu
    (["4.99", "35.01", "20"], 20.0),
    (["nan", "inf", "-inf", "19.5"], 19.5),
    (["20.004", "20.005"], 20.0),                  # Rundung wie Jinja round(2) (Python round)
    (["", "abc", "21.333"], 21.33),
]


def _template_value(raw_values: list[str]) -> float | None:
    refs = [f"sensor.s{i}" for i in range(len(raw_values))]
    env = jinja2.Environment()
    states = dict(zip(refs, raw_values, strict=True))
    env.globals["states"] = lambda entity_id: states.get(entity_id, "unknown")
    env.globals["state_attr"] = lambda entity_id, attribute: None
    rendered = env.from_string(room_temperature_template(refs)).render()
    return None if rendered == "None" else float(rendered)


def _parsed(raw: str):
    try:
        return float(raw)
    except ValueError:
        return None


@pytest.mark.parametrize(("raw_values", "expected"), CASES)
def test_python_and_template_agree(raw_values, expected):
    assert _template_value(raw_values) == expected
    assert room_mean([_parsed(raw) for raw in raw_values]) == expected


def test_bools_are_not_temperatures():
    assert room_mean([True, 21.0]) == 21.0
