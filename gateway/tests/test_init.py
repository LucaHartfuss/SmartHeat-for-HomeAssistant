"""Init-Schritt."""
import base64
import hashlib
import json
import logging
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
    for name in ("device",):
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


def _acl_blocks() -> dict[str, list[str]]:
    """ACL-Bloecke je Benutzer: "user <name>" -> Topic-Zeilen."""
    blocks = [block.splitlines() for block in init.render_acl().strip().split("\n\n")]
    return {lines[0]: lines[1:] for lines in blocks}


def test_acl_lets_each_service_write_only_its_own_topics():
    blocks = _acl_blocks()
    assert set(blocks) == {"user runtime", "user zigbee2mqtt"}
    # Genau diese Regeln: eine zusaetzliche Schreibregel (z. B. bridge/state, dem die Laufzeit vertraut) faellt auf.
    assert sorted(blocks["user runtime"]) == sorted([
        "topic read zigbee2mqtt/#", "topic write zigbee2mqtt/+/set", "topic write zigbee2mqtt/bridge/request/#"])
    assert blocks["user zigbee2mqtt"] == ["topic readwrite zigbee2mqtt/#"]
    assert not any("shg/" in line for block in blocks.values() for line in block)


def test_a_passwd_from_0_5_0_with_the_agent_user_is_rebuilt(tmp_path):
    bus = tmp_path / "bus"
    init.ensure(bus, tmp_path / "z", tmp_path / "data", "ember")
    passwd = bus / "mosquitto" / "passwd"
    passwd.write_text(passwd.read_text() + f"agent:{init.mosquitto_hash('alt')}\n")
    assert "bus/mosquitto/passwd" in init.ensure(bus, tmp_path / "z", tmp_path / "data", "ember")
    assert not any(line.startswith("agent:") for line in passwd.read_text().splitlines())


def test_adapter_from_the_host_file(tmp_path, monkeypatch, caplog):
    (tmp_path / "host").mkdir()
    (tmp_path / "host" / "zigbee_adapter").write_text("zstack\n")
    assert init.adapter(tmp_path / "host") == "zstack"
    assert init.adapter(tmp_path / "nichtda") is None  # ohne Datei kein Abgleich
    (tmp_path / "host" / "zigbee_adapter").write_text("\n")
    assert init.adapter(tmp_path / "host") is None
    assert caplog.records == []  # leer ist kein Fehler
    (tmp_path / "host" / "zigbee_adapter").write_text("zstack\nschlecht: 1\n")  # kein Name eines Adapters
    assert init.adapter(tmp_path / "host") is None
    assert "kein Adaptername" in caplog.text and "schlecht" not in caplog.text


def _run_mit_adapter(tmp_path, adapter_name):
    return init.ensure(tmp_path / "bus", tmp_path / "zigbee2mqtt", tmp_path / "data", adapter_name,
                       token=lambda n=32: "test-pw")


def test_a_stick_swap_changes_the_adapter(tmp_path):
    """Ein anderer Stick (Host-Datei zigbee_adapter) aendert nach dem ersten Schreiben nur serial.adapter."""
    _run_mit_adapter(tmp_path, "zstack")
    config = tmp_path / "zigbee2mqtt" / "configuration.yaml"
    vorher = config.read_text()
    assert _run_mit_adapter(tmp_path, "ember") == ["zigbee2mqtt/configuration.yaml (Adapter)"]
    assert config.read_text() == vorher.replace("adapter: zstack", "adapter: ember")
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert _run_mit_adapter(tmp_path, "ember") == []  # zweiter Lauf schweigt


def test_without_the_host_file_the_adapter_stays(tmp_path):
    """Ohne Host-Datei (None) bleibt der Adapter einer bestehenden Konfiguration; der Standard gilt nur beim ersten
    Schreiben."""
    _run_mit_adapter(tmp_path, "zstack")
    config = tmp_path / "zigbee2mqtt" / "configuration.yaml"
    vorher = config.read_text()
    assert _run_mit_adapter(tmp_path, None) == []
    assert config.read_text() == vorher
    erste = tmp_path / "neu"
    init.ensure(erste / "bus", erste / "zigbee2mqtt", erste / "data", None, token=lambda n=32: "test-pw")
    assert "  adapter: ember\n" in (erste / "zigbee2mqtt" / "configuration.yaml").read_text()
    init.ensure(erste / "bus2", erste / "z2", erste / "data", None, token=lambda n=32: "test-pw", default_adapter="zstack")
    assert "  adapter: zstack\n" in (erste / "z2" / "configuration.yaml").read_text()


def test_unreadable_credentials_are_replaced_and_passwd_follows(tmp_path):
    _run(tmp_path)
    (tmp_path / "bus" / "credentials" / "runtime" / "bus.json").write_text("{kaputt")
    written = _run(tmp_path, token=lambda n=32: "test-neu")
    assert written == ["bus/credentials/runtime/bus.json", "bus/mosquitto/passwd"]
    assert json.loads((tmp_path / "bus" / "credentials" / "runtime" / "bus.json").read_text())["password"] == "test-neu"


