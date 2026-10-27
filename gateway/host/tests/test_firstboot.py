import inspect
import json
import logging
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from smartheat_host import bundles, firstboot, updater

COMPOSE = ('services:\n  zigbee2mqtt:\n    image: koenkk/zigbee2mqtt@sha256:' + "a" * 64 +
           '\n    devices: ["/dev/zigbee:/dev/zigbee"]\n')
FIXTURE_BUNDLE = Path(__file__).parent / "fixtures" / "bundle"
ARCHIV_A, ARCHIV_B = "a" * 64, "b" * 64  # Archive heissen nach der SHA-256 ihres Inhalts


class FakeCompose:
    def __init__(self, fail=False):
        self.ups, self.fail = [], fail

    def up(self, version):
        self.ups.append(version)
        if self.fail:
            raise bundles.ComposeError("boom")


@pytest.fixture
def root(tmp_path):
    bundles.BundleStore(tmp_path / "daten").install(
        "0.3.0", {"docker-compose.yml": COMPOSE.encode(), "mosquitto.conf": b""})
    updater.State(current="0.3.0").save(tmp_path / "daten" / "updater" / "state.json")
    return tmp_path / "daten"


@pytest.fixture
def werkein(tmp_path):
    """Die Werkseinstellung im Image: zwei Container-Archive."""
    ordner = tmp_path / "werkseinstellung"
    (ordner / "images").mkdir(parents=True)
    for digest in (ARCHIV_A, ARCHIV_B):
        (ordner / "images" / f"{digest}.tar").write_bytes(b"x")
    return ordner


def _lauf(root, compose, werkein, **kwargs):
    kwargs.setdefault("path_exists", lambda p: True)
    kwargs.setdefault("load", lambda p: None)
    return firstboot.run(root, compose, werkein, **kwargs)


def _merker(root):
    ordner = root / "host" / "geladen"
    return sorted(p.name for p in ordner.iterdir()) if ordner.is_dir() else []


def test_loads_starts_and_keeps_the_archives(root, werkein):
    loaded, compose = [], FakeCompose()
    assert _lauf(root, compose, werkein, load=lambda path: loaded.append(path.name)) == 0
    assert loaded == [f"{ARCHIV_A}.tar", f"{ARCHIV_B}.tar"] and compose.ups == ["0.3.0"]
    assert sorted(p.name for p in (werkein / "images").iterdir()) == [f"{ARCHIV_A}.tar", f"{ARCHIV_B}.tar"]
    assert _merker(root) == [ARCHIV_A, ARCHIV_B]


def test_every_start_starts_the_bundle_but_each_archive_is_loaded_only_once(root, werkein):
    """GPC31: jeder Start ruft compose.up(current); je Archiv-Digest laedt er einmal (Merker unter host/geladen/)."""
    loaded, compose = [], FakeCompose()
    for _ in range(3):
        assert _lauf(root, compose, werkein, load=lambda path: loaded.append(path.name)) == 0
    assert loaded == [f"{ARCHIV_A}.tar", f"{ARCHIV_B}.tar"]
    assert compose.ups == ["0.3.0"] * 3


def test_a_new_archive_in_a_new_system_is_loaded_and_the_known_ones_are_not(root, werkein):
    loaded = []
    _lauf(root, FakeCompose(), werkein, load=lambda path: loaded.append(path.stem))
    neu = "c" * 64
    (werkein / "images" / f"{neu}.tar").write_bytes(b"y")
    loaded.clear()
    _lauf(root, FakeCompose(), werkein, load=lambda path: loaded.append(path.stem))
    assert loaded == [neu] and _merker(root) == [ARCHIV_A, ARCHIV_B, neu]


