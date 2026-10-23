import json
import os
import signal
import threading
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
                                     "regelt", "stoerung", "gesperrt", "update_noetig"])
def test_known_states_have_patterns(tmp_path, zustand):
    assert led.state_from_file(_state(tmp_path, zustand)) == zustand
    assert sum(seconds for _, seconds in led.PATTERNS[zustand]) <= 2.0


def test_patterns_cover_exactly_the_device_states():
    from smartheat_device import wire

    assert set(led.PATTERNS) == set(wire.ZUSTAENDE)


def test_blocked_and_update_needed_are_distinct_from_each_other_and_from_stoerung():
    assert len({led.PATTERNS[z] for z in ("gesperrt", "update_noetig", "stoerung")}) == 3


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


def _plant_fifo(path):
    os.mkfifo(path)


def _plant_symlink(path):
    target = path.with_name("ziel.json")
    target.write_text(json.dumps({"zustand": "regelt"}))
    path.symlink_to(target)


def _plant_huge(path):
    path.write_text(json.dumps({"zustand": "regelt", "fuell": "x" * 200_000}))


@pytest.mark.parametrize("plant", [_plant_fifo, _plant_symlink, _plant_huge])
def test_unsafe_state_files_are_stoerung_and_never_block(tmp_path, plant):
    """data/ ist fuer Container schreibbar, der Host-Dienst laeuft als root: kein Folgen von Symlinks, kein Blockieren
    an einer FIFO, kein unbegrenztes Lesen."""
    path = tmp_path / "agent_state.json"
    plant(path)
    assert led.state_from_file(path) == "stoerung"


def test_main_takes_the_led_from_find_led_and_not_from_the_environment(tmp_path, monkeypatch):
    """`SHG_LED` aus der Umgebung gilt nicht mehr; ohne LED nach `find_led` endet der Dienst mit der Meldung, ohne
    den Trigger der genannten Datei anzufassen."""
    led_dir = tmp_path / "ACT"
    led_dir.mkdir()
    (led_dir / "trigger").write_text("none [mmc0] heartbeat\n")
    monkeypatch.setenv("SHG_LED", str(led_dir))
    monkeypatch.setenv("SHG_ROOT", str(tmp_path))
    monkeypatch.setattr(led, "find_led", lambda: None)
    monkeypatch.setattr(led, "play", lambda *args: pytest.fail("main hat die LED aus der Umgebung genommen"))
    with pytest.raises(SystemExit) as ende:
        led.main()
    assert "Keine LED gefunden" in str(ende.value.code)
    assert (led_dir / "trigger").read_text() == "none [mmc0] heartbeat\n"


def test_sigterm_ends_the_service_and_gives_the_trigger_back(tmp_path, monkeypatch):
    """`main` wie von systemd gestartet: die LED laeuft mit Trigger `none`; SIGTERM beendet mit Code 0, der vorherige
    Trigger (`mmc0`) steht wieder in der Datei. Das Signal geht an den eigenen Prozess; der Test setzt vorher einen
    Handler, der bei einem fehlenden Handler von `main` laut fehlschlaegt statt den Prozess zu beenden."""
    led_dir = tmp_path / "ACT"
    led_dir.mkdir()
    (led_dir / "trigger").write_text("none [mmc0] heartbeat\n")
    (led_dir / "brightness").write_text("0")
    (tmp_path / "data").mkdir()
    _state(tmp_path / "data", "startet")
    monkeypatch.setattr(led, "find_led", lambda: led_dir)
    monkeypatch.setenv("SHG_ROOT", str(tmp_path))
    gesehen: list[str] = []

    def sende_sigterm_sobald_die_led_laeuft() -> None:
        frist = time.monotonic() + 10
        while (led_dir / "trigger").read_text() != "none" and time.monotonic() < frist:
            time.sleep(0.01)
        gesehen.append((led_dir / "trigger").read_text())
        os.kill(os.getpid(), signal.SIGTERM)

    def ohne_handler_von_main(signum, frame):
        raise RuntimeError("main hat keinen eigenen SIGTERM-Handler gesetzt")

    alt = signal.signal(signal.SIGTERM, ohne_handler_von_main)
    sender = threading.Thread(target=sende_sigterm_sobald_die_led_laeuft)
    try:
        sender.start()
        with pytest.raises(SystemExit) as ende:
            led.main()
    finally:
        sender.join(timeout=15)
        signal.signal(signal.SIGTERM, alt)
    assert ende.value.code == 0
    assert gesehen == ["none"]  # Gegenprobe: waehrend des Betriebs war der Trigger uebernommen
    assert (led_dir / "trigger").read_text() == "mmc0"


def test_sigterm_right_after_taking_the_trigger_still_gives_it_back(tmp_path, monkeypatch):
    """Das Signal kann schon zwischen `take` und dem Eintritt in die Schleife eintreffen (systemd stoppt kurz nach dem
    Start). Der Test erzwingt das: SIGTERM geht unmittelbar nach dem echten `take` an den eigenen Prozess, ohne
    Schleife, Thread oder Wartezeit. Der Trigger muss trotzdem zurueckgegeben werden."""
    led_dir = tmp_path / "ACT"
    led_dir.mkdir()
    (led_dir / "trigger").write_text("none [mmc0] heartbeat\n")
    (led_dir / "brightness").write_text("0")
    (tmp_path / "data").mkdir()
    _state(tmp_path / "data", "startet")
    monkeypatch.setattr(led, "find_led", lambda: led_dir)
    monkeypatch.setenv("SHG_ROOT", str(tmp_path))
    echtes_take = led.Led.take
    gesehen: list[str] = []

    def take_dann_sigterm(self) -> None:
        echtes_take(self)
        gesehen.append((led_dir / "trigger").read_text())
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(1)  # das Signal wird hier im Hauptthread zugestellt (SystemExit), nicht erst nach dem Test

    monkeypatch.setattr(led.Led, "take", take_dann_sigterm)
    alt = signal.signal(signal.SIGTERM, lambda signum, frame: pytest.fail("main hat keinen SIGTERM-Handler gesetzt"))
    try:
        with pytest.raises(SystemExit) as ende:
            led.main()
    finally:
        signal.signal(signal.SIGTERM, alt)
    assert ende.value.code == 0
    assert gesehen == ["none"]  # Gegenprobe: der Trigger war uebernommen, als das Signal kam
    assert (led_dir / "trigger").read_text() == "mmc0"
