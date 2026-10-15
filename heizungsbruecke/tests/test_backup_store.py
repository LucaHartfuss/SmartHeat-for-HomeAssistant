import json
import os
import stat

from smartheat_runtime.backup_store import load_backup, save_backup


def test_load_backup_returns_empty_dict_when_file_missing(tmp_path):
    assert load_backup(tmp_path / "backup.json") == {}


def test_save_then_load_backup_roundtrips(tmp_path):
    path = tmp_path / "backup.json"
    save_backup(path, {"restore_point": {"curve": 0.7, "room_setpoint": 25.7}})
    assert load_backup(path) == {"restore_point": {"curve": 0.7, "room_setpoint": 25.7}}


def test_save_backup_overwrites_previous_content(tmp_path):
    path = tmp_path / "backup.json"
    save_backup(path, {"restore_point": {"curve": 0.7}})
    save_backup(path, {"restore_point": {"curve": 0.8}})
    assert load_backup(path) == {"restore_point": {"curve": 0.8}}


def test_save_backup_leaves_no_temp_file_behind(tmp_path):
    path = tmp_path / "backup.json"
    save_backup(path, {"restore_point": {"curve": 0.7}})
    assert not (tmp_path / "backup.json.tmp").exists()
    assert list(tmp_path.iterdir()) == [path]


def test_save_backup_writes_via_atomic_rename(tmp_path, monkeypatch):
    path = tmp_path / "backup.json"
    replace_calls = []
    original_replace = os.replace

    def spy_replace(src, dst, **kwargs):
        # At the moment of replace, the temp file must already hold the full content.
        with open(src, encoding="utf-8", opener=lambda name, flags: os.open(name, flags, dir_fd=kwargs["src_dir_fd"])) as handle:
            assert handle.read() == json.dumps({"restore_point": {"curve": 0.9}})
        replace_calls.append((src, dst))
        return original_replace(src, dst, **kwargs)

    monkeypatch.setattr("smartheat_core.dateien.os.replace", spy_replace)

    save_backup(path, {"restore_point": {"curve": 0.9}})

    assert replace_calls == [("backup.json.tmp", "backup.json")]
    assert load_backup(path) == {"restore_point": {"curve": 0.9}}


def test_save_backup_syncs_the_temp_file_before_replacing(tmp_path, monkeypatch):
    calls = []
    real_fsync, real_replace = os.fsync, os.replace

    def _fsync(fd):
        calls.append("fsync")
        real_fsync(fd)

    def _replace(*args, **kwargs):
        calls.append("replace")
        real_replace(*args, **kwargs)

    monkeypatch.setattr("smartheat_core.dateien.os.fsync", _fsync)
    monkeypatch.setattr("smartheat_core.dateien.os.replace", _replace)

    save_backup(tmp_path / "b.json", {"a": 1})

    assert calls == ["fsync", "replace", "fsync"]  # Datei, Umbenennen, dann der Ordner


def test_save_backup_keeps_the_key_order_and_is_private(tmp_path):
    path = tmp_path / "b.json"
    save_backup(path, {"z": 1, "a": 2})
    assert path.read_text() == '{"z": 1, "a": 2}' and stat.S_IMODE(path.stat().st_mode) == 0o600