def test_a_marker_is_only_written_after_a_successful_load(root, werkein):
    def zweites_scheitert(path):
        if path.stem == ARCHIV_B:
            raise bundles.LoadError(path.name)
    compose = FakeCompose()
    with pytest.raises(bundles.LoadError):
        _lauf(root, compose, werkein, load=zweites_scheitert)
    assert _merker(root) == [ARCHIV_A] and compose.ups == []
    assert sorted(p.name for p in (werkein / "images").iterdir()) == [f"{ARCHIV_A}.tar", f"{ARCHIV_B}.tar"]
    loaded = []  # der naechste Versuch holt nur nach, was fehlt
    _lauf(root, compose, werkein, load=lambda path: loaded.append(path.stem))
    assert loaded == [ARCHIV_B] and compose.ups == ["0.3.0"]


def test_files_in_the_archive_folder_that_are_no_digest_archives_are_skipped_with_a_warning(root, werkein, caplog):
    for name in ("README", "01.tar", ("A" * 64) + ".tar", ("a" * 63) + ".tar", ("a" * 64) + ".tar.part"):
        (werkein / "images" / name).write_bytes(b"x")
    (werkein / "images" / ("d" * 64 + ".tar")).mkdir()  # ein Ordner mit dem Namen eines Archivs
    loaded = []
    with caplog.at_level(logging.WARNING):
        assert _lauf(root, FakeCompose(), werkein, load=lambda path: loaded.append(path.name)) == 0
    assert loaded == [f"{ARCHIV_A}.tar", f"{ARCHIV_B}.tar"]
    uebergangen = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(uebergangen) == 6
    assert any("README" in text for text in uebergangen)


def test_without_an_archive_folder_the_bundle_is_still_started(root, tmp_path):
    compose = FakeCompose()
    assert _lauf(root, compose, tmp_path / "gibt-es-nicht") == 0 and compose.ups == ["0.3.0"]


def test_missing_stick_waits_without_loading(root, werkein):
    loaded, compose = [], FakeCompose()
    code = _lauf(root, compose, werkein, load=lambda p: loaded.append(p), path_exists=lambda p: False)
    assert code == firstboot.EXIT_RETRY
    assert loaded == [] and compose.ups == [] and _merker(root) == []


def test_open_updater_job_only_loads(root, werkein):
    updater.State(current="0.3.0", in_progress={"version": "0.3.1", "previous": "0.3.0", "since": 1.0}).save(
        root / "updater" / "state.json")
    compose, loaded = FakeCompose(), []
    assert _lauf(root, compose, werkein, load=lambda p: loaded.append(p.stem), path_exists=lambda p: False) == 0
    assert loaded == [ARCHIV_A, ARCHIV_B] and compose.ups == []


SYSTEM_AUFTRAG = {"version": "0.3.1", "phase": "installiere", "slot_alt": "A", "seit": None, "grund": None, "ablehnen": None}


def test_firstboot_startet_nichts_bei_offenem_systemauftrag(root, werkein):
    """Ein offener Systemwechsel (Phase `installiere`, noch ohne in_progress) gehoert dem Updater: der Start laedt nur."""
    updater.State(current="0.3.0", system=dict(SYSTEM_AUFTRAG)).save(root / "updater" / "state.json")
    compose, loaded = FakeCompose(), []
    assert _lauf(root, compose, werkein, load=lambda p: loaded.append(p.stem), path_exists=lambda p: False) == 0
    assert loaded == [ARCHIV_A, ARCHIV_B] and compose.ups == []


def test_firstboot_startet_nichts_wenn_der_systemauftrag_waehrend_des_ladens_beginnt(root, werkein):
    """Wie der Auftrag mit in_progress: eroeffnet der Updater den Systemwechsel erst waehrend des Ladens, startet der
    Start danach nichts."""
    def laden(archiv):
        updater.State(current="0.3.0", system=dict(SYSTEM_AUFTRAG)).save(root / "updater" / "state.json")

    compose = FakeCompose()
    assert _lauf(root, compose, werkein, load=laden) == 0
    assert compose.ups == []


def test_without_current_bundle_only_loads(root, werkein):
    updater.State().save(root / "updater" / "state.json")
    loaded, compose = [], FakeCompose()
    assert _lauf(root, compose, werkein, load=lambda p: loaded.append(p.stem), path_exists=lambda p: False) == 0
    assert loaded == [ARCHIV_A, ARCHIV_B] and compose.ups == []


