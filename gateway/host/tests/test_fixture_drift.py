"""Drift-Test (Plan G2b-2 Task 2, Roadmap-Restpunkt G2b-1): Die Compose-Fixture des Updater-Integrationstests folgt
gateway/compose/docker-compose.yml bis auf die im Kopf der Fixture genannten Abweichungen. Aendert sich die echte
Compose-Datei, scheitert dieser Test, bis die Fixture nachgezogen ist."""
import copy
from pathlib import Path

import yaml

GATEWAY = Path(__file__).resolve().parents[2]
COMPOSE = GATEWAY / "compose" / "docker-compose.yml"
FIXTURE = GATEWAY / "host" / "tests" / "fixtures" / "updater-compose.yml"
HARDENING = ["no-new-privileges:true"]


def as_fixture(real: dict) -> dict:
    """Die echte Compose-Datei, umgebaut nach den Regeln aus dem Kopf der Fixture."""
    data = copy.deepcopy(real)
    del data["services"]["zigbee2mqtt"]  # Test ohne Stick
    data["networks"]["wan"] = {**data["networks"]["wan"], "external": True, "name": "shg-upd-wan"}
    agent = data["services"]["agent"]
    del agent["ports"]
    agent["environment"] = {**agent["environment"], "SHG_DEVICE_API_URL": "http://shg-upd-api:8090"}
    for block in (data["x-hardening"], data["x-gateway"], *data["services"].values()):
        if block.get("security_opt") == HARDENING:
            block["security_opt"] = [*HARDENING, "label:disable"]
        if "build" in block:
            block["build"] = {**block["build"], "context": "../../../.."}
    return data


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def test_updater_fixture_follows_the_device_compose():
    assert as_fixture(_load(COMPOSE)) == _load(FIXTURE)


def test_a_new_service_in_the_device_compose_is_noticed():
    real = _load(COMPOSE)
    real["services"]["neu"] = {"image": "example@sha256:" + "0" * 64}
    assert as_fixture(real) != _load(FIXTURE)
