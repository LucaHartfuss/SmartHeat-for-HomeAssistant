"""Fakes fuer die Bruecken-Tests: FakeHa (einfach), LaggingFakeHa (realistisches HA-/mypyllant-Modell)
und WallClock (Wanduhr). Plain-Import (`from fakes import ...`), tests/ hat kein __init__.py."""
import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch
from zoneinfo import ZoneInfo

import requests
import websocket

from smartheat_core.safety import resolve_local_safety
from smartheat_runtime import entitlement
from smartheat_runtime.runtime_config import RuntimeConfig

# Text fremder Herkunft fuer die Logzeilen-Tests (Fehlertext aus HA, Einheit, Meldeschluessel mit Text des
# Servers): ein Zeilenumbruch mit Logkopf, das Bidi-Steuerzeichen U+202E und die ARABIC LETTER MARK U+061C. In der
# Logzeile steht er nur als Escape (smartheat_core.logtext.lesbar).
FREMDTEXT = "HA\nWARNING  gefaelscht ‮؜"
FREMDTEXT_IM_LOG = "HA\\nWARNING  gefaelscht \\u202e\\u061c"


def logzeilen(caplog) -> list[str]:
    """Die Logzeilen aus caplog (ohne Traceback); keine traegt FREMDTEXT roh (Zeilenumbruch, U+202E, U+061C)."""
    zeilen = [record.getMessage() for record in caplog.records]
    assert [zeile for zeile in zeilen if "\n" in zeile or "‮" in zeile or "؜" in zeile] == []
    return zeilen


def runtime_config(**overrides) -> RuntimeConfig:
    """Gueltige RuntimeConfig fuer Tests, die einen Laufzeit-Kontext von Hand bauen."""
    values = {
        "tenant_id": "test_tenant", "lever_set_id": "vaillant_vrc720",
        "local_safety": resolve_local_safety("vaillant_vrc720", "Heizkoerper"),
        "daily_trigger_time": "12:00", "local_check_interval": 300, "telemetry_interval": 300,
        "notify_hints_off": (), "room_sensor_refs": ("sensor.room_actual",), "battery_refs": (),
        "entitlement_path": Path("/nonexistent/entitlement_state.json"), "abo_source": lambda: entitlement.UNKNOWN,
    }
    values.update(overrides)
    return RuntimeConfig(**values)


# Hebelsatz mit `level` (Viessmann) fuer die Laufzeit-Tests: Optionen des Add-ons und Ausgangszustand der Fake-HA
# (Eingaben; test_runtime.py und test_runtime_hebelsaetze.py)
VIESSMANN_OPTIONEN = {
    "lever_set": "viessmann_vicare", "entity_curve_current": "number.slope", "entity_level_current": "number.shift",
    "entity_shift_current": "number.normal_temperature", "entity_mode_select": "climate.heizkreis",
    "entity_heat_limit": "", "entity_min_flow": "",
}
VIESSMANN_ZUSTAND = {
    "number.slope": 1.0, "number.shift": 0.0, "number.normal_temperature": 20.0, "climate.heizkreis::preset_mode": "eco",
}


class HaEventSink:
    """StatusSink der Laufzeit-Tests (StatusReporter): das Ereignis unveraendert an fire_event. Produktiv sendet
    geraet_status.GeraetStatus (Schema 3); dies ist der Ersatz fuer den entfallenen HaStatusSink."""

    def __init__(self, ha_api) -> None:
        self._ha_api = ha_api

    def publish(self, event: dict) -> None:
        self._ha_api.fire_event("smartheat_status", event)


