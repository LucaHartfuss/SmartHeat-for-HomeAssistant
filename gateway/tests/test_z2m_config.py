import logging
import stat

import pytest
import yaml

from smartheat_gateway.z2m_config import ensure, render, sync_credentials, sync_device_options


def test_written_once_and_never_again(tmp_path):
    assert ensure(tmp_path, "ember", "zigbee2mqtt", "test-pw")
    text = (tmp_path / "configuration.yaml").read_text()
    for line in ("base_topic: zigbee2mqtt", "server: mqtt://mosquitto:1883", "port: /dev/zigbee", "adapter: ember",
                 "user: zigbee2mqtt", 'password: "test-pw"',
                 "network_key: GENERATE", "pan_id: GENERATE", "ext_pan_id: GENERATE", "last_seen: ISO_8601",
                 "log_level: warning"):
        assert line in text
    assert "permit_join" not in text
    # Zigbee2MQTT sendet Geraetewerte retained; der Spiegel wertet sie ueber last_seen (L4-17)
    assert "\ndevice_options:\n  retain: true\nadvanced:\n" in text
    assert yaml.safe_load(text)["device_options"] == {"retain": True}
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


def test_sync_leaves_undecodable_or_unreadable_files_alone(tmp_path):
    path = tmp_path / "configuration.yaml"
    path.write_bytes(b"mqtt:\n  user: \xff\xfe\n")
    assert not sync_credentials(tmp_path, "zigbee2mqtt", "test-pw")
    assert path.read_bytes() == b"mqtt:\n  user: \xff\xfe\n"
    path.unlink()
    path.mkdir()  # Verzeichnis statt Datei
    assert not sync_credentials(tmp_path, "zigbee2mqtt", "test-pw")


def test_sync_comment_at_column_0_does_not_end_the_mqtt_block(tmp_path):
    path = tmp_path / "configuration.yaml"
    path.write_text('mqtt:\n  user: zigbee2mqtt\n# Kommentar\n  password: "test-alt"\nserial:\n  port: x\n')
    assert sync_credentials(tmp_path, "zigbee2mqtt", "test-neu")
    assert path.read_text() == ('mqtt:\n  user: zigbee2mqtt\n# Kommentar\n  password: "test-neu"\nserial:\n  port: x\n')


def test_sync_comment_first_line_does_not_set_the_indent(tmp_path):
    path = tmp_path / "configuration.yaml"
    path.write_text("mqtt:\n      # tief eingerueckter Kommentar\n  server: mqtt://mosquitto:1883\n")
    assert sync_credentials(tmp_path, "zigbee2mqtt", "test-pw")
    assert path.read_text() == ('mqtt:\n  user: zigbee2mqtt\n  password: "test-pw"\n'
                                "      # tief eingerueckter Kommentar\n  server: mqtt://mosquitto:1883\n")


def test_the_template_switches_the_frontend_and_the_home_assistant_link_off(tmp_path):
    """Ausgabe der Vorlage, als YAML gelesen (nicht der Dateitext): kein Frontend von Zigbee2MQTT im Netz, keine
    Anbindung an Home Assistant; das gilt auch fuer die Datei, die `ensure` schreibt."""
    gerendert = yaml.safe_load(render("ember", "zigbee2mqtt", "test-pw"))
    assert gerendert["frontend"] == {"enabled": False}
    assert gerendert["homeassistant"] == {"enabled": False}
    assert ensure(tmp_path, "ember", "zigbee2mqtt", "test-pw")
    geschrieben = yaml.safe_load((tmp_path / "configuration.yaml").read_text())
    assert geschrieben["frontend"] == {"enabled": False}
    assert geschrieben["homeassistant"] == {"enabled": False}
    assert geschrieben["mqtt"]["server"] == "mqtt://mosquitto:1883"  # Gegenprobe: die Datei ist sonst wie gelesen


ALT = ('# von Zigbee2MQTT\nversion: 4\nmqtt:\n  user: zigbee2mqtt\n  password: x\n'
       'advanced:\n  # Netz\n  network_key:\n    - 1\n    - 2\n  last_seen: ISO_8601\n')


def _geheilt(tmp_path, text):
    path = tmp_path / "configuration.yaml"
    path.write_text(text)
    path.chmod(0o644)
    return sync_device_options(tmp_path), path


def test_device_options_block_is_appended_when_missing(tmp_path):
    geschrieben, path = _geheilt(tmp_path, ALT)
    assert geschrieben
    assert path.read_text() == ALT + "device_options:\n  retain: true\n"
    assert yaml.safe_load(path.read_text())["device_options"] == {"retain": True}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not sync_device_options(tmp_path)  # idempotent


def test_device_options_block_is_appended_after_a_last_line_without_newline(tmp_path):
    geschrieben, path = _geheilt(tmp_path, "version: 4\nadvanced:\n  log_level: warning")
    assert geschrieben
    assert path.read_text() == "version: 4\nadvanced:\n  log_level: warning\ndevice_options:\n  retain: true\n"


