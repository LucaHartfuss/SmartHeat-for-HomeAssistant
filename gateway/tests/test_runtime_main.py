import pytest
from configs import apply_config, write_runtime_files
from world import GatewayWorld

from smartheat_gateway import runtime_main, topics


@pytest.fixture
def world(data_dir, monkeypatch, clock):
    return GatewayWorld(data_dir, monkeypatch, clock)


def test_reload_command_restarts_the_runtime(world):
    world.start_runtime()
    write_runtime_files(world.paths, apply_config())
    world.bus.publish(topics.CMD_RELOAD, {"setup_id": "setup-1"})
    world.run()
    assert world.restarts == 2
    assert world.host.boot_info().setup_id == "setup-1"


def test_config_watch_catches_a_missed_reload(world):
    world.start_runtime()
    write_runtime_files(world.paths, apply_config())
    world.advance(runtime_main.CONFIG_WATCH_SECONDS + 1)
    assert world.restarts == 2


def test_unchanged_config_does_not_restart_and_keeps_the_alive_file_fresh(world):
    write_runtime_files(world.paths, apply_config())
    world.start_runtime()
    world.connect()
    alive = world.paths.data / "alive"
    alive.unlink()
    world.advance(5 * runtime_main.CONFIG_WATCH_SECONDS)
    assert world.restarts == 1
    assert alive.exists()


def test_raum_is_published_on_target_change(world):
    write_runtime_files(world.paths, apply_config())
    world.start_runtime()
    world.connect()
    world.bus.publish(topics.CMD_ROOM_TARGET, {"value": 21.5, "source": "portal", "ts": "x"})
    world.run()
    raum = world.bus.retained[topics.RAUM]
    assert (raum["soll"], raum["soll_quelle"], raum["ist"]) == (21.5, "portal", 20.0)
