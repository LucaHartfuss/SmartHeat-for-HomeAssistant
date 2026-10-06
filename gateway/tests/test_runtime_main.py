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


def test_raum_is_republished_every_tick_even_when_unchanged(world):
    write_runtime_files(world.paths, apply_config())
    world.start_runtime()
    world.connect()
    first = [body for topic, body in world.bus.decoded() if topic == topics.RAUM]
    world.advance(runtime_main.RAUM_SECONDS)
    world.advance(runtime_main.RAUM_SECONDS)
    published = [body for topic, body in world.bus.decoded() if topic == topics.RAUM]
    assert len(published) == len(first) + 2
    assert published[-1]["ts"] != published[-2]["ts"]
    assert {(b["ist"], b["soll"]) for b in published} == {(20.0, 20.0)}


def test_failing_alive_file_does_not_stop_the_config_watch(world, monkeypatch):
    world.start_runtime()

    def broken(self, *args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr("pathlib.Path.touch", broken)
    world.advance(runtime_main.CONFIG_WATCH_SECONDS + 1)
    write_runtime_files(world.paths, apply_config())  # nach dem fehlgeschlagenen Touch geaendert
    world.advance(runtime_main.CONFIG_WATCH_SECONDS + 1)
    assert world.restarts == 2


def test_idle_states_clear_the_retained_raum(world):
    write_runtime_files(world.paths, apply_config(room_sensors=["zigbee:0xzz"]))  # Konfigurationsfehler
    world.bus.publish(topics.RAUM, {"ist": 19.0, "soll": 22.0, "soll_quelle": "portal", "ts": "alt"}, retain=True)
    result = world.start_runtime()
    assert result.reason == "konfigurationsfehler"
    assert topics.RAUM not in world.bus.retained
    assert world.status()["status"] == "konfigurationsfehler"  # der Status bleibt dem Ruhezustand ueberlassen
