"""Nur der Host-Dienst (root) nutzt files.py (kein smartheat_core im Host-Paket); der Container schreibt ueber
smartheat_core.dateien (tests/test_core_dateien.py im Add-on)."""
import json
import os
import stat

import pytest

from smartheat_gateway.files import write_json


def test_atomic_private_write(tmp_path):
    path = tmp_path / "a" / "secret.json"
    write_json(path, {"x": 1}, private=True)
    assert json.loads(path.read_text()) == {"x": 1}
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert not list(path.parent.glob("*.tmp"))


def test_planted_symlinks_are_replaced_never_followed(tmp_path):
    """Der Host (root) schreibt in einen Ordner, in dem ein Container (uid 1000) Symlinks anlegen kann: weder der
    Name der temporaeren Datei noch der Zielname darf auf eine fremde Datei zeigen duerfen."""
    outside = tmp_path / "fremd.txt"
    outside.write_text("unberuehrt")
    data = tmp_path / "data"
    data.mkdir()
    (data / "x.json.tmp").symlink_to(outside)
    (data / "x.json").symlink_to(outside)
    write_json(data / "x.json", {"a": 1})
    assert outside.read_text() == "unberuehrt"
    assert not (data / "x.json").is_symlink() and json.loads((data / "x.json").read_text()) == {"a": 1}
    assert not list(data.glob("*.tmp"))
    assert stat.S_IMODE(os.stat(data / "x.json").st_mode) == 0o644


def test_a_stale_tmp_file_does_not_block_the_write(tmp_path):
    (tmp_path / "x.json.tmp").write_text("alt")
    write_json(tmp_path / "x.json", {"a": 2})
    assert json.loads((tmp_path / "x.json").read_text()) == {"a": 2}


def test_a_symlinked_parent_directory_is_refused(tmp_path):
    real = tmp_path / "echt"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    with pytest.raises(OSError):
        write_json(tmp_path / "link" / "x.json", {"a": 1})
    assert not list(real.iterdir())