def test_failures_keep_the_archives_and_start_nothing(root, werkein):
    def broken(path):
        raise bundles.LoadError(path.name)
    with pytest.raises(bundles.LoadError):
        _lauf(root, FakeCompose(), werkein, load=broken)
    with pytest.raises(bundles.ComposeError):
        _lauf(root, FakeCompose(fail=True), werkein)
    assert len(list((werkein / "images").iterdir())) == 2


def test_updater_job_opened_while_loading_wins_over_the_start(root, werkein):
    compose = FakeCompose()

    def load_while_updater_takes_over(path):
        updater.State(current="0.3.0", in_progress={"version": "0.3.1", "previous": "0.3.0", "since": 1.0}).save(
            root / "updater" / "state.json")
    assert _lauf(root, compose, werkein, load=load_while_updater_takes_over) == 0
    assert compose.ups == []


def test_current_switched_while_loading_is_not_started_again(root, werkein):
    compose = FakeCompose()

    def load_while_updater_switches(path):
        updater.State(current="0.3.1", previous="0.3.0").save(root / "updater" / "state.json")
    assert _lauf(root, compose, werkein, load=load_while_updater_switches) == 0
    assert compose.ups == []


def test_the_start_loads_with_the_docker_load_of_the_bundles_module():
    """Ein `docker load` mit Frist fuer den Start und den Updater (bundles.docker_load); firstboot hat keine eigene."""
    assert inspect.signature(firstboot.run).parameters["load"].default is bundles.docker_load
    assert not hasattr(firstboot, "docker_load") and not hasattr(firstboot, "LoadError")


def test_a_hanging_docker_load_fails_the_start_and_leaves_no_marker(root, werkein, monkeypatch):
    def hanging(cmd, **kw):
        assert kw["timeout"] == bundles.LOAD_TIMEOUT_SECONDS == 600
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(bundles.subprocess, "run", hanging)
    compose = FakeCompose()
    with pytest.raises(bundles.LoadError, match="Frist"):
        firstboot.run(root, compose, werkein, path_exists=lambda p: True)
    assert compose.ups == [] and _merker(root) == []


def test_run_defaults_to_the_werkseinstellung_of_the_image():
    assert inspect.signature(firstboot.run).parameters["werkseinstellung"].default == Path("/opt/smartheat/werkseinstellung")
    assert str(firstboot.WERKSEINSTELLUNG) == "/opt/smartheat/werkseinstellung"


# --- main ---------------------------------------------------------------------------------------------------------


def test_main_start_maps_failures_to_exit_code_1(root, monkeypatch):
    monkeypatch.setenv("SHG_ROOT", str(root))

    def broken(*args, **kwargs):
        raise bundles.LoadError("boom")
    monkeypatch.setattr(firstboot, "run", broken)
    with pytest.raises(SystemExit) as stop:
        firstboot.main(["start"])
    assert stop.value.code == 1


def test_main_starts_the_bundle_in_the_project_smartheat_whatever_the_environment_says(root, monkeypatch):
    """Der Projektname ist `bundles.COMPOSE_PROJECT` (install.sh startet mit `-p smartheat`); `SHG_COMPOSE_PROJECT`
    aus der Umgebung gilt nicht mehr."""
    monkeypatch.setenv("SHG_ROOT", str(root))
    monkeypatch.setenv("SHG_COMPOSE_PROJECT", "anders")
    seen = []
    monkeypatch.setattr(firstboot, "run", lambda wurzel, compose: seen.append(compose) or 0)
    with pytest.raises(SystemExit) as stop:
        firstboot.main(["start"])
    assert stop.value.code == 0
    command = seen[0]._command("0.3.0")
    assert command[command.index("-p") + 1] == bundles.COMPOSE_PROJECT == "smartheat"