class FakeNotifySink:
    """NotifySink der Laufzeit-Tests (Grenze zum Kunden): der echte Notifier entscheidet ueber Entprellung,
    Hinweis-Schalter und offene Meldungen; der Sink haelt nur fest, was beim Kunden ankommt."""

    def __init__(self) -> None:
        self.pushes: list[tuple[str, str]] = []  # (Schluessel, Text) je Push
        self.shown: dict[str, str] = {}  # offene kritische Meldungen je Schluessel
        self.withdrawn: list[str] = []

    def push(self, key: str, message: str) -> None:
        self.pushes.append((key, message))

    def show(self, key: str, message: str) -> None:
        self.shown[key] = message

    def withdraw(self, key: str) -> None:
        self.shown.pop(key, None)
        self.withdrawn.append(key)


class FakeMqttChannel:
    """MqttChannel der Laufzeit-Tests (Port, smartheat_runtime.ports): haelt die gesendeten Nachrichten fest."""

    def __init__(self, connected: bool = True) -> None:
        self.connected = connected
        self.connect_failures = 0
        self.snapshots: list[dict] = []
        self.telemetry: list[dict] = []
        self.started = False
        self.stopped = False

    def is_connected(self) -> bool:
        return self.connected

    def publish_snapshot(self, payload: dict) -> None:
        self.snapshots.append(copy.deepcopy(payload))

    def publish_telemetry(self, payload: dict) -> None:
        self.telemetry.append(copy.deepcopy(payload))

    def loop_start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True



class FakeTriggerClient:
    """Fake des Grenz-Adapters zum HA-WebSocket (`heizungsbruecke.geraet_trigger.HaTriggerClient`):
    verbindet nichts, haelt die Konstruktor-Argumente in `kwargs` fest (die Tests rufen daraus
    `on_trigger_event`/`on_connected`). Konstruktor, `start` und `stop` haben die Signatur des echten Clients
    (test_fake_gleichlauf.py). Einsetzen per `monkeypatch.setattr(..., trigger_klasse(ablage))`: jede Umgebung bekommt
    eine eigene Unterklasse, deren Instanzen in `ablage` landen."""

    erzeugt: list  # gesetzt von trigger_klasse

    def __init__(self, ws_url, token, triggers, on_trigger_event, on_connected=None):
        self.kwargs = {"ws_url": ws_url, "token": token, "triggers": triggers,
                       "on_trigger_event": on_trigger_event, "on_connected": on_connected}
        self.connected = True
        self.started = False
        self.stopped = False
        type(self).erzeugt.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


def trigger_klasse(ablage: list, basis: type[FakeTriggerClient] = FakeTriggerClient, **attribute) -> type:
    """Unterklasse von `basis` fuer eine Umgebung: erzeugte Clients landen in `ablage`; `attribute` setzt weitere
    Klassenattribute (z. B. ein Log)."""
    return type(basis.__name__, (basis,), {"erzeugt": ablage, **attribute})

def datei_stand(path: Path) -> tuple[int, int, bytes] | None:
    """Stand einer Datei auf dem Datentraeger (Inode, Aenderungszeit in ns, Inhalt), None ohne Datei. Jedes Schreiben
    geht atomar ueber eine neue Datei (dateien.schreiben, os.replace) und aendert die Inode; der Inhalt ist mit dabei,
    damit eine wiederverwendete Inode bei gleicher Aenderungszeit kein "ungeschrieben" vortaeuscht. Gleicher Stand
    heisst: die Datei wurde nicht geschrieben (Wirkung auf dem Datentraeger statt eines Spions auf dem Store)."""
    try:
        stat = path.stat()
        inhalt = path.read_bytes()
    except FileNotFoundError:
        return None
    return stat.st_ino, stat.st_mtime_ns, inhalt


