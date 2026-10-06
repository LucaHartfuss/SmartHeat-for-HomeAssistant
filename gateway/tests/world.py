"""Bausteine fuer Laufzeit- und Szenariotests des Gateways (Muster: heizungsbruecke/tests/golden_scenario.py)."""
import copy
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from configs import SENSOR, THERMOSTAT
from fake_z2m import FakeZigbee2Mqtt
from fakes import FakeBus

from smartheat_gateway import runtime_main
from smartheat_gateway.paths import Paths

BERLIN = ZoneInfo("Europe/Berlin")
START = datetime(2026, 1, 15, 8, 0, tzinfo=BERLIN)


class FrozenWall:
    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = (self._now.astimezone(ZoneInfo("UTC")) + timedelta(seconds=seconds)).astimezone(BERLIN)


class RecordingMqtt:
    def __init__(self, kwargs: dict) -> None:
        self.kwargs = kwargs
        self.connect_failures = 0
        self.connected = True
        self.snapshots: list[dict] = []
        self.telemetry: list[dict] = []
        self.setpoints_callback = None

    def is_connected(self):
        return self.connected

    def subscribe_setpoints(self, on_message):
        self.setpoints_callback = on_message

    def loop_start(self):
        pass

    def stop(self):
        pass

    def publish_snapshot(self, payload):
        self.snapshots.append(copy.deepcopy(payload))

    def publish_telemetry(self, payload):
        self.telemetry.append(copy.deepcopy(payload))


class GatewayWorld:
    def __init__(self, data_dir, monkeypatch, clock) -> None:
        self.paths = Paths(data_dir)
        self.clock = clock
        self.wall = FrozenWall(START)
        self.bus = FakeBus()
        self.z2m = FakeZigbee2Mqtt(self.bus)
        self.z2m.bridge(online=True)
        self.z2m.add_sensor(SENSOR)
        self.z2m.add_thermostat(THERMOSTAT)
        self.z2m.report(SENSOR, temperature=20.0, battery=90)
        self.abo = {"status": "active"}
        self.mqtt: list[RecordingMqtt] = []
        self.restarts = 0
        self.result = None
        self.host = None
        monkeypatch.setenv("SHG_ALIVE_FILE", str(data_dir / "alive"))
        monkeypatch.setattr("smartheat_core.wallclock._now", self.wall.now)
        monkeypatch.setattr("smartheat_runtime.entitlement.query_status", lambda *args, **kwargs: self.abo["status"])
        monkeypatch.setattr("smartheat_runtime.mqtt_link.BridgeMqttClient", self._new_mqtt)
        monkeypatch.setattr("smartheat_runtime.abo.restart_process", self._restart)

    def _new_mqtt(self, *args, **kwargs):
        self.mqtt.append(RecordingMqtt(kwargs))
        return self.mqtt[-1]

    def _restart(self):
        raise SystemExit(0)

    def start_runtime(self):
        self.result, self.host = runtime_main.start(self.paths, self.bus, clock=self.clock, driver_threads=False)
        self.restarts += 1
        return self.result

    def run(self) -> None:
        """Worker abarbeiten; ein gewollter Neustart (SystemExit 0) startet die Laufzeit neu wie Compose."""
        for _ in range(5):
            try:
                self.result.worker.run_pending()
                return
            except SystemExit as stop:
                assert stop.code in (0, None)
                self.start_runtime()
        raise AssertionError("Laufzeit startet staendig neu")

    def advance(self, seconds: float) -> None:
        self.clock.advance(seconds)
        self.wall.advance(seconds)
        if self.host is not None and self.host.driver is not None:
            self.host.driver.poll_once()
        self.run()

    def connect(self) -> None:
        self.mqtt[-1].kwargs["on_connected"](None)
        self.run()

    def answer(self, levers: dict, status: str = "ok", reason=None) -> None:
        payload = {"schema": 4, "seq": self.mqtt[-1].snapshots[-1]["seq"], "ts": "2026-01-15T00:00:00+01:00",
                   "status": status, "reason": reason, "levers": levers, "learned": {}}
        self.mqtt[-1].setpoints_callback(None, None, SimpleNamespace(retain=False, payload=json.dumps(payload)))
        self.run()

    def status(self) -> dict | None:
        return self.bus.retained.get("shg/status")