@pytest.mark.parametrize("argv", [[], ["stop"], ["start", "ablage"], ["ablage", "x"]])
def test_main_knows_only_ablage_and_start(argv, capsys):
    with pytest.raises(SystemExit) as stop:
        firstboot.main(argv)
    assert stop.value.code == 2 and "ablage|start" in capsys.readouterr().err


def test_main_ablage_gleicht_die_ordner_an_und_legt_die_werkseinstellung_an(root, monkeypatch):
    monkeypatch.setenv("SHG_ROOT", str(root))
    aufrufe = []
    monkeypatch.setattr(firstboot, "ordner_angleichen", lambda wurzel: aufrufe.append(("ordner", wurzel)))
    monkeypatch.setattr(firstboot, "anlegen", lambda wurzel: aufrufe.append(("anlegen", wurzel)) or True)
    with pytest.raises(SystemExit) as stop:
        firstboot.main(["ablage"])
    assert stop.value.code == 0 and aufrufe == [("ordner", root), ("anlegen", root)]  # erst die Ordner, dann der Zustand


@pytest.mark.parametrize("fehler", [firstboot.AblageFehler("bus ist ein Symlink"), bundles.ManifestError("kaputt"),
                                    PermissionError("nein")])
def test_main_ablage_maps_failures_to_exit_code_1_and_logs_the_cause(root, monkeypatch, caplog, fehler):
    monkeypatch.setenv("SHG_ROOT", str(root))

    def kaputt(wurzel):
        raise fehler
    monkeypatch.setattr(firstboot, "ordner_angleichen", kaputt)
    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as stop:
        firstboot.main(["ablage"])
    assert stop.value.code == 1
    assert any(str(fehler) in r.getMessage() and r.levelno == logging.ERROR for r in caplog.records)


# --- anlegen ------------------------------------------------------------------------------------------------------


@pytest.fixture
def werkbundle(tmp_path):
    """Werkseinstellung mit dem ersten Bundle (Fixture-Bundle 0.2.0, plus Signatur) und der state.json-Vorlage."""
    ordner = tmp_path / "werkseinstellung"
    shutil.copytree(FIXTURE_BUNDLE, ordner / "bundle")
    (ordner / "bundle" / "manifest.json.sig").write_text('{"format": "smartheat-kette-1"}')
    vorlage = {"current": "0.2.0", "in_progress": None, "previous": None, "rejected": [],
               "zertifikat_untergrenze": "2026-03-01T00:00:00+00:00"}
    (ordner / "state.json").write_text(json.dumps(vorlage))
    return ordner


def test_anlegen_legt_das_erste_bundle_und_den_zustand_an(tmp_path, werkbundle):
    root = tmp_path / "daten"
    (root / "updater").mkdir(parents=True)
    assert firstboot.anlegen(root, werkbundle) is True
    for name in ("docker-compose.yml", "mosquitto.conf", "manifest.json", "manifest.json.sig"):
        assert (root / "bundles" / "0.2.0" / name).read_bytes() == (werkbundle / "bundle" / name).read_bytes(), name
    state = root / "updater" / "state.json"
    assert state.read_bytes() == (werkbundle / "state.json").read_bytes()
    assert (state.stat().st_mode & 0o777) == 0o644
    geladen = updater.State.load(state)
    assert geladen.current == "0.2.0" and geladen.zertifikat_untergrenze == "2026-03-01T00:00:00+00:00"
    assert bundles.BundleStore(root).exists("0.2.0")


def test_anlegen_legt_fehlende_ordner_an_und_braucht_keine_signatur(tmp_path, werkbundle):
    (werkbundle / "bundle" / "manifest.json.sig").unlink()
    root = tmp_path / "daten"
    assert firstboot.anlegen(root, werkbundle) is True
    assert not (root / "bundles" / "0.2.0" / "manifest.json.sig").exists()
    assert (root / "updater" / "state.json").is_file()


