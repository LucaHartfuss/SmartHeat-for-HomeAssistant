"""Fakes fuer die Bruecken-Tests: FakeHa (einfach), LaggingFakeHa (realistisches HA-/mypyllant-Modell)
und WallClock (Wanduhr). Plain-Import (`from fakes import ...`), tests/ hat kein __init__.py."""
import copy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from smartheat_core.safety import resolve_local_safety
from smartheat_runtime.runtime_config import RuntimeConfig
from smartheat_transport.descriptor import credential_for, parse_descriptor

# Zugangsoptionen seit AWS-2: Deskriptor (Mosquitto ueber cloudflared) und Installations-Token. Gueltige Test-Optionen
# mit mqtt_username/mqtt_password tragen beides, sonst meldet der Start "Konfiguration veraltet".
MOSQUITTO_TRANSPORT = '{"kind": "mosquitto_cloudflared", "host": "127.0.0.1", "port": 18830}'
INSTALLATION_TOKEN = "tok"
ACCESS_OPTIONS = {"transport": MOSQUITTO_TRANSPORT, "installation_token": INSTALLATION_TOKEN}


def runtime_config(**overrides) -> RuntimeConfig:
    """Gueltige RuntimeConfig fuer Tests, die einen Laufzeit-Kontext von Hand bauen (Plan SHG G1)."""
    descriptor = parse_descriptor(MOSQUITTO_TRANSPORT)
    values = {
        "tenant_id": "test_tenant", "setup_id": None, "lever_set_id": "vaillant_vrc720",
        "local_safety": resolve_local_safety("vaillant_vrc720", "Heizkoerper"),
        "descriptor": descriptor, "credential": credential_for(descriptor, username="u", password="p"),
        "installation_token": INSTALLATION_TOKEN, "accounts_api_base_url": "https://accounts.example.test",
        "daily_trigger_time": "12:00", "local_check_interval": 300, "telemetry_interval": 300,
        "notify_hints_off": (), "room_sensor_refs": ("sensor.room_actual",), "battery_refs": (),
        "entitlement_path": Path("/nonexistent/entitlement_state.json"),
    }
    values.update(overrides)
    return RuntimeConfig(**values)


class FakeHa:
    token = "tok"

    def __init__(self):
        self.states = {
            "sensor.room_actual": 20.0, "sensor.room_target": 21.0,
            "number.curve_current": 0.9, "number.shift_current": 22.0,
            "sensor.outdoor_temp": 5.0, "number.heat_limit": 16.0,
            # Raum-Soll 21.0 -> kein Mindestvorlauf-Schreiben beim Start
            "number.min_flow": 21.0,
        }
        self.writes = []
        self.pushes = []
        self.persistent = []
        self.dismissed = []
        self.events = []
        self.write_error = None

    def get_state(self, entity_id):
        value = self.states[entity_id]
        if isinstance(value, Exception):
            raise value
        return value

    def set_number_value(self, entity_id, value):
        if self.write_error is not None:
            raise self.write_error
        self.states[entity_id] = value
        self.writes.append((entity_id, value))

    def set_climate_temperature(self, entity_id, value):
        self.set_number_value(f"{entity_id}::temperature", value)

    def set_hvac_mode(self, entity_id, mode):
        self.states[entity_id] = mode
        self.writes.append((entity_id, mode))

    def select_option(self, entity_id, option):
        if self.write_error is not None:
            raise self.write_error
        self.states[entity_id] = option
        self.writes.append((entity_id, option))

    def set_preset_mode(self, entity_id, preset):
        if self.write_error is not None:
            raise self.write_error
        self.states[f"{entity_id}::preset_mode"] = preset
        self.writes.append((f"{entity_id}::preset_mode", preset))

    def get_attribute(self, entity_id, attribute):
        value = self.states[f"{entity_id}::{attribute}"]
        if isinstance(value, Exception):
            raise value
        return str(value)

    def send_notification(self, service, message):
        self.pushes.append(message)

    def create_persistent_notification(self, title, message, notification_id):
        self.persistent.append((notification_id, message))

    def dismiss_persistent_notification(self, notification_id):
        self.dismissed.append(notification_id)

    def get_config(self):
        return {"time_zone": "Europe/Berlin", "state": "RUNNING"}

    def websocket_url(self):
        return "ws://x/api/websocket"

    def fire_event(self, event_type, data):
        self.events.append((event_type, copy.deepcopy(data)))

    def is_reachable(self):
        return True

    def entity_exists(self, entity_id):
        return True

    def get_raw_state(self, entity_id):
        value = self.states[entity_id]
        if isinstance(value, Exception):
            raise value
        if value in ("unavailable", "unknown", ""):
            raise ValueError(value)
        return str(value)


