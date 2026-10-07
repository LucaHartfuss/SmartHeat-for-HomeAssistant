"""Init-Schritt (Plan G2b-1 Task 7, Spec G2b-1 3)."""
import base64
import hashlib
import json
import stat

from smartheat_gateway import init


def _run(tmp_path, token=None):
    counter = iter(range(1000))
    token = token or (lambda n=32: f"test-pw-{next(counter)}")
    return init.ensure(tmp_path / "bus", tmp_path / "zigbee2mqtt", tmp_path / "data", "ember", token=token)


def test_first_run_creates_everything_private(tmp_path):
    written = _run(tmp_path)
    for service in init.SERVICES:
        creds = tmp_path / "bus" / "credentials" / service / "bus.json"
        data = json.loads(creds.read_text())
        assert data["username"] == service and data["password"].startswith("test-pw-")
        assert stat.S_IMODE(creds.stat().st_mode) == 0o600
    for name in ("passwd", "acl"):
        assert stat.S_IMODE((tmp_path / "bus" / "mosquitto" / name).stat().st_mode) == 0o600
    config = (tmp_path / "zigbee2mqtt" / "configuration.yaml").read_text()
    assert "user: zigbee2mqtt" in config and 'password: "test-pw-' in config and "permit_join" not in config
    assert stat.S_IMODE((tmp_path / "zigbee2mqtt" / "configuration.yaml").stat().st_mode) == 0o600
    for name in ("device", "agent"):
        assert stat.S_IMODE((tmp_path / "data" / name).stat().st_mode) == 0o700
    assert "bus/mosquitto/passwd" in written


def test_second_run_keeps_passwords_and_writes_nothing(tmp_path):
    _run(tmp_path)
    before = {p: p.read_bytes() for p in (tmp_path / "bus").rglob("*") if p.is_file()}
    assert _run(tmp_path, token=lambda n=32: "test-anderes") == []
    assert {p: p.read_bytes() for p in (tmp_path / "bus").rglob("*") if p.is_file()} == before


def test_missing_passwd_is_rebuilt_from_existing_credentials(tmp_path):
    _run(tmp_path)
    (tmp_path / "bus" / "mosquitto" / "passwd").unlink()
    assert _run(tmp_path) == ["bus/mosquitto/passwd"]
    users = [line.split(":", 1)[0] for line in (tmp_path / "bus" / "mosquitto" / "passwd").read_text().splitlines()]
    assert users == list(init.SERVICES)


def test_mosquitto_hash_format():
    salt = b"0123456789ab"
    line = init.mosquitto_hash("test-pw", salt=salt, iterations=101)
    _, seven, iterations, salt_b64, hash_b64 = line.split("$")
    assert (seven, iterations) == ("7", "101")
    assert base64.b64decode(salt_b64) == salt
    assert base64.b64decode(hash_b64) == hashlib.pbkdf2_hmac("sha512", b"test-pw", salt, 101, 64)


def test_acl_lets_each_service_write_only_its_own_topics():
    acl = init.render_acl()
    blocks = {block.splitlines()[0]: block for block in acl.strip().split("\n\n")}
    assert "topic write shg/cmd/#" in blocks["user agent"]
    assert "topic write shg/cmd/#" not in blocks["user runtime"] + blocks["user zigbee2mqtt"]
    assert "topic write shg/status" in blocks["user runtime"]
    assert "topic write shg/status" not in blocks["user agent"] + blocks["user zigbee2mqtt"]
    assert blocks["user zigbee2mqtt"].splitlines()[1:] == ["topic readwrite zigbee2mqtt/#"]
    assert "topic write zigbee2mqtt/+/set" in blocks["user runtime"]
    assert "topic write zigbee2mqtt/bridge/request/#" in blocks["user agent"]


def test_adapter_from_the_host_file(tmp_path, monkeypatch):
    (tmp_path / "host").mkdir()
    (tmp_path / "host" / "zigbee_adapter").write_text("zstack\n")
    assert init.adapter(tmp_path / "host", default="ember") == "zstack"
    assert init.adapter(tmp_path / "nichtda", default="ember") == "ember"


def test_unreadable_credentials_are_replaced_and_passwd_follows(tmp_path):
    _run(tmp_path)
    (tmp_path / "bus" / "credentials" / "runtime" / "bus.json").write_text("{kaputt")
    written = _run(tmp_path, token=lambda n=32: "test-neu")
    assert written == ["bus/credentials/runtime/bus.json", "bus/mosquitto/passwd"]
    assert json.loads((tmp_path / "bus" / "credentials" / "runtime" / "bus.json").read_text())["password"] == "test-neu"


def test_stale_passwd_is_rebuilt(tmp_path):
    """Zurueckgespielte Sicherung: bus/passwd passt nicht mehr zu den Zugangsdateien (Plan G2b-2 Task 3)."""
    _run(tmp_path)
    passwd = tmp_path / "bus" / "mosquitto" / "passwd"
    passwd.write_text("".join(f"{s}:{init.mosquitto_hash('test-veraltet')}\n" for s in init.SERVICES))
    assert _run(tmp_path) == ["bus/mosquitto/passwd"]
    entries = dict(line.split(":", 1) for line in passwd.read_text().splitlines())
    for service in init.SERVICES:
        password = json.loads((tmp_path / "bus" / "credentials" / service / "bus.json").read_text())["password"]
        assert init.mosquitto_verify(password, entries[service])


def test_passwd_with_a_missing_user_is_rebuilt(tmp_path):
    _run(tmp_path)
    passwd = tmp_path / "bus" / "mosquitto" / "passwd"
    passwd.write_text("".join(line + "\n" for line in passwd.read_text().splitlines() if not line.startswith("agent:")))
    assert _run(tmp_path) == ["bus/mosquitto/passwd"]


def test_new_zigbee2mqtt_password_reaches_its_configuration(tmp_path):
    """Schluesseltausch: Zugangsdatei geloescht -> neues Passwort in passwd UND in configuration.yaml."""
    _run(tmp_path)
    (tmp_path / "bus" / "credentials" / "zigbee2mqtt" / "bus.json").unlink()
    written = _run(tmp_path, token=lambda n=32: "test-neu")
    assert written == ["bus/credentials/zigbee2mqtt/bus.json", "bus/mosquitto/passwd",
                       "zigbee2mqtt/configuration.yaml (Zugangsdaten)"]
    assert 'password: "test-neu"' in (tmp_path / "zigbee2mqtt" / "configuration.yaml").read_text()


def test_mosquitto_verify():
    line = init.mosquitto_hash("test-pw")
    assert init.mosquitto_verify("test-pw", line) and not init.mosquitto_verify("test-anders", line)
    for broken in ("", "$6$abc$def", "$7$x$y$z", "$7$101$%%%$%%%"):
        assert not init.mosquitto_verify("test-pw", broken)


def test_binary_corrupt_passwd_is_rebuilt(tmp_path):
    _run(tmp_path)
    passwd = tmp_path / "bus" / "mosquitto" / "passwd"
    passwd.write_bytes(b"\xff\xfe\x80 kaputt")
    assert _run(tmp_path) == ["bus/mosquitto/passwd"]


def test_mosquitto_verify_rejects_absurd_iteration_counts():
    for count in ("99999999999", "1000001", "99999999999999999999999999"):
        assert not init.mosquitto_verify("test-pw", f"$7${count}$YWJj$ZGVm")