def test_retain_is_added_to_an_existing_block_without_it(tmp_path):
    text = "version: 4\ndevice_options:\n  # Kommentar\n  legacy: true\nadvanced:\n  last_seen: ISO_8601\n"
    geschrieben, path = _geheilt(tmp_path, text)
    assert geschrieben
    assert path.read_text() == ("version: 4\ndevice_options:\n  retain: true\n  # Kommentar\n  legacy: true\n"
                                "advanced:\n  last_seen: ISO_8601\n")
    assert yaml.safe_load(path.read_text())["device_options"] == {"retain": True, "legacy": True}
    assert not sync_device_options(tmp_path)


def test_retain_is_added_to_an_empty_block_at_the_end_of_the_file(tmp_path):
    geschrieben, path = _geheilt(tmp_path, "version: 4\ndevice_options:\n")
    assert geschrieben
    assert path.read_text() == "version: 4\ndevice_options:\n  retain: true\n"


@pytest.mark.parametrize("alt", ["false", "False", '"false"'])
def test_retain_false_is_set_to_true_and_the_rest_stays(tmp_path, alt):
    text = ALT + f"device_options:\n  # bleibt\n  retain: {alt} # eigener Kommentar\n  anderes: 1\nfrontend:\n  enabled: false\n"
    geschrieben, path = _geheilt(tmp_path, text)
    assert geschrieben
    assert path.read_text() == text.replace(f"retain: {alt} #", "retain: true #")
    assert not sync_device_options(tmp_path)


@pytest.mark.parametrize("text", [
    "version: 4\ndevice_options:\n  retain: true\nadvanced:\n  log_level: warning\n",
    "version: 4\ndevice_options:\n  retain: 'true' # an\n",
])
def test_retain_true_already_there_changes_nothing(tmp_path, text):
    geschrieben, path = _geheilt(tmp_path, text)
    assert not geschrieben and path.read_text() == text
    assert stat.S_IMODE(path.stat().st_mode) == 0o644  # nicht angefasst


def test_retain_of_another_block_or_nesting_does_not_count(tmp_path):
    text = "version: 4\nmqtt:\n  retain: true\ndevice_options:\n  tief:\n    retain: true\n"
    geschrieben, path = _geheilt(tmp_path, text)
    assert geschrieben
    assert path.read_text() == ("version: 4\nmqtt:\n  retain: true\ndevice_options:\n  retain: true\n"
                                "  tief:\n    retain: true\n")


@pytest.mark.parametrize("kopf", ["device_options: {}", "device_options: {retain: false}", "device_options: # Kommentar"])
def test_a_device_options_form_that_cannot_be_healed_logs_a_warning_without_the_file_content(tmp_path, caplog, kopf):
    """Review Task 1, Minor 1: kein stilles Nicht-Heilen; die Zeile nennt keinen Dateiinhalt."""
    text = f"version: 4\n{kopf}\n  network_key: GEHEIM-MARKE-4711\n"
    with caplog.at_level(logging.WARNING, logger="smartheat_gateway.z2m_config"):
        geschrieben, path = _geheilt(tmp_path, text)
    assert not geschrieben and path.read_text() == text
    (zeile,) = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "nicht heilbar" in zeile and "von Hand" in zeile
    assert "GEHEIM-MARKE-4711" in path.read_text()  # die Marke steht in der Datei, ...
    assert all("GEHEIM-MARKE-4711" not in f"{r.getMessage()} {r.args}" for r in caplog.records)  # ... in keiner Logzeile


@pytest.mark.parametrize("text", [
    ALT,  # fehlender Block: heilbar
    "version: 4\ndevice_options:\n  legacy: true\n",  # Block ohne retain: heilbar
    "version: 4\ndevice_options:\n  retain: true\n",  # schon in Ordnung
    "version: 4\ndevice_options: {retain: true}\n",  # Flow-Form, retain erkennbar schon true
])
def test_no_warning_when_the_form_is_healable_or_already_fine(tmp_path, caplog, text):
    with caplog.at_level(logging.WARNING, logger="smartheat_gateway.z2m_config"):
        _geheilt(tmp_path, text)
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_device_options_in_flow_style_and_unreadable_files_stay_alone(tmp_path):
    geschrieben, path = _geheilt(tmp_path, "version: 4\ndevice_options: {retain: false}\n")
    assert not geschrieben and path.read_text() == "version: 4\ndevice_options: {retain: false}\n"
    path.write_bytes(b"version: \xff\xfe\n")
    assert not sync_device_options(tmp_path)
    assert path.read_bytes() == b"version: \xff\xfe\n"
    assert not sync_device_options(tmp_path / "fehlt")
    path.unlink()
    path.mkdir()  # Verzeichnis statt Datei
    assert not sync_device_options(tmp_path)