class FailingServiceHa(FakeHa):
    """Der Notify-Dienst `notify.kaputt` wirft, alle anderen gehen durch (Plan SHG G1, Review-Fokus 5)."""

    def send_notification(self, service, message):
        if service == "notify.kaputt":
            raise RuntimeError("Dienst weg")
        super().send_notification(service, message)


class LaggingFakeHa(FakeHa):
    """HA/mypyllant-Modell (TP12e, AU-023): ein geschriebener Wert ist erst nach `lag_seconds` lesbar
    (mypyllant pollt die Hersteller-Cloud bis ca. 30 min spaeter), ein Moduswechsel wirkt nach
    `mode_lag_seconds`; ein Service-Aufruf an eine nicht verfuegbare Entity ist ein wirkungsloses 200
    (HA ueberspringt sie still, AU-016); waehrend einer Quota-Sperre scheitert jeder Aufruf mit 403
    und zaehlt in `quota_attempts` (AU-015). `set_hvac_mode` respektiert `write_error` wie die
    Schreibaufrufe. `clock` ist die monotone Test-Uhr (conftest.FakeClock)."""

    def __init__(self, clock, *, lag_seconds: float = 1800.0, mode_lag_seconds: float = 600.0) -> None:
        super().__init__()
        self._clock = clock
        self.lag_seconds = lag_seconds
        self.mode_lag_seconds = mode_lag_seconds
        self._pending: list[tuple[float, str, object]] = []
        self.quota_until: float | None = None
        self.quota_attempts = 0
        self.service_calls: list[tuple[str, object]] = []

    def _settle(self) -> None:
        now = self._clock()
        for _, entity_id, value in sorted((p for p in self._pending if p[0] <= now), key=lambda p: p[0]):
            self.states[entity_id] = value
        self._pending = [p for p in self._pending if p[0] > now]

    def get_state(self, entity_id):
        self._settle()
        return super().get_state(entity_id)

    def get_raw_state(self, entity_id):
        self._settle()
        return super().get_raw_state(entity_id)

    def _service(self, key: str, value, lag: float) -> None:
        self.service_calls.append((key, value))
        if self.write_error is not None:
            raise self.write_error
        if self.quota_until is not None and self._clock() < self.quota_until:
            self.quota_attempts += 1
            raise RuntimeError("403 Quota Exceeded")
        if self.states.get(key.partition("::")[0]) in ("unavailable", "unknown"):
            return  # HTTP 200 ohne Wirkung
        self.writes.append((key, value))
        self._pending.append((self._clock() + lag, key, value))

    def set_number_value(self, entity_id, value):
        self._service(entity_id, value, self.lag_seconds)

    def set_climate_temperature(self, entity_id, value):
        self._service(f"{entity_id}::temperature", value, self.lag_seconds)

    def set_hvac_mode(self, entity_id, mode):
        self._service(entity_id, mode, self.mode_lag_seconds)

    def select_option(self, entity_id, option):
        self._service(entity_id, option, self.mode_lag_seconds)

    def set_preset_mode(self, entity_id, preset):
        self._service(f"{entity_id}::preset_mode", preset, self.mode_lag_seconds)

    def get_attribute(self, entity_id, attribute):
        self._settle()
        return super().get_attribute(entity_id, attribute)


class WallClock:
    """Steuerbare Wanduhr in Europe/Berlin fuer Szenarien um 12:00, Mitternacht und die Zeitumstellung.
    `now()` ist die Wanduhr der Bruecke, per `monkeypatch.setattr("smartheat_core.wallclock._now", wall.now)`;
    sie liefert immer eine Berlin-Zeit (aware), damit der Test nicht an der Zeitzone der Test-Maschine haengt."""

    ZONE = ZoneInfo("Europe/Berlin")

    def __init__(self, start: datetime) -> None:
        self._utc = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._utc.astimezone(self.ZONE)

    def set(self, value: datetime) -> None:
        self._utc = value.astimezone(UTC)

    def advance(self, seconds: float) -> None:
        self._utc += timedelta(seconds=seconds)  # UTC-Arithmetik: ueber die Zeitumstellung hinweg absolut
