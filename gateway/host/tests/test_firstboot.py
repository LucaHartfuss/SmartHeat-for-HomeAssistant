import pytest

from smartheat_host import bundles, firstboot, updater

COMPOSE = ('services:\n  zigbee2mqtt:\n    image: koenkk/zigbee2mqtt@sha256:' + "a" * 64 +
           '\n    devices: ["/dev/zigbee:/dev/zigbee"]\n')


class FakeCompose:
    def __init__(self, fail=False):
        self.ups, self.fail = [], fail

    def up(self, version):
        self.ups.append(version)
        if self.fail:
            raise bundles.ComposeError("boom")


@pytest.fixture
def root(tmp_path):
    bundles.BundleStore(tmp_path).install("0.3.0", {"docker-compose.yml": COMPOSE.encode(), "mosquitto.conf": b""})
    updater.State(current="0.3.0").save(tmp_path / "updater" / "state.json")
    (tmp_path / "images").mkdir()
    for name in ("01.tar", "02.tar"):
        (tmp_path / "images" / name).write_bytes(b"x")
    return tmp_path


def test_loads_starts_and_removes_the_archives(root):
    loaded, compose = [], FakeCompose()
    assert firstboot.run(root, compose, load=lambda path: loaded.append(path.name), path_exists=lambda p: True) == 0
    assert loaded == ["01.tar", "02.tar"] and compose.ups == ["0.3.0"]
    assert list((root / "images").iterdir()) == []


def test_missing_stick_waits_without_loading(root):
    loaded, compose = [], FakeCompose()
    code = firstboot.run(root, compose, load=lambda p: loaded.append(p), path_exists=lambda p: False)
    assert code == firstboot.EXIT_RETRY
    assert loaded == [] and compose.ups == [] and len(list((root / "images").iterdir())) == 2


def test_open_updater_job_only_loads(root):
    updater.State(current="0.3.0", in_progress={"version": "0.3.1", "previous": "0.3.0", "since": 1.0}).save(
        root / "updater" / "state.json")
    compose = FakeCompose()
    assert firstboot.run(root, compose, load=lambda p: None, path_exists=lambda p: False) == 0
    assert compose.ups == [] and list((root / "images").iterdir()) == []


def test_failures_keep_the_archives_for_the_next_try(root):
    def broken(path):
        raise firstboot.LoadError(path.name)
    with pytest.raises(firstboot.LoadError):
        firstboot.run(root, FakeCompose(), load=broken, path_exists=lambda p: True)
    with pytest.raises(bundles.ComposeError):
        firstboot.run(root, FakeCompose(fail=True), load=lambda p: None, path_exists=lambda p: True)
    assert len(list((root / "images").iterdir())) == 2


def test_nothing_to_do_without_archives(tmp_path):
    compose = FakeCompose()
    assert firstboot.run(tmp_path, compose, load=lambda p: None) == 0 and compose.ups == []
