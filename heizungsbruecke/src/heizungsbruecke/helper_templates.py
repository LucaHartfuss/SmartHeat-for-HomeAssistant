"""Template-Texte fuer die Template-Hilfssensoren (Spec TP6, 3.2). Eine Quelle ist `sensor.x`
oder `entity::attribut` (Add-on-Konvention, siehe ha_api.get_state). Ungueltige oder
unplausible Werte zaehlen nicht; ohne gueltigen Wert rendert das Template `none`, der Sensor
steht dann auf `unknown` und der Tick meldet einen lokalen Datenfehler."""
from heizungsbruecke.plausibility import OUTDOOR_TEMP_RANGE, ROOM_TEMP_RANGE


def _source_expression(ref: str) -> str:
    entity_id, _, attribute = ref.partition("::")
    if attribute:
        return f"state_attr('{entity_id}', '{attribute}') | float(none)"
    return f"states('{entity_id}') | float(none)"


def room_temperature_template(room_sensors: list[str]) -> str:
    """Ungewichtetes Mittel aller gueltigen Raumfuehler, 2 Nachkommastellen."""
    if not room_sensors:
        raise ValueError("room_temperature_template braucht mindestens einen Raumfuehler")
    low, high = ROOM_TEMP_RANGE
    sources = ", ".join(_source_expression(ref) for ref in room_sensors)
    return (
        "{% set ns = namespace(values=[]) %}"
        f"{{% for v in [{sources}] %}}"
        f"{{% if v is not none and {low:g} <= v <= {high:g} %}}{{% set ns.values = ns.values + [v] %}}{{% endif %}}"
        "{% endfor %}"
        "{{ (ns.values | sum / ns.values | length) | round(2) if ns.values else none }}"
    )


def outdoor_temperature_template(weather_entity_id: str) -> str:
    low, high = OUTDOOR_TEMP_RANGE
    return (
        f"{{% set v = state_attr('{weather_entity_id}', 'temperature') | float(none) %}}"
        f"{{{{ v if v is not none and {low:g} <= v <= {high:g} else none }}}}"
    )
