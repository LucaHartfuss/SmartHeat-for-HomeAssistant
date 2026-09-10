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