def test_anlegen_laesst_einen_bestehenden_stand_stehen(tmp_path, werkbundle):
    root = tmp_path / "daten"
    firstboot.anlegen(root, werkbundle)
    updater.State(current="0.4.0", previous="0.2.0").save(root / "updater" / "state.json")  # der Updater hat gearbeitet
    (root / "bundles" / "0.2.0" / "mosquitto.conf").write_text("geaendert")
    assert firstboot.anlegen(root, werkbundle) is False
    assert updater.State.load(root / "updater" / "state.json").current == "0.4.0"
    assert (root / "bundles" / "0.2.0" / "mosquitto.conf").read_text() == "geaendert"


@pytest.mark.parametrize("inhalt", ["", "{", "kaputt"])
def test_anlegen_ueberschreibt_auch_keinen_kaputten_zustand(tmp_path, werkbundle, inhalt):
    root = tmp_path / "daten"
    (root / "updater").mkdir(parents=True)
    (root / "updater" / "state.json").write_text(inhalt)
    assert firstboot.anlegen(root, werkbundle) is False
    assert (root / "updater" / "state.json").read_text() == inhalt and not (root / "bundles").exists()


def test_anlegen_ohne_werkseinstellung_tut_nichts(tmp_path, caplog):
    root = tmp_path / "daten"
    with caplog.at_level(logging.WARNING):
        assert firstboot.anlegen(root, tmp_path / "gibt-es-nicht") is False
    assert not (root / "updater" / "state.json").exists()
    assert any(r.levelno == logging.WARNING for r in caplog.records)


@pytest.mark.parametrize("fehlt", ["bundle", "state.json"])
def test_anlegen_mit_unvollstaendiger_werkseinstellung_tut_nichts(tmp_path, werkbundle, fehlt):
    ziel = werkbundle / fehlt
    shutil.rmtree(ziel) if ziel.is_dir() else ziel.unlink()
    root = tmp_path / "daten"
    assert firstboot.anlegen(root, werkbundle) is False
    assert not (root / "updater" / "state.json").exists() and not (root / "bundles").exists()


def test_anlegen_lehnt_eine_vorlage_mit_anderer_version_ab_und_legt_nichts_an(tmp_path, werkbundle):
    (werkbundle / "state.json").write_text('{"current": "0.9.9"}')
    root = tmp_path / "daten"
    with pytest.raises(firstboot.AblageFehler, match="0.2.0"):
        firstboot.anlegen(root, werkbundle)
    assert not (root / "updater" / "state.json").exists() and not (root / "bundles").exists()


def test_anlegen_lehnt_eine_kaputte_vorlage_ab(tmp_path, werkbundle):
    (werkbundle / "state.json").write_text("kaputt")
    with pytest.raises(firstboot.AblageFehler):
        firstboot.anlegen(tmp_path / "daten", werkbundle)


def test_anlegen_lehnt_ein_kaputtes_manifest_ab_und_legt_nichts_an(tmp_path, werkbundle):
    (werkbundle / "bundle" / "manifest.json").write_text('{"version": "0.2.0"}')
    root = tmp_path / "daten"
    with pytest.raises(bundles.ManifestError):
        firstboot.anlegen(root, werkbundle)
    assert not (root / "updater" / "state.json").exists() and not (root / "bundles").exists()


def test_anlegen_holt_nach_einem_abbruch_vor_dem_zustand_alles_nach(tmp_path, werkbundle, monkeypatch):
    """Der Zustand kommt zuletzt: bricht der Strom nach dem Bundle ab, fehlt state.json und der naechste Start legt an."""
    root = tmp_path / "daten"
    echt = firstboot.dateien.schreiben

    def bricht_ab(*args, **kwargs):
        raise OSError("Strom weg")
    monkeypatch.setattr(firstboot.dateien, "schreiben", bricht_ab)
    with pytest.raises(OSError):
        firstboot.anlegen(root, werkbundle)
    assert bundles.BundleStore(root).exists("0.2.0") and not (root / "updater" / "state.json").exists()
    monkeypatch.setattr(firstboot.dateien, "schreiben", echt)
    assert firstboot.anlegen(root, werkbundle) is True
    assert (root / "updater" / "state.json").is_file()


