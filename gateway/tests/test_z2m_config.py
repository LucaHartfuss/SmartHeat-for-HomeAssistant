import stat

from smartheat_gateway.z2m_config import ensure, sync_credentials


def test_written_once_and_never_again(tmp_path):
    assert ensure(tmp_path, "ember", "zigbee2mqtt", "test-pw")
    text = (tmp_path / "configuration.yaml").read_text()
    for line in ("base_topic: zigbee2mqtt", "server: mqtt://mosquitto:1883", "port: /dev/zigbee", "adapter: ember",
                 "user: zigbee2mqtt", 'password: "test-pw"',
                 "network_key: GENERATE", "pan_id: GENERATE", "ext_pan_id: GENERATE", "last_seen: ISO_8601",
                 "log_level: warning"):
        assert line in text
    assert "permit_join" not in text
    assert "retain" not in text  # Spiegel stempelt retained Werte als frisch (Task 15, Ruling 4)
    (tmp_path / "configuration.yaml").write_text("geaendert von Zigbee2MQTT")
    assert not ensure(tmp_path, "zstack", "zigbee2mqtt", "test-pw")
    assert (tmp_path / "configuration.yaml").read_text() == "geaendert von Zigbee2MQTT"


def test_sync_rewrites_only_the_credentials(tmp_path):
    ensure(tmp_path, "ember", "zigbee2mqtt", "test-alt")
    path = tmp_path / "configuration.yaml"
    # So schreibt Zigbee2MQTT die Datei nach dem Ersetzen von GENERATE: ohne Anfuehrungszeichen, Schluessel als Liste.
    text = path.read_text().replace('"test-alt"', "test-alt").replace(
        "network_key: GENERATE", "network_key:\n    - 1\n    - 2")
    path.write_text(text)
    assert sync_credentials(tmp_path, "zigbee2mqtt", "test-neu")
    new = path.read_text()
    assert '  password: "test-neu"' in new and "test-alt" not in new
    assert new.replace('"test-neu"', "test-alt") == text  # sonst unveraendert
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not sync_credentials(tmp_path, "zigbee2mqtt", "test-neu")  # zweiter Lauf: nichts zu tun


def test_sync_inserts_missing_lines_and_leaves_unknown_files_alone(tmp_path):
    path = tmp_path / "configuration.yaml"
    path.write_text("mqtt:\n  server: mqtt://mosquitto:1883\nserial:\n  port: /dev/zigbee\n")
    assert sync_credentials(tmp_path, "zigbee2mqtt", "test-pw")
    assert path.read_text() == ('mqtt:\n  user: zigbee2mqtt\n  password: "test-pw"\n  server: mqtt://mosquitto:1883\n'
                                "serial:\n  port: /dev/zigbee\n")
    path.write_text("ganz anderes Format\n")
    assert not sync_credentials(tmp_path, "zigbee2mqtt", "test-pw")
    assert path.read_text() == "ganz anderes Format\n"
    assert not sync_credentials(tmp_path / "fehlt", "zigbee2mqtt", "test-pw")


def test_sync_ignores_nested_keys_of_the_mqtt_block(tmp_path):
    path = tmp_path / "configuration.yaml"
    path.write_text('mqtt:\n  user: zigbee2mqtt\n  password: "test-pw"\n  extra:\n    password: anderes\n')
    assert not sync_credentials(tmp_path, "zigbee2mqtt", "test-pw")
