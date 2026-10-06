import os
import stat

from smartheat_gateway.files import read_json, write_json


def test_atomic_private_write_and_tolerant_read(tmp_path):
    path = tmp_path / "a" / "secret.json"
    write_json(path, {"x": 1}, private=True)
    assert read_json(path) == {"x": 1}
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert not list(path.parent.glob("*.tmp"))
    path.write_text("{kaputt")
    assert read_json(path) is None
    assert read_json(tmp_path / "fehlt.json") is None
