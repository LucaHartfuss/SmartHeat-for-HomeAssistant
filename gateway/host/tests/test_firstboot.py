import inspect
import subprocess

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
    compose, loaded = FakeCompose(), []
    assert firstboot.run(root, compose, load=lambda p: loaded.append(p.name), path_exists=lambda p: False) == 0
    assert loaded == ["01.tar", "02.tar"]
    assert compose.ups == [] and list((root / "images").iterdir()) == []


SYSTEM_AUFTRAG = {"version": "0.3.1", "phase": "installiere", "slot_alt": "A", "seit": None, "grund": None, "ablehnen": None}


def test_firstboot_startet_nichts_bei_offenem_systemauftrag(root):
    """Ein offener Systemwechsel (Phase `installiere`, noch ohne in_progress) gehoert dem Updater: der Erststart laedt nur."""
    updater.State(current="0.3.0", system=dict(SYSTEM_AUFTRAG)).save(root / "updater" / "state.json")
    compose, loaded = FakeCompose(), []
    assert firstboot.run(root, compose, load=lambda p: loaded.append(p.name), path_exists=lambda p: False) == 0
    assert loaded == ["01.tar", "02.tar"] and compose.ups == []
    assert list((root / "images").iterdir()) == []


def test_firstboot_startet_nichts_wenn_der_systemauftrag_waehrend_des_ladens_beginnt(root):
    """Wie der Auftrag mit in_progress: eroeffnet der Updater den Systemwechsel erst waehrend des Ladens, startet der
    Erststart danach nichts."""
    def laden(archiv):
        updater.State(current="0.3.0", system=dict(SYSTEM_AUFTRAG)).save(root / "updater" / "state.json")

    compose = FakeCompose()
    assert firstboot.run(root, compose, load=laden, path_exists=lambda p: True) == 0
    assert compose.ups == []


def test_without_current_bundle_only_loads(root):
    updater.State().save(root / "updater" / "state.json")
    loaded, compose = [], FakeCompose()
    assert firstboot.run(root, compose, load=lambda p: loaded.append(p.name), path_exists=lambda p: False) == 0
    assert loaded == ["01.tar", "02.tar"] and compose.ups == []
    assert list((root / "images").iterdir()) == []


def test_partial_load_keeps_all_archives_and_starts_nothing(root):
    loaded, compose = [], FakeCompose()

    def second_breaks(path):
        if path.name == "02.tar":
            raise bundles.LoadError(path.name)
        loaded.append(path.name)
    with pytest.raises(bundles.LoadError):
        firstboot.run(root, compose, load=second_breaks, path_exists=lambda p: True)
    assert loaded == ["01.tar"] and compose.ups == []
    assert sorted(path.name for path in (root / "images").iterdir()) == ["01.tar", "02.tar"]


def test_main_maps_failures_to_exit_code_1(root, monkeypatch):
    monkeypatch.setenv("SHG_ROOT", str(root))

    def broken(*args, **kwargs):
        raise bundles.LoadError("boom")
    monkeypatch.setattr(firstboot, "run", broken)
    with pytest.raises(SystemExit) as stop:
        firstboot.main()
    assert stop.value.code == 1


def test_main_starts_the_bundle_in_the_project_smartheat_whatever_the_environment_says(root, monkeypatch):
    """Der Projektname ist `bundles.COMPOSE_PROJECT` (install.sh startet mit `-p smartheat`); `SHG_COMPOSE_PROJECT`
    aus der Umgebung gilt nicht mehr."""
    monkeypatch.setenv("SHG_ROOT", str(root))
    monkeypatch.setenv("SHG_COMPOSE_PROJECT", "anders")
    seen = []
    monkeypatch.setattr(firstboot, "run", lambda wurzel, compose: seen.append(compose) or 0)
    with pytest.raises(SystemExit) as stop:
        firstboot.main()
    assert stop.value.code == 0
    command = seen[0]._command("0.3.0")
    assert command[command.index("-p") + 1] == bundles.COMPOSE_PROJECT == "smartheat"


def test_updater_job_opened_while_loading_wins_over_the_start(root):
    compose = FakeCompose()

    def load_while_updater_takes_over(path):
        updater.State(current="0.3.0", in_progress={"version": "0.3.1", "previous": "0.3.0", "since": 1.0}).save(
            root / "updater" / "state.json")
    assert firstboot.run(root, compose, load=load_while_updater_takes_over, path_exists=lambda p: True) == 0
    assert compose.ups == [] and list((root / "images").iterdir()) == []


def test_current_switched_while_loading_is_not_started_again(root):
    compose = FakeCompose()

    def load_while_updater_switches(path):
        updater.State(current="0.3.1", previous="0.3.0").save(root / "updater" / "state.json")
    assert firstboot.run(root, compose, load=load_while_updater_switches, path_exists=lambda p: True) == 0
    assert compose.ups == [] and list((root / "images").iterdir()) == []


def test_failures_keep_the_archives_for_the_next_try(root):
    def broken(path):
        raise bundles.LoadError(path.name)
    with pytest.raises(bundles.LoadError):
        firstboot.run(root, FakeCompose(), load=broken, path_exists=lambda p: True)
    with pytest.raises(bundles.ComposeError):
        firstboot.run(root, FakeCompose(fail=True), load=lambda p: None, path_exists=lambda p: True)
    assert len(list((root / "images").iterdir())) == 2


def test_the_first_start_loads_with_the_docker_load_of_the_bundles_module(root):
    """Ein `docker load` mit Frist fuer den Erststart und den Updater (bundles.docker_load); firstboot hat keine eigene."""
    assert inspect.signature(firstboot.run).parameters["load"].default is bundles.docker_load
    assert not hasattr(firstboot, "docker_load") and not hasattr(firstboot, "LoadError")


def test_a_hanging_docker_load_fails_the_first_start_and_keeps_the_archives(root, monkeypatch):
    def hanging(cmd, **kw):
        assert kw["timeout"] == bundles.LOAD_TIMEOUT_SECONDS == 600
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(bundles.subprocess, "run", hanging)
    compose = FakeCompose()
    with pytest.raises(bundles.LoadError, match="Frist"):
        firstboot.run(root, compose, path_exists=lambda p: True)
    assert compose.ups == [] and len(list((root / "images").iterdir())) == 2


def test_nothing_to_do_without_archives(tmp_path):
    compose = FakeCompose()
    assert firstboot.run(tmp_path, compose, load=lambda p: None) == 0 and compose.ups == []