# --- ordner_angleichen --------------------------------------------------------------------------------------------

ERWARTET = {
    ".": (0o755, 0, 0),
    "data": (0o755, 1000, 1000),
    "zigbee2mqtt": (0o700, 1000, 1000),
    "bus": (0o700, 1000, 1000),
    "bus/mosquitto": (0o700, 1000, 1000),
    "bus/mosquitto-daten": (0o700, 1000, 1000),
    "bus/credentials": (0o700, 1000, 1000),
    "bus/credentials/runtime": (0o700, 1000, 1000),
    "bus/credentials/zigbee2mqtt": (0o700, 1000, 1000),
    "host": (0o755, 0, 0),
    "host/geladen": (0o755, 0, 0),
    "bundles": (0o755, 0, 0),
    "updater": (0o755, 0, 0),
}


class Besitzer:
    """Ersatz fuer os.fchown (die Tests laufen ohne root): merkt sich je Ordner (Inode) den verlangten Besitzer."""

    def __init__(self):
        self.gesetzt: dict[int, tuple[int, int]] = {}

    def __call__(self, fd, uid, gid):
        self.gesetzt[os.fstat(fd).st_ino] = (uid, gid)

    def von(self, pfad: Path):
        return self.gesetzt.get(pfad.stat().st_ino)


def _stand(root: Path, besitzer: Besitzer) -> dict[str, tuple[int, int, int]]:
    stand = {}
    for rel in ERWARTET:
        pfad = root if rel == "." else root / rel
        if pfad.is_dir():
            uid, gid = besitzer.von(pfad) or (None, None)
            stand[rel] = (stat.S_IMODE(pfad.stat().st_mode), uid, gid)
    return stand


def test_ordner_angleichen_legt_alles_mit_modus_und_besitzer_an(tmp_path):
    root, besitzer = tmp_path / "daten", Besitzer()
    firstboot.ordner_angleichen(root, fchown=besitzer)
    assert _stand(root, besitzer) == ERWARTET


def test_die_tabelle_deckt_die_ordner_des_alten_installers_und_mosquitto_daten(tmp_path):
    assert {rel or "." for rel, *_ in firstboot.ORDNER} == set(ERWARTET)


def test_ordner_angleichen_setzt_falsche_modi_neu_und_laesst_den_inhalt_stehen(tmp_path):
    root, besitzer = tmp_path / "daten", Besitzer()
    firstboot.ordner_angleichen(root, fchown=besitzer)
    for rel in ("bus", "bus/credentials/runtime", "data", "host", "."):
        (root if rel == "." else root / rel).chmod(0o777)
    (root / "data" / "geraet.json").write_text("bleibt")
    (root / "archive").mkdir()
    (root / "archive" / "x").write_text("fremd")
    (root / "archive").chmod(0o711)
    firstboot.ordner_angleichen(root, fchown=besitzer)
    assert _stand(root, besitzer) == ERWARTET
    assert (root / "data" / "geraet.json").read_text() == "bleibt"
    assert stat.S_IMODE((root / "archive").stat().st_mode) == 0o711 and (root / "archive" / "x").read_text() == "fremd"


def test_ordner_angleichen_setzt_den_besitzer_auch_bei_vorhandenen_ordnern_neu(tmp_path):
    root = tmp_path / "daten"
    firstboot.ordner_angleichen(root, fchown=Besitzer())
    frisch = Besitzer()
    firstboot.ordner_angleichen(root, fchown=frisch)
    assert _stand(root, frisch) == ERWARTET  # jeder Ordner wurde (wieder) auf den Besitzer gesetzt


