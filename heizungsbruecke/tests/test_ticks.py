"""Tick-Zustellung (ticks.py) auf Einheitsebene; der volle Ablauf steht in test_runtime.py."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from heizungsbruecke import ticks
from heizungsbruecke.ha_binding import HaPlantBinding
from heizungsbruecke.manifest import ChannelManifest
from smartheat_core.binding import VAILLANT_MYPYLLANT
from smartheat_core.pipeline import DeviceWriteError, LeverPipeline
from smartheat_core.safety import LocalSafety
from smartheat_runtime import delivery

SETTLE = VAILLANT_MYPYLLANT.settle_seconds


def _safety(heat_limit_max=20.0):
    return LocalSafety(
        ranges={"curve": (0.4, 1.5), "room_setpoint": (15.0, 25.0), "heat_limit": (5.0, heat_limit_max),
                "min_flow": (20.0, 30.0)},
        comfort_boost={"curve": 1.5, "room_setpoint": 25.0, "heat_limit": heat_limit_max},
        emergency_boost_levers=("curve", "room_setpoint", "heat_limit"),
        arrival_threshold_k=0.5,
    )


def test_end_emergency_boost_keeps_running_comfort_boost():
    rt = SimpleNamespace(override=MagicMock(), store=SimpleNamespace(state=SimpleNamespace(boost_active=True)))

    assert ticks._execute(rt, delivery.EndEmergencyBoost()) is None

    rt.override.set_boosts.assert_called_once_with(comfort=True, emergency=False)


def test_end_emergency_boost_without_any_boost_asks_override_to_end_both():
    rt = SimpleNamespace(override=MagicMock(), store=SimpleNamespace(state=SimpleNamespace(boost_active=False)))

    ticks._execute(rt, delivery.EndEmergencyBoost())

    rt.override.set_boosts.assert_called_once_with(comfort=False, emergency=False)


def test_attempt_without_mqtt_client_reports_unsent(monkeypatch):
    monkeypatch.setattr(
        ticks, "read_snapshot", lambda *a, **kw: SimpleNamespace(invalid=(), levers={}, room_target=20.0),
    )
    manifest = ChannelManifest(entity_ids={"shift_current": "number.shift"})
    ha_api = MagicMock()
    rt = SimpleNamespace(
        manifest=manifest, ha_api=ha_api,
        mqtt_client=None, store=SimpleNamespace(state=SimpleNamespace(restore_point={"room_setpoint": 21.0})),
        override=SimpleNamespace(
            settled=lambda lever: True, last_written=lambda lever: None, binding=HaPlantBinding(ha_api, manifest),
        ),
    )

    assert ticks._attempt(rt, "s1", "daily") == delivery.Published(seq="s1", unsent=True)


def test_snapshot_uses_restore_point_when_zone_is_off(monkeypatch):
    captured = {}

    def _read(manifest, ha_api, binding, known):
        captured.update(known)
        return SimpleNamespace(invalid=(), levers={}, room_target=20.0)

    monkeypatch.setattr(ticks, "read_snapshot", _read)
    ha_api = MagicMock()
    ha_api.get_state.return_value = 0.0
    manifest = ChannelManifest(entity_ids={"shift_current": "climate.zone::temperature"})
    rt = SimpleNamespace(
        manifest=manifest, ha_api=ha_api,
        mqtt_client=None, store=SimpleNamespace(state=SimpleNamespace(restore_point={"room_setpoint": 21.0})),
        override=SimpleNamespace(
            settled=lambda lever: True, last_written=lambda lever: None, binding=HaPlantBinding(ha_api, manifest),
        ),
    )
    ticks._attempt(rt, "s1", "daily")
    assert captured == {"room_setpoint": 21.0}


class _StaleZoneHa:
    """mypyllant nach dem Umstellen auf Manuell: HA zeigt bis zum naechsten Poll weiter den Sollwert
    des Zeitprogramms, eigene Schreibvorgaenge erscheinen noch nicht."""

    def __init__(self):
        self.states = {"climate.zone": "auto", "climate.zone::temperature": 18.0}
        self.writes = []

    def get_state(self, entity_id):
        return self.states[entity_id]

    def get_raw_state(self, entity_id):
        return self.states[entity_id]

    def set_hvac_mode(self, entity_id, mode):
        self.writes.append(("hvac", entity_id, mode))

    def set_climate_temperature(self, entity_id, value):
        self.writes.append(("temperature", entity_id, value))


def test_first_start_snapshot_reports_the_written_start_shift_not_the_stale_zone(monkeypatch, make_store, clock):
    # Re-Review: _prime schreibt die Startverschiebung (21.0), der erzwungene Tick geht Sekunden spaeter
    # raus. Der Live-Read zeigt noch 18.0 aus dem Zeitprogramm; der Server-Erstkontakt wuerde 18
    # uebernehmen und mit seiner Antwort die 21 ueberschreiben.
    captured = {}

    def _read(manifest, ha_api, binding, known):
        captured.update(known)
        return SimpleNamespace(invalid=(), levers={}, room_target=20.0)

    monkeypatch.setattr(ticks, "read_snapshot", _read)
    manifest = ChannelManifest(entity_ids={
        "curve_current": "number.curve", "shift_current": "climate.zone::temperature", "min_flow": "number.min_flow",
    })
    ha, store = _StaleZoneHa(), make_store()
    override = LeverPipeline(store, HaPlantBinding(ha, manifest), _safety(), clock=clock)
    override.prepare_start(21.0)
    assert ("temperature", "climate.zone", 21.0) in ha.writes
    rt = SimpleNamespace(manifest=manifest, ha_api=ha, mqtt_client=None, store=store, override=override)

    ticks._attempt(rt, "s1", "first_start")
    assert captured == {"room_setpoint": 21.0}

    # Nach der Beruhigungszeit gilt wieder der Live-Wert (z. B. ein echter Eingriff in der App).
    clock.advance(SETTLE + 1)
    ticks._attempt(rt, "s2", "daily")
    assert captured == {"room_setpoint": 18.0}


def test_fallback_follow_up_keeps_the_retry_chain_alive():
    assert ticks._fallback_follow_up(delivery.Attempt(seq="s1", trigger="daily")) == delivery.Published(seq="s1")
    assert ticks._fallback_follow_up(delivery.EndEmergencyBoost()) is None


class _LaggingPlantHa:
    """mypyllant nach einem Cloud-Schreibvorgang: HA zeigt bis zum naechsten Poll die alten Werte."""

    def __init__(self):
        self.states = {"number.curve": 1.0, "number.shift": 17.5, "number.heat_limit": 16.0}
        self.writes = []

    def get_state(self, entity_id):
        return self.states[entity_id]

    def get_raw_state(self, entity_id):
        return str(self.states[entity_id])

    def set_number_value(self, entity_id, value):
        self.writes.append((entity_id, value))


def test_snapshot_reports_all_own_writes_until_settled_then_the_live_values(monkeypatch, make_store, clock):
    # Audit 3, A3-02: der Tagestick vergleicht curve/heat_limit mit den zuletzt gesendeten Werten
    # ("Anlage folgt nicht"). Kurz nach einem eigenen Schreiben zeigt HA noch die alten Werte; ohne diese Regel
    # zaehlte eine Soll-Absenkung kurz vor dem Tagestick als Abweichung.
    snapshots = []

    def _read(manifest, ha_api, binding, known):
        snapshots.append(dict(known))
        return SimpleNamespace(invalid=(), levers={}, room_target=20.0)

    monkeypatch.setattr(ticks, "read_snapshot", _read)
    manifest = ChannelManifest(entity_ids={
        "curve_current": "number.curve", "shift_current": "number.shift", "heat_limit": "number.heat_limit",
    })
    ha, store = _LaggingPlantHa(), make_store()
    override = LeverPipeline(store, HaPlantBinding(ha, manifest), _safety(heat_limit_max=23.0), clock=clock)
    override.apply_server_values({"curve": 1.2, "room_setpoint": 18.5, "heat_limit": 17.0})
    assert sorted(ha.writes) == [("number.curve", 1.2), ("number.heat_limit", 17.0), ("number.shift", 18.5)]
    rt = SimpleNamespace(manifest=manifest, ha_api=ha, mqtt_client=None, store=store, override=override)

    ticks._attempt(rt, "s1", "daily")
    clock.advance(SETTLE + 1)
    ticks._attempt(rt, "s2", "daily")

    assert snapshots == [
        {"curve": 1.2, "room_setpoint": 18.5, "heat_limit": 17.0},
        {"room_setpoint": 17.5},  # eingeschwungen: Live-Werte (curve/heat_limit liest read_snapshot selbst)
    ]


def test_write_fault_detail_names_the_lever():
    # Ersetzt test_write_fault_detail_keeps_the_role_names_of_the_customer_text (Controller-Ruling Task 10, Spec
    # P2-3): Datenfehler nennen Hebel; der Detailtext ist der von DeviceWriteError ("curve (number.x): Ursache").
    error = DeviceWriteError("curve", "number.curve", RuntimeError("myVAILLANT-Cloud nicht erreichbar"))
    assert str(error) == "curve (number.curve): myVAILLANT-Cloud nicht erreichbar"
    error = DeviceWriteError("room_setpoint", "climate.zone::temperature", RuntimeError("x" * 300))
    assert str(error) == "room_setpoint (climate.zone::temperature): " + "x" * 200
    error = DeviceWriteError("heat_limit", "number.hl", RuntimeError("weg"))
    assert str(error) == "heat_limit (number.hl): weg"
