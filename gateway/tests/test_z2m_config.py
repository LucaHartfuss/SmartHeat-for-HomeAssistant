from smartheat_gateway.agent.z2m_config import ensure


def test_written_once_and_never_again(tmp_path):
    assert ensure(tmp_path, "ember")
    text = (tmp_path / "configuration.yaml").read_text()
    for line in ("base_topic: zigbee2mqtt", "server: mqtt://mosquitto:1883", "port: /dev/zigbee", "adapter: ember",
                 "network_key: GENERATE", "pan_id: GENERATE", "ext_pan_id: GENERATE", "last_seen: ISO_8601",
                 "log_level: warning"):
        assert line in text
    assert "permit_join" not in text
    assert "retain" not in text  # Spiegel stempelt retained Werte als frisch (Task 15, Ruling 4)
    (tmp_path / "configuration.yaml").write_text("geaendert von Zigbee2MQTT")
    assert not ensure(tmp_path, "zstack")
    assert (tmp_path / "configuration.yaml").read_text() == "geaendert von Zigbee2MQTT"