def test_ordner_angleichen_ist_idempotent(tmp_path):
    root, besitzer = tmp_path / "daten", Besitzer()
    firstboot.ordner_angleichen(root, fchown=besitzer)
    erste = {p: (p.stat().st_mtime_ns, stat.S_IMODE(p.stat().st_mode)) for p in [root, *root.rglob("*")]}
    firstboot.ordner_angleichen(root, fchown=besitzer)
    zweite = {p: (p.stat().st_mtime_ns, stat.S_IMODE(p.stat().st_mode)) for p in [root, *root.rglob("*")]}
    assert erste == zweite and _stand(root, besitzer) == ERWARTET


@pytest.mark.parametrize("rel", ["bus", "bus/credentials", "bus/credentials/runtime", "data", "host", "updater"])
def test_ein_symlink_statt_eines_ordners_wird_nicht_angefasst_der_rest_wird_angeglichen(tmp_path, rel):
    root, besitzer = tmp_path / "daten", Besitzer()
    firstboot.ordner_angleichen(root, fchown=besitzer)
    ziel = tmp_path / "fremd"
    ziel.mkdir()
    ziel.chmod(0o755)
    (ziel / "datei").write_text("keep")
    opfer = root / rel
    shutil.rmtree(opfer)
    opfer.symlink_to(ziel)
    (root / "bundles").chmod(0o777)  # ein anderer Ordner, der trotzdem angeglichen werden muss
    frisch = Besitzer()
    with pytest.raises(firstboot.AblageFehler, match="Symlink") as fehler:
        firstboot.ordner_angleichen(root, fchown=frisch)
    assert opfer.name in str(fehler.value)
    assert stat.S_IMODE(ziel.stat().st_mode) == 0o755 and frisch.von(ziel) is None
    assert (ziel / "datei").read_text() == "keep" and opfer.is_symlink()
    assert stat.S_IMODE((root / "bundles").stat().st_mode) == 0o755


def test_ein_symlink_auf_dem_weg_wird_nicht_durchschritten(tmp_path):
    """bus ist ein Symlink: bus/mosquitto darf weder durch ihn angelegt noch angeglichen werden."""
    root = tmp_path / "daten"
    root.mkdir()
    ziel = tmp_path / "fremd"
    ziel.mkdir()
    (root / "bus").symlink_to(ziel)
    besitzer = Besitzer()
    with pytest.raises(firstboot.AblageFehler, match="Symlink"):
        firstboot.ordner_angleichen(root, fchown=besitzer)
    assert list(ziel.iterdir()) == [] and besitzer.von(ziel) is None


def test_eine_datei_statt_eines_ordners_wird_gemeldet(tmp_path):
    root = tmp_path / "daten"
    root.mkdir()
    (root / "data").write_text("datei")
    with pytest.raises(firstboot.AblageFehler, match="data: .*kein Ordner"):
        firstboot.ordner_angleichen(root, fchown=Besitzer())
    assert (root / "data").read_text() == "datei"


def test_ein_symlink_als_wurzel_wird_abgelehnt(tmp_path):
    echt = tmp_path / "echt"
    echt.mkdir()
    echt.chmod(0o700)
    link = tmp_path / "daten"
    link.symlink_to(echt)
    besitzer = Besitzer()
    with pytest.raises(firstboot.AblageFehler, match="Symlink"):
        firstboot.ordner_angleichen(link, fchown=besitzer)
    assert stat.S_IMODE(echt.stat().st_mode) == 0o700 and list(echt.iterdir()) == []


def test_mehrere_verstoesse_werden_zusammen_gemeldet(tmp_path):
    root = tmp_path / "daten"
    root.mkdir()
    (root / "data").symlink_to(tmp_path)
    (root / "host").write_text("datei")
    with pytest.raises(firstboot.AblageFehler) as fehler:
        firstboot.ordner_angleichen(root, fchown=Besitzer())
    assert "data" in str(fehler.value) and "host" in str(fehler.value)
    assert (root / "bundles").is_dir()  # der Rest wurde trotzdem angelegt


def test_ordner_angleichen_nutzt_standardmaessig_os_fchown():
    assert inspect.signature(firstboot.ordner_angleichen).parameters["fchown"].default is os.fchown
