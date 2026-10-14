"""A5-60: ein atomarer Schreibweg fuer Kern, Laufzeit, Geraetekern und Gateway."""
import json
import os
import stat

import pytest

from smartheat_core import dateien


def test_json_is_written_atomically_with_the_given_mode(tmp_path):
    path = tmp_path / "a" / "b.json"
    dateien.schreiben_json(path, {"x": "ä", "a": 1}, modus=dateien.PRIVAT)
    assert path.read_text() == '{"a": 1, "x": "ä"}'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and not path.with_name("b.json.tmp").exists()
    dateien.schreiben_json(path, [1], modus=dateien.LESBAR)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644 and json.loads(path.read_text()) == [1]


def test_the_mode_is_mandatory(tmp_path):
    with pytest.raises(TypeError):
        dateien.schreiben_json(tmp_path / "x.json", {})  # type: ignore[call-arg]


def test_a_symlink_at_the_target_is_replaced_not_followed(tmp_path):
    ziel = tmp_path / "fremd.txt"
    ziel.write_text("fremd")
    (tmp_path / "x.json").symlink_to(ziel)
    dateien.schreiben_json(tmp_path / "x.json", {"a": 1}, modus=dateien.PRIVAT)
    assert ziel.read_text() == "fremd" and not (tmp_path / "x.json").is_symlink()


def test_a_symlinked_directory_is_refused(tmp_path):
    (tmp_path / "echt").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "echt", target_is_directory=True)
    with pytest.raises(OSError):
        dateien.schreiben_json(tmp_path / "link" / "x.json", {}, modus=dateien.PRIVAT)


def test_a_stale_temp_file_or_temp_symlink_is_removed(tmp_path):
    (tmp_path / "x.json.tmp").symlink_to(tmp_path / "nirgends")
    dateien.schreiben_text(tmp_path / "x.json", "neu", modus=dateien.PRIVAT)
    assert (tmp_path / "x.json").read_text() == "neu"


def test_large_data_is_written_completely(tmp_path):
    data = os.urandom(3 * 1024 * 1024)
    dateien.schreiben(tmp_path / "gross.bin", data, modus=dateien.PRIVAT)
    assert (tmp_path / "gross.bin").read_bytes() == data


def test_read_json_treats_missing_and_broken_files_as_none(tmp_path):
    (tmp_path / "kaputt.json").write_text("{")
    assert dateien.lesen_json(tmp_path / "kaputt.json") is None and dateien.lesen_json(tmp_path / "fehlt.json") is None