#: Registry der Schreibziele, die die Tests binden (Schreibziele nur von der Plattform des Hebelsatzes,
#: alle aus einem Config-Entry): je Plattform (dokument.SCHREIB_PLATTFORM) die Entities der Test-Optionen (Vaillant
#: runtime_helpers.OPTIONS und climate.zone, Weishaupt WEISHAUPT, Viessmann VIESSMANN_OPTIONEN). KONFIG_EINTRAG ist das
#: Config-Entry je Plattform, das auch der Kreis der Test-Dokumente nennt (geraet_helpers.konfiguration_aus_optionen).
SCHREIBZIELE = {
    "mypyllant": ("number.curve_current", "number.shift_current", "number.heat_limit", "number.min_flow",
                  "climate.zone"),
    "weishaupt_modbus": ("number.hk", "number.normal", "number.swu", "select.betriebsart", "number.komfort",
                         "number.absenk"),
    "vicare": ("number.slope", "number.shift", "number.normal_temperature", "climate.heizkreis"),
}
KONFIG_EINTRAG = {"mypyllant": "e1", "weishaupt_modbus": "e-weishaupt", "vicare": "e-vicare"}


def schreibziel_eintrag(entity_id: str, plattform: str) -> dict:
    """Registry-Eintrag eines Schreibziels wie im echten HA (Plattform, Config-Entry, Geraet des Waermeerzeugers)."""
    return {"entity_id": entity_id, "platform": plattform, "config_entry_id": KONFIG_EINTRAG[plattform],
            "device_id": f"d-{plattform}"}


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
        # echtes HA: die Registry liefert keine device_class, sie steht im Zustand; die lokale Positivliste der Rollen
        # liest sie dort
        self.states.update({f"sensor.{name}::device_class": "temperature"
                            for name in ("room_actual", "room_target", "outdoor_temp")})
        self.writes = []
        self.pushes = []
        self.persistent = []
        self.dismissed = []
        self.events = []
        self.write_error = None
        # Zuordnungspruefung: Entity-/Geraete-Registry, Dienste, in HA unbekannte Entities, HA nicht
        # erreichbar. Die Schreibziele der Tests stehen in der Registry (SCHREIBZIELE); ohne Eintrag waere ein
        # Schreibziel beim Start nicht pruefbar. Ein Eintrag ohne Zustand zaehlt fuer die Positivliste nicht
        # (einstellbare_plattformen).
        self.registry: list[dict] = [schreibziel_eintrag(entity_id, plattform)
                                     for plattform, entity_ids in SCHREIBZIELE.items() for entity_id in entity_ids]
        self.devices: list[dict] = []
        self.services: list[dict] = []
        self.missing: set[str] = set()
        self.offline = False
        self.config = {"time_zone": "Europe/Berlin", "state": "RUNNING", "unit_system": {"temperature": "°C"}}

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
        return self.config

    def websocket_url(self):
        return "ws://x/api/websocket"

    def fire_event(self, event_type, data):
        self.events.append((event_type, copy.deepcopy(data)))

    def is_reachable(self):
        return not self.offline

    def entity_exists(self, entity_id):
        if self.offline:
            raise OSError("HA nicht erreichbar")
        return entity_id not in self.missing

    def _online(self):
        if self.offline:
            raise OSError("HA nicht erreichbar")

    def entity_registry(self):
        self._online()
        return [dict(entry) for entry in self.registry]

    def device_registry(self):
        self._online()
        return [dict(device) for device in self.devices]

    def get_states(self):
        self._online()
        # wie das echte HA: jede Entity mit Wert oder Attributen hat einen Zustand; eine nicht lesbare (Exception als
        # Wert) steht als "unavailable" da, eine nur ueber Attribute bekannte als "unknown" (die Positivliste
        # der Rollen verlangt einen Zustand je gebundener Entity)
        result = []
        for entity_id in dict.fromkeys(key.partition("::")[0] for key in self.states):
            value = self.states.get(entity_id, "unknown")
            attributes = {k.partition("::")[2]: v for k, v in self.states.items()
                          if k.startswith(f"{entity_id}::") and not isinstance(v, Exception)}
            result.append({"entity_id": entity_id, "state": "unavailable" if isinstance(value, Exception) else str(value),
                           "attributes": attributes, "last_updated": "2026-10-11T10:00:00+00:00"})
        return result

    def get_services(self):
        self._online()
        return [dict(service) for service in self.services]

    def get_raw_state(self, entity_id):
        value = self.states[entity_id]
        if isinstance(value, Exception):
            raise value
        if value in ("unavailable", "unknown", ""):
            raise ValueError(value)
        return str(value)


