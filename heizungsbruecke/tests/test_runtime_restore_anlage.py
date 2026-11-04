"""Zeigt eine Kreisnummer jetzt auf eine andere Anlage als die, deren Zustand ihre Datei haelt, schreiben weder die
Rueckkehr auf den Wiederherstellungspunkt noch die Boost-Rücknahme (Konfigurationsfehler, Abmelden) auf die neuen
Entities: der Wiederherstellungspunkt gehoert zu den alten. Gemeldet wird mit dem Code `zustand_andere_anlage`."""
import dataclasses

import pytest
from bindung_beispiele import WEISHAUPT_MODBUS

from heizungsbruecke.ha_binding import HaPlantBinding
from smartheat_core.levers import LEVER_SETS
from smartheat_core.safety import resolve_local_safety
from smartheat_runtime import app
from smartheat_runtime.app import GRUND_ANDERE_ANLAGE, RestoreParts
from smartheat_runtime.backup_store import save_backup
from smartheat_runtime.notifier import Notifier
from smartheat_runtime.roles import ChannelManifest
from smartheat_runtime.state import StateStore
from smartheat_runtime.status import StatusReporter

MANIFEST = ChannelManifest(refs={
    "curve_current": "number.hk", "shift_current": "number.normal", "heat_limit": "number.swu",
})
SAFETY = resolve_local_safety("weishaupt_wwp", "Heizkoerper")
BINDUNG = dataclasses.replace(WEISHAUPT_MODBUS, aux_originals=())
ALT, NEU = "weishaupt:alt", "weishaupt:neu"
PUNKT = {"curve": 1.0, "room_setpoint": 20.0, "heat_limit": 16.0}


class Ha:
    def __init__(self):
        self.states = {"number.hk": 0.75, "number.normal": 20.0, "number.swu": 18.0}
        self.writes = []

    def get_state(self, entity_id):
        return self.states[entity_id]

    def get_raw_state(self, entity_id):
        return str(self.states.get(entity_id, "on"))

    def set_number_value(self, entity_id, value):
        self.states[entity_id] = value
        self.writes.append((entity_id, value))


class Senke:
    """Status- und Meldesenke in einem."""

    def __init__(self):
        self.ereignisse = []
        self.meldungen = []

    def publish(self, event):
        self.ereignisse.append(event)

    def push(self, key, message):
        self.meldungen.append(message)

    def show(self, key, message):
        self.meldungen.append(message)

    def withdraw(self, key):
        pass


def _aufbau(tmp_path, *, plant_id):
    save_backup(tmp_path / "backup.json", {
        "boost_active": True, "restore_point": PUNKT, "tenant_id": "t1", "plant_id": plant_id})
    store = StateStore(tmp_path / "backup.json", tmp_path / "failsafe_state.json")
    ha = Ha()
    senke = Senke()
    notifier = Notifier(store, senke)
    status = StatusReporter(senke, "t1", store, LEVER_SETS["weishaupt_wwp"], "1.0")
    return store, ha, senke, notifier, status


def _teile(ha, plant_id):
    return RestoreParts(HaPlantBinding(ha, MANIFEST, BINDUNG), SAFETY, plant_id)


def test_boost_ruecknahme_schreibt_bei_gleicher_anlage(tmp_path, clock):
    store, ha, *_ = _aufbau(tmp_path, plant_id=NEU)

    assert app._end_boosts(store, _teile(ha, NEU), clock) is True
    assert ha.writes
    assert not store.state.boost_active


def test_boost_ruecknahme_schreibt_bei_anderer_anlage_nichts(tmp_path, clock):
    store, ha, *_ = _aufbau(tmp_path, plant_id=ALT)

    assert app._end_boosts(store, _teile(ha, NEU), clock) is False
    assert ha.writes == []
    assert store.state.boost_active  # der Zustand bleibt, wie er war


def test_ein_zustand_ohne_kennung_gilt_als_gebunden(tmp_path, clock):
    """Ein Bestand ohne Bindung wird nicht verworfen (bind_to_tenant): die Ruecknahme schreibt weiter."""
    store, ha, *_ = _aufbau(tmp_path, plant_id=None)

    assert app._end_boosts(store, _teile(ha, NEU), clock) is True
    assert ha.writes