def test_stale_passwd_is_rebuilt(tmp_path):
    """Zurueckgespielte Sicherung: bus/passwd passt nicht mehr zu den Zugangsdateien."""
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
    passwd.write_text("".join(line + "\n" for line in passwd.read_text().splitlines() if not line.startswith("runtime:")))
    assert _run(tmp_path) == ["bus/mosquitto/passwd"]


def test_new_zigbee2mqtt_password_reaches_its_configuration(tmp_path):
    """Schluesseltausch: Zugangsdatei geloescht -> neues Passwort in passwd UND in configuration.yaml."""
    _run(tmp_path)
    (tmp_path / "bus" / "credentials" / "zigbee2mqtt" / "bus.json").unlink()
    written = _run(tmp_path, token=lambda n=32: "test-neu")
    assert written == ["bus/credentials/zigbee2mqtt/bus.json", "bus/mosquitto/passwd",
                       "zigbee2mqtt/configuration.yaml (Zugangsdaten)"]
    assert 'password: "test-neu"' in (tmp_path / "zigbee2mqtt" / "configuration.yaml").read_text()


def test_an_existing_configuration_without_retain_is_healed_and_reported(tmp_path):
    """Gateways mit alter Grundkonfiguration bekommen device_options.retain: true; ein zweiter Lauf schweigt."""
    _run(tmp_path)
    config = tmp_path / "zigbee2mqtt" / "configuration.yaml"
    config.write_text(config.read_text().replace("device_options:\n  retain: true\n", ""))
    assert "device_options" not in config.read_text()
    assert _run(tmp_path) == ["zigbee2mqtt/configuration.yaml (retain)"]
    assert "device_options:\n  retain: true\n" in config.read_text()
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert _run(tmp_path) == []


def test_credentials_and_retain_are_both_healed_in_one_run(tmp_path):
    _run(tmp_path)
    config = tmp_path / "zigbee2mqtt" / "configuration.yaml"
    config.write_text(config.read_text().replace("device_options:\n  retain: true\n", ""))
    (tmp_path / "bus" / "credentials" / "zigbee2mqtt" / "bus.json").unlink()
    written = _run(tmp_path, token=lambda n=32: "test-neu")
    assert written[-2:] == ["zigbee2mqtt/configuration.yaml (Zugangsdaten)", "zigbee2mqtt/configuration.yaml (retain)"]
    assert 'password: "test-neu"' in config.read_text() and "retain: true" in config.read_text()


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


def test_binary_corrupt_acl_is_rewritten(tmp_path):
    _run(tmp_path)
    acl = tmp_path / "bus" / "mosquitto" / "acl"
    acl.write_bytes(b"\xff\xfe\x00\x80 kaputt")
    assert _run(tmp_path) == ["bus/mosquitto/acl"]
    assert acl.read_text() == init.render_acl()


def test_the_init_step_never_logs_a_password(tmp_path, monkeypatch, caplog, capsys):
    """Erster Lauf, Heilung (Zugangsdatei fehlt, neues Passwort) und zweiter Lauf: im Log und auf den Ausgaben steht nie
    ein Passwort aus den Zugangsdateien der Dienste, nur Dateinamen (das Passwort in der Zigbee2MQTT-Konfiguration ist
    dasselbe wie in der Zugangsdatei von `zigbee2mqtt`)."""
    for variable, ordner in (("SHG_BUS_DIR", "bus"), ("SHG_ZIGBEE_DIR", "zigbee2mqtt"), ("SHG_DATA_DIR", "data"),
                             ("SHG_HOST_DIR", "host")):
        monkeypatch.setenv(variable, str(tmp_path / ordner))
    caplog.set_level(logging.DEBUG)

    def passwoerter() -> set[str]:
        return {json.loads((tmp_path / "bus" / "credentials" / s / "bus.json").read_text())["password"]
                for s in init.SERVICES}

    init.main()
    erster = passwoerter()
    assert len(erster) == len(init.SERVICES) and all(len(p) >= 32 for p in erster)
    assert "bus/credentials/runtime/bus.json" in caplog.text  # Gegenprobe: der Lauf hat etwas geloggt, nur Dateinamen
    (tmp_path / "bus" / "credentials" / "zigbee2mqtt" / "bus.json").unlink()
    init.main()
    zweiter = passwoerter()
    assert zweiter != erster  # das neue Passwort ist erzeugt und steht in den Dateien
    init.main()
    ausgabe = capsys.readouterr()
    for passwort in erster | zweiter:
        assert passwort not in caplog.text and passwort not in ausgabe.out and passwort not in ausgabe.err
    assert "nichts zu tun" in caplog.text
