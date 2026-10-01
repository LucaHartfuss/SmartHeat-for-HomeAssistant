"""Tick-Zustellung (ticks.py) auf Einheitsebene; der volle Ablauf steht in test_runtime.py."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from heizungsbruecke import delivery, ticks
from heizungsbruecke.manifest import ChannelManifest
from heizungsbruecke.override import OWN_WRITE_SETTLE_SECONDS, Override


def test_end_emergency_boost_keeps_running_comfort_boost():
    rt = SimpleNamespace(override=MagicMock(), store=SimpleNamespace(state=SimpleNamespace(boost_active=True)))

    assert ticks._execute(rt, delivery.EndEmergencyBoost()) is None

    rt.override.set_boosts.assert_called_once_with(comfort=True, emergency=False)


def test_end_emergency_boost_without_any_boost_asks_override_to_end_both():
    rt = SimpleNamespace(override=MagicMock(), store=SimpleNamespace(state=SimpleNamespace(boost_active=False)))

    ticks._execute(rt, delivery.EndEmergencyBoost())

    rt.override.set_boosts.assert_called_once_with(comfort=False, emergency=False)


def test_attempt_without_mqtt_client_reports_unsent(monkeypatch):
    monkeypatch.setattr(ticks, "read_snapshot_roles", lambda *a, **kw: SimpleNamespace(invalid_roles=(), roles={}))
    rt = SimpleNamespace(
        manifest=SimpleNamespace(entity_ids={"shift_current": "number.shift"}), ha_api=MagicMock(),
        mqtt_client=None, store=SimpleNamespace(state=SimpleNamespace(shift_current=21.0)),
        override=SimpleNamespace(settled=lambda role: True, last_written=lambda role: None),
    )

    assert ticks._attempt(rt, "s1", "daily") == delivery.Published(seq="s1", unsent=True)


def test_snapshot_uses_restore_point_when_zone_is_off(monkeypatch):
    captured = {}

    def _read(manifest, ha_api, computed_values):
        captured.update(computed_values)
        return SimpleNamespace(invalid_roles=(), roles={})

    monkeypatch.setattr(ticks, "read_snapshot_roles", _read)
    ha_api = MagicMock()
    ha_api.get_state.return_value = 0.0
    rt = SimpleNamespace(
        manifest=SimpleNamespace(entity_ids={"shift_current": "climate.zone::temperature"}), ha_api=ha_api,
        mqtt_client=None, store=SimpleNamespace(state=SimpleNamespace(shift_current=21.0)),
        override=SimpleNamespace(settled=lambda role: True, last_written=lambda role: None),
    )
    ticks._attempt(rt, "s1", "daily")
    assert captured == {"shift_current": 21.0}


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

    def _read(manifest, ha_api, computed_values):
        captured.update(computed_values)
        return SimpleNamespace(invalid_roles=(), roles={})

    monkeypatch.setattr(ticks, "read_snapshot_roles", _read)
    manifest = ChannelManifest(entity_ids={
        "curve_current": "number.curve", "shift_current": "climate.zone::temperature", "min_flow": "number.min_flow",
    })
    options = {
        "curve_min": 0.4, "curve_max": 1.5, "shift_min": 15.0, "shift_max": 25.0,
        "min_flow_min": 20.0, "min_flow_max": 30.0, "boost_curve_value": 1.5, "boost_shift_value": 25.0,
    }
    ha, store = _StaleZoneHa(), make_store()
    override = Override(store, manifest, ha, options, clock=clock)
    override.prepare_zone(start_shift=21.0)
    assert ("temperature", "climate.zone", 21.0) in ha.writes
    rt = SimpleNamespace(manifest=manifest, ha_api=ha, mqtt_client=None, store=store, override=override)

    ticks._attempt(rt, "s1", "first_start")
    assert captured == {"shift_current": 21.0}

    # Nach der Beruhigungszeit gilt wieder der Live-Wert (z. B. ein echter Eingriff in der App).
    clock.advance(OWN_WRITE_SETTLE_SECONDS + 1)
    ticks._attempt(rt, "s2", "daily")
    assert captured == {"shift_current": 18.0}


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
    # Audit 3, A3-02: der Tagestick vergleicht curve_current/heat_limit mit den zuletzt gesendeten Werten
    # ("Anlage folgt nicht"). Kurz nach einem eigenen Schreiben zeigt HA noch die alten Werte; ohne diese Regel
    # zaehlte eine Soll-Absenkung kurz vor dem Tagestick als Abweichung.
    snapshots = []

    def _read(manifest, ha_api, computed_values):
        snapshots.append(dict(computed_values))
        return SimpleNamespace(invalid_roles=(), roles={})

    monkeypatch.setattr(ticks, "read_snapshot_roles", _read)
    manifest = ChannelManifest(entity_ids={
        "curve_current": "number.curve", "shift_current": "number.shift", "heat_limit": "number.heat_limit",
    })
    options = {
        "curve_min": 0.4, "curve_max": 1.5, "shift_min": 15.0, "shift_max": 25.0,
        "heat_limit_min": 5.0, "heat_limit_max": 23.0,
        "min_flow_min": 20.0, "min_flow_max": 30.0, "boost_curve_value": 1.5, "boost_shift_value": 25.0,
    }
    ha, store = _LaggingPlantHa(), make_store()
    override = Override(store, manifest, ha, options, clock=clock)
    override.apply_server_values(curve=1.2, shift=18.5, heat_limit=17.0)
    assert sorted(ha.writes) == [("number.curve", 1.2), ("number.heat_limit", 17.0), ("number.shift", 18.5)]
    rt = SimpleNamespace(manifest=manifest, ha_api=ha, mqtt_client=None, store=store, override=override)

    ticks._attempt(rt, "s1", "daily")
    clock.advance(OWN_WRITE_SETTLE_SECONDS + 1)
    ticks._attempt(rt, "s2", "daily")

    assert snapshots == [
        {"curve_current": 1.2, "shift_current": 18.5, "heat_limit": 17.0},
        {"shift_current": 17.5},  # eingeschwungen: Live-Werte (curve/heat_limit liest read_snapshot_roles selbst)
    ]