def test_konfigurationsfehler_nimmt_den_boost_der_anderen_anlage_nicht_zurueck(tmp_path, clock):
    store, ha, senke, notifier, status = _aufbau(tmp_path, plant_id=ALT)

    bridge = app._fail_start(notifier, status, clock, "Konfiguration kaputt", neustart=lambda: None, store=store,
                             parts=_teile(ha, NEU))

    assert bridge.reason == "konfigurationsfehler"
    assert ha.writes == []
    assert store.state.boost_active
    assert any(GRUND_ANDERE_ANLAGE in text for text in senke.meldungen)
    assert any("Boost-Werten" in text for text in senke.meldungen)


def test_konfigurationsfehler_ohne_teile_nennt_nicht_den_code_der_anderen_anlage(tmp_path, clock):
    store, ha, senke, notifier, status = _aufbau(tmp_path, plant_id=NEU)

    app._fail_start(notifier, status, clock, "Konfiguration kaputt", neustart=lambda: None, store=store, parts=None)

    assert any("Boost-Werten" in text for text in senke.meldungen)
    assert not any(GRUND_ANDERE_ANLAGE in text for text in senke.meldungen)


def test_scheitert_die_ruecknahme_bei_gleicher_anlage_nennt_die_meldung_nicht_den_code(tmp_path, clock):
    store, ha, senke, notifier, status = _aufbau(tmp_path, plant_id=NEU)

    def kaputt(entity_id, value):
        raise OSError("weg")

    ha.set_number_value = kaputt

    app._fail_start(notifier, status, clock, "Konfiguration kaputt", neustart=lambda: None, store=store,
                    parts=_teile(ha, NEU))

    assert store.state.boost_active
    assert any("Boost-Werten" in text for text in senke.meldungen)
    assert not any(GRUND_ANDERE_ANLAGE in text for text in senke.meldungen)


def test_abmelden_schreibt_bei_anderer_anlage_nichts_und_meldet_den_code(tmp_path, clock):
    store, ha, senke, notifier, status = _aufbau(tmp_path, plant_id=ALT)

    bridge = app._sign_off(_teile(ha, NEU), store, notifier, status, clock, 60.0, neustart=lambda: None,
                           anlage="t1")

    assert bridge.reason == "abgemeldet"
    assert ha.writes == []
    assert store.state.boost_active
    assert GRUND_ANDERE_ANLAGE in bridge.status.event()["grund"]
    assert any("Boost-Werten" in text for text in senke.meldungen)
    assert any(GRUND_ANDERE_ANLAGE in text for text in senke.meldungen)


def test_abmelden_ohne_teile_nennt_nicht_den_code_der_anderen_anlage(tmp_path, clock):
    store, ha, senke, notifier, status = _aufbau(tmp_path, plant_id=NEU)

    app._sign_off(None, store, notifier, status, clock, 60.0, neustart=lambda: None, anlage="t1")

    assert any("Boost-Werten" in text for text in senke.meldungen)
    assert not any(GRUND_ANDERE_ANLAGE in text for text in senke.meldungen)


@pytest.mark.parametrize("gleich", [True, False])
def test_abmelden_ohne_laufenden_boost_schreibt_nur_bei_gleicher_anlage(tmp_path, clock, gleich):
    """Auch das Zuruecksetzen auf die Ursprungswerte (ohne Boost) gehoert zur Anlage des Zustands."""
    store, ha, senke, notifier, status = _aufbau(tmp_path, plant_id=NEU if gleich else ALT)
    store.update(boost_active=False)

    app._sign_off(_teile(ha, NEU), store, notifier, status, clock, 60.0, neustart=lambda: None, anlage="t1",
                  server_originals=lambda: {"curve": 1.2, "room_setpoint": 21.0, "heat_limit": 17.0})

    assert bool(ha.writes) is gleich


def test_beide_hosts_tragen_die_kennung_der_anlage_in_die_teile():
    """RestoreParts hat die Kennung als Pflichtfeld: ein Host ohne sie liesse sich nicht bauen."""
    with pytest.raises(TypeError):
        RestoreParts(HaPlantBinding(Ha(), MANIFEST, BINDUNG), SAFETY)  # type: ignore[call-arg]
