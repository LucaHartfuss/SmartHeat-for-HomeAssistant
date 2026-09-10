import json
import os
from pathlib import Path

from heizungsbruecke.backup_store import save_backup, load_backup


def test_load_backup_returns_empty_dict_when_file_missing(tmp_path):
    assert load_backup(tmp_path / "backup.json") == {}


def test_save_then_load_backup_roundtrips(tmp_path):
    path = tmp_path / "backup.json"
    save_backup(path, {"curve_current": 0.7, "offset_current": 25.7})
    assert load_backup(path) == {"curve_current": 0.7, "offset_current": 25.7}


def test_save_backup_overwrites_previous_content(tmp_path):
    path = tmp_path / "backup.json"
    save_backup(path, {"curve_current": 0.7})
    save_backup(path, {"curve_current": 0.8})
    assert load_backup(path) == {"curve_current": 0.8}


def test_save_backup_leaves_no_temp_file_behind(tmp_path):
    path = tmp_path / "backup.json"
    save_backup(path, {"curve_current": 0.7})
    assert not (tmp_path / "backup.json.tmp").exists()
    assert list(tmp_path.iterdir()) == [path]


def test_save_backup_writes_via_atomic_rename(tmp_path, monkeypatch):
    import heizungsbruecke.backup_store as backup_store_module

    path = tmp_path / "backup.json"
    replace_calls = []
    original_replace = os.replace

    def spy_replace(src, dst):
        # At the moment of replace, the temp file must already hold the full content.
        assert Path(src).read_text() == json.dumps({"curve_current": 0.9})
        replace_calls.append((src, dst))
        return original_replace(src, dst)

    monkeypatch.setattr(backup_store_module.os, "replace", spy_replace)

    save_backup(path, {"curve_current": 0.9})

    assert len(replace_calls) == 1
    assert load_backup(path) == {"curve_current": 0.9}
