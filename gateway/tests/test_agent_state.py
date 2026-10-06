import json

import pytest

from smartheat_gateway.agent.state import AgentStateWriter, derive


@pytest.mark.parametrize(("kwargs", "expected"), [
    (dict(server_seen=False, server_down_seconds=None, device_state=None, runtime_status=None), "startet"),
    (dict(server_seen=True, server_down_seconds=301, device_state="uebernommen", runtime_status={"status": "regelt"}),
     "keine_verbindung"),
    (dict(server_seen=True, server_down_seconds=None, device_state="nicht_uebernommen", runtime_status=None),
     "nicht_uebernommen"),
    (dict(server_seen=True, server_down_seconds=None, device_state="uebernommen", runtime_status=None),
     "wartet_auf_einrichtung"),
    (dict(server_seen=True, server_down_seconds=10, device_state="uebernommen",
          runtime_status={"status": "abgemeldet"}), "wartet_auf_einrichtung"),
    (dict(server_seen=True, server_down_seconds=None, device_state="uebernommen",
          runtime_status={"status": "regelt"}), "regelt"),
    (dict(server_seen=True, server_down_seconds=None, device_state="uebernommen",
          runtime_status={"status": "abo_inaktiv"}), "regelt"),
    (dict(server_seen=True, server_down_seconds=None, device_state="uebernommen",
          runtime_status={"status": "notbetrieb"}), "stoerung"),
    (dict(server_seen=True, server_down_seconds=None, device_state="uebernommen",
          runtime_status={"status": "abo_beendet"}), "stoerung"),
    (dict(server_seen=True, server_down_seconds=None, device_state="uebernommen",
          runtime_status={"status": "startet"}), "startet"),
])
def test_derive(kwargs, expected):
    assert derive(**kwargs) == expected


def test_writer_refreshes_the_file_every_minute(tmp_path, clock):
    path = tmp_path / "agent_state.json"
    writer = AgentStateWriter(path, lambda: f"t{clock()}", clock)
    writer.update("regelt")
    first = json.loads(path.read_text())
    writer.update("regelt")
    assert json.loads(path.read_text()) == first
    clock.advance(61)
    writer.update("regelt")
    assert json.loads(path.read_text())["ts"] != first["ts"]


def test_broken_state_file_is_overwritten(tmp_path, clock):
    path = tmp_path / "agent_state.json"
    path.write_text("{kaputt")
    AgentStateWriter(path, lambda: "t", clock).update("startet")
    assert json.loads(path.read_text())["zustand"] == "startet"