class FailingServiceHa(FakeHa):
    """Der Notify-Dienst `notify.kaputt` wirft, alle anderen gehen durch (Review-Fokus 5)."""

    def send_notification(self, service, message):
        if service == "notify.kaputt":
            raise RuntimeError("Dienst weg")
        super().send_notification(service, message)


class LaggingFakeHa(FakeHa):
    """HA/mypyllant-Modell: ein geschriebener Wert ist erst nach `lag_seconds` lesbar
    (mypyllant pollt die Hersteller-Cloud bis ca. 30 min spaeter), ein Moduswechsel wirkt nach
    `mode_lag_seconds`; ein Service-Aufruf an eine nicht verfuegbare Entity ist ein wirkungsloses 200
    (HA ueberspringt sie still); waehrend einer Quota-Sperre scheitert jeder Aufruf mit 403
    und zaehlt in `quota_attempts`. `set_hvac_mode` respektiert `write_error` wie die
    Schreibaufrufe. `clock` ist die monotone Test-Uhr (FakeClock)."""

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


class FakeClock:
    """Monotone Test-Uhr fuer RegulationWorker: steht still, bis ein Test sie vorstellt."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


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


def ha_antwort(body=None, *, fehler=None):
    """HTTP-Antwort an der requests-Grenze der HA-API (Attrappe mit spec der echten Klasse)."""
    response = Mock(spec=requests.Response)
    response.status_code = 200
    response.json.return_value = body
    if fehler is None:
        response.raise_for_status.return_value = None
    else:
        response.raise_for_status.side_effect = fehler
    return response


class HaWebSocket:
    """Gegenstelle am websocket-Rand (`websocket.create_connection`): je Verbindung `auth_required`, nach dem Token
    `auth_ok` (ein anderes Token: `auth_invalid`), je Kommando die naechste Antwort aus `ergebnisse`. Zeichnet die
    Adressen der Verbindungen und die gesendeten Kommandos auf."""

    def __init__(self, *ergebnisse, token="test-token"):
        self._ergebnisse = list(ergebnisse)
        self._token = token
        self.urls: list[str] = []
        self.kommandos: list[dict] = []

    def _verbinden(self, url, timeout=None, **_optionen):
        self.urls.append(url)
        ws = MagicMock(spec=websocket.WebSocket)
        ausgang = [{"type": "auth_required"}]

        def senden(text):
            nachricht = json.loads(text)
            if nachricht.get("type") == "auth":
                angemeldet = nachricht.get("access_token") == self._token
                ausgang.append({"type": "auth_ok" if angemeldet else "auth_invalid"})
                return
            self.kommandos.append(nachricht)
            ausgang.append({"id": nachricht["id"], "type": "result", "success": True,
                            "result": self._ergebnisse.pop(0)})

        ws.send.side_effect = senden
        ws.recv.side_effect = lambda: json.dumps(ausgang.pop(0))
        return ws

    def angeschlossen(self):
        return patch("heizungsbruecke.ha_api.websocket.create_connection", autospec=True, side_effect=self._verbinden)


class HttpAntwort:
    """HTTP-Antwort an der requests-Grenze (Supervisor-HTTP): json, status_code und raise_for_status wie
    requests.Response; ein Status ab 400 wirft requests.HTTPError."""

    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


def nachricht_bytes(nachricht) -> int:
    """Bytes einer Nachricht so, wie der Link sie sendet (kompakt, UTF-8; inventory.groesse)."""
    return len(json.dumps(nachricht, separators=(",", ":"), ensure_ascii=False).encode())
