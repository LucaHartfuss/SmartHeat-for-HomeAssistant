import json
import os
import time

import pytest

from smartheat_host import led


def _state(tmp_path, zustand, age=0):
    path = tmp_path / "agent_state.json"
    path.write_text(json.dumps({"zustand": zustand, "seit": "x", "ts": "x"}))
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


@pytest.mark.parametrize("zustand", ["startet", "nicht_uebernommen", "wartet_auf_einrichtung", "keine_verbindung",
                                     "regelt", "stoerung"])
def test_known_states_have_patterns(tmp_path, zustand):
    assert led.state_from_file(_state(tmp_path, zustand)) == zustand
    assert sum(seconds for _, seconds in led.PATTERNS[zustand]) <= 2.0


def test_patterns_cover_exactly_the_agent_states():
    from smartheat_gateway.agent import wire

    assert set(led.PATTERNS) == set(wire.AGENT_STATES)


def test_stale_unreadable_or_unknown_state_is_stoerung(tmp_path):
    assert led.state_from_file(_state(tmp_path, "regelt", age=301)) == "stoerung"
    assert led.state_from_file(tmp_path / "fehlt.json") == "stoerung"
    assert led.state_from_file(_state(tmp_path, "unbekannt")) == "stoerung"


def test_malformed_state_files_are_stoerung(tmp_path):
    path = tmp_path / "agent_state.json"
    for content in ("kaputt{", "[]", '{"zustand": ["regelt"]}', "{}"):
        path.write_text(content)
        assert led.state_from_file(path) == "stoerung", content


def test_regelt_is_steady_on_and_stoerung_has_three_pulses():
    assert all(on for on, _ in led.PATTERNS["regelt"])
    assert [on for on, _ in led.PATTERNS["stoerung"]].count(True) == 3
    assert [on for on, _ in led.PATTERNS["nicht_uebernommen"]].count(True) == 2


def test_led_takes_over_and_restores_the_trigger(tmp_path):
    base = tmp_path / "ACT"
    base.mkdir()
    (base / "trigger").write_text("none [mmc0] timer heartbeat\n")
    (base / "brightness").write_text("0\n")
    device = led.Led(base)
    device.take()
    assert (base / "trigger").read_text().strip() == "none"
    device.set(True)
    assert (base / "brightness").read_text().strip() == "1"
    device.restore()
    assert (base / "trigger").read_text().strip() == "mmc0"


def test_find_led_prefers_act(tmp_path):
    (tmp_path / "led0").mkdir()
    assert led.find_led(tmp_path) == tmp_path / "led0"
    (tmp_path / "ACT").mkdir()
    assert led.find_led(tmp_path) == tmp_path / "ACT"
    assert led.find_led(tmp_path / "nichts") is None


def test_play_follows_the_pattern(tmp_path):
    base = tmp_path / "ACT"
    base.mkdir()
    (base / "trigger").write_text("[none]\n")
    (base / "brightness").write_text("0\n")
    writes = []
    device = led.Led(base)
    device.set = lambda on: writes.append(on)  # type: ignore[method-assign]
    clock = [0.0]
    led.play(device, lambda: "stoerung", lambda: clock[0], lambda s: clock.__setitem__(0, clock[0] + s),
             steps=len(led.PATTERNS["stoerung"]))
    assert writes == [on for on, _ in led.PATTERNS["stoerung"]]


def test_play_switches_pattern_when_the_state_changes(tmp_path):
    base = tmp_path / "ACT"
    base.mkdir()
    writes = []
    device = led.Led(base)
    device.set = lambda on: writes.append(on)  # type: ignore[method-assign]
    clock = [0.0]
    states = iter(["keine_verbindung", "regelt", "regelt", "regelt"])
    current = ["keine_verbindung"]

    def read_state():
        current[0] = next(states, current[0])
        return current[0]

    led.play(device, read_state, lambda: clock[0], lambda s: clock.__setitem__(0, clock[0] + s), steps=4)
    assert writes == [True, True, True, True]  # keine_verbindung waere nach 0,5 s aus; nach dem Wechsel dauerhaft an
