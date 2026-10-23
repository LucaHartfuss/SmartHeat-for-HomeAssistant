"""Audit 4, A4-08 (GK-3, GW-2), Nutzer-Entscheidung E3."""

import logging

import pytest

from smartheat_runtime.backup_store import load_backup, save_backup
from smartheat_runtime.delivery import DeliveryState
from smartheat_runtime.state import StateStore, StorageError, bind_to_tenant

OLD = {
    "restore_point": {"curve": 1.4, "room_setpoint": 22.0, "heat_limit": 18.0}, "originals": {"heat_limit": 16.0},
    "last_published_target_rt": 21.0, "last_daily_trigger_date": "2026-10-07", "lifetime_writes": 7,
}


def _store(tmp_path, backup):
    save_backup(tmp_path / "backup.json", backup)
    return StateStore(tmp_path / "backup.json", tmp_path / "failsafe_state.json")


def test_existing_state_without_binding_is_kept(tmp_path):
    # Review Focus 1: Update von client1
    store = _store(tmp_path, OLD)
    assert bind_to_tenant(store, "s1", "anlage-a") == "bestand"
    assert store.state.restore_point == OLD["restore_point"] and store.state.last_published_target_rt == 21.0
    assert load_backup(tmp_path / "backup.json")["plant_id"] == "anlage-a"


def test_a_new_setup_of_the_same_plant_keeps_the_originals_and_forces_a_first_tick(tmp_path):
    # Review Focus 2
    store = _store(tmp_path, {**OLD, "tenant_id": "s1", "plant_id": "anlage-a"})
    assert bind_to_tenant(store, "s2", "anlage-a") == "neue_einrichtung"
    assert store.state.originals == {"heat_limit": 16.0} and store.state.restore_point == OLD["restore_point"]
    assert store.state.last_published_target_rt is None and store.state.last_daily_trigger_date is None


def test_another_plant_discards_the_plant_bound_state(tmp_path):
    store = _store(tmp_path, {**OLD, "tenant_id": "s1", "plant_id": "anlage-a", "boost_active": True})
    assert bind_to_tenant(store, "s2", "anlage-b") == "andere_anlage"
    state = store.state
    assert (state.restore_point, state.originals, state.lifetime_writes, state.boost_active) == ({}, {}, 0, False)
    assert state.last_published_target_rt is None


def test_the_same_setup_changes_nothing(tmp_path):
    store = _store(tmp_path, {**OLD, "tenant_id": "s1", "plant_id": "anlage-a"})
    assert bind_to_tenant(store, "s1", "anlage-a") == "unveraendert"
    assert store.state.last_published_target_rt == 21.0


@pytest.mark.parametrize("plant", ["anlage-a", "anlage-b"], ids=["neue_einrichtung", "andere_anlage"])
def test_the_delivery_state_is_reset_even_if_saving_the_binding_fails(tmp_path, monkeypatch, plant):
    # app.py faengt den StorageError ab und startet weiter: die Zustellung darf dann nicht mehr vom alten Setup stammen
    store = _store(tmp_path, {**OLD, "tenant_id": "s1", "plant_id": "anlage-a"})
    store.set_delivery(DeliveryState(server_failures=2, notbetrieb=True))

    def failing_update(**changes):
        raise StorageError("Datentraeger voll")

    monkeypatch.setattr(store, "update", failing_update)
    with pytest.raises(StorageError):
        bind_to_tenant(store, "s2", plant)
    assert store.state.delivery == DeliveryState()


def test_ein_bestand_mit_anlage_aber_ohne_einrichtung_ist_eine_neue_einrichtung(tmp_path):
    # Rueckwegfall: tenant_id fehlt, plant_id ist gesetzt -> nicht "bestand" (sonst bliebe der Erstkontakt-Tick aus)
    store = _store(tmp_path, {**OLD, "plant_id": "anlage-a"})
    assert store.state.tenant_id is None and store.state.plant_id == "anlage-a"
    assert bind_to_tenant(store, "s2", "anlage-a") == "neue_einrichtung"
    assert store.state.tenant_id == "s2" and store.state.originals == {"heat_limit": 16.0}
    assert store.state.last_published_target_rt is None and store.state.last_daily_trigger_date is None


# --- A6-36: Anlagen-Kennung aus dem Kreis; einmalige Uebernahme der frueheren Kennung (Hebel-Referenzen bis 1.1.0) ---

#: Wie Add-on 1.1.0 die Kennung schrieb (roles.lever_refs_identity), woertlich; und die neue aus dem Kreis.
FRUEHER = ('{"curve": "number.curve_current", "heat_limit": "number.heat_limit", "min_flow": "number.min_flow", '
           '"room_setpoint": "number.shift_current"}')
KREIS = '{"anlage": "S", "nummer": "0", "plattform": "mypyllant"}'
#: Anlagenbezogener Zustand eines laufenden Boosts (Ursprungswerte, Energiesummen, Boost, Budget, Rest von 1.0.0).
BESTAND_1_1_0 = {
    **OLD, "tenant_id": "s1", "plant_id": FRUEHER, "setup_id": "s1",
    "aux_originals": {"mode_select": "auto"}, "learned": {"curve": 1.3},
    "boost_active": True, "boost_since": "2026-10-07T06:00:00+00:00", "emergency_boost_active": False,
    "energy_state": {"primary_heating": {"raw": 812.5, "sum": 1234.5, "einheit": "kWh"}},
    "write_budget": {"writes:total": {"day": "2026-10-07", "count": 3}},
}
_ANLAGE = ("restore_point", "originals", "aux_originals", "learned", "boost_active", "boost_since",
           "energy_state", "write_budget", "lifetime_writes")


def test_fruehere_kennung_derselben_anlage_wird_uebernommen(tmp_path, caplog):
    store = _store(tmp_path, BESTAND_1_1_0)
    vorher = {key: getattr(store.state, key) for key in _ANLAGE}
    assert vorher["boost_active"] is True and vorher["energy_state"]  # Gegenprobe: der Bestand ist geladen
    with caplog.at_level(logging.INFO, logger="smartheat_runtime.state"):
        assert bind_to_tenant(store, "s1", KREIS, frueher=FRUEHER) == "uebernommen"
    assert {key: getattr(store.state, key) for key in _ANLAGE} == vorher
    assert store.state.last_published_target_rt == 21.0 and store.state.last_daily_trigger_date == "2026-10-07"
    gespeichert = load_backup(tmp_path / "backup.json")
    assert gespeichert["plant_id"] == KREIS and gespeichert["tenant_id"] == "s1"
    assert gespeichert["energy_state"] == BESTAND_1_1_0["energy_state"] and gespeichert["boost_active"] is True
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.INFO, "Anlagen-Kennung aus dem Heizkreis übernommen")]
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="smartheat_runtime.state"):  # zweiter Start: nichts mehr zu uebernehmen
        assert bind_to_tenant(StateStore(tmp_path / "backup.json", tmp_path / "failsafe_state.json"), "s1", KREIS,
                              frueher=FRUEHER) == "unveraendert"
    assert caplog.records == []


def test_uebernahme_mit_neuer_einrichtung_behaelt_die_ursprungswerte(tmp_path):
    """Kennung uebernommen, danach der Vergleich der Einrichtung wie bisher: neue tenant_id: Tick und Zustellung neu."""
    store = _store(tmp_path, BESTAND_1_1_0)
    store.set_delivery(DeliveryState(server_failures=2))
    assert bind_to_tenant(store, "s2", KREIS, frueher=FRUEHER) == "neue_einrichtung"
    assert store.state.plant_id == KREIS and store.state.tenant_id == "s2"
    assert store.state.originals == OLD["originals"] and store.state.boost_active is True
    assert store.state.last_published_target_rt is None and store.state.delivery == DeliveryState()
    assert load_backup(tmp_path / "backup.json")["plant_id"] == KREIS


@pytest.mark.parametrize("frueher", [None, "{}", FRUEHER.replace("number.heat_limit", "number.andere")],
                         ids=["ohne", "leer", "andere_hebel"])
def test_fremde_kennung_verwirft_weiter(tmp_path, caplog, frueher):
    """Nur die fruehere Kennung genau dieser Hebel gilt als dieselbe Anlage; jede andere bleibt eine andere Anlage."""
    store = _store(tmp_path, BESTAND_1_1_0)
    with caplog.at_level(logging.INFO, logger="smartheat_runtime.state"):
        assert bind_to_tenant(store, "s1", KREIS, frueher=frueher) == "andere_anlage"
    state = store.state
    assert (state.restore_point, state.originals, state.energy_state, state.boost_active) == ({}, {}, {}, False)
    assert state.plant_id == KREIS
    assert "Anlagen-Kennung aus dem Heizkreis übernommen" not in caplog.text


def test_die_uebernahme_greift_nur_beim_bestand_mit_frueherer_kennung(tmp_path):
    """Steht schon die neue Kennung (oder eine des Gateways), aendert frueher nichts: ein Bestand eines anderen Kreises
    wird verworfen, auch wenn frueher passt."""
    store = _store(tmp_path, {**BESTAND_1_1_0, "plant_id": KREIS.replace('"0"', '"1"')})
    assert bind_to_tenant(store, "s1", KREIS, frueher=FRUEHER) == "andere_anlage"
    assert store.state.originals == {} and store.state.boost_active is False


def test_uebernahme_bei_schreibfehler_behaelt_alles_und_versucht_es_beim_naechsten_start(
        tmp_path, monkeypatch, caplog):
    """Die Uebernahmezeile steht erst nach dem gelungenen Schreiben: beim Schreibfehler fehlt sie, der naechste Start
    schreibt sie genau einmal."""
    store = _store(tmp_path, BESTAND_1_1_0)
    def kaputt(*args, **kwargs):
        raise OSError("Datentraeger kaputt")

    monkeypatch.setattr("smartheat_runtime.backup_store.save_backup", kaputt)
    with caplog.at_level(logging.INFO, logger="smartheat_runtime.state"), pytest.raises(StorageError):
        bind_to_tenant(store, "s1", KREIS, frueher=FRUEHER)
    assert store.state.plant_id == KREIS and store.state.originals == OLD["originals"]
    assert load_backup(tmp_path / "backup.json")["plant_id"] == FRUEHER
    assert "Anlagen-Kennung aus dem Heizkreis übernommen" not in caplog.text
    monkeypatch.undo()
    neu = StateStore(tmp_path / "backup.json", tmp_path / "failsafe_state.json")
    with caplog.at_level(logging.INFO, logger="smartheat_runtime.state"):
        assert bind_to_tenant(neu, "s1", KREIS, frueher=FRUEHER) == "uebernommen"
    assert neu.state.originals == OLD["originals"]
    assert caplog.text.count("Anlagen-Kennung aus dem Heizkreis übernommen") == 1


def test_uebernahme_mit_neuer_einrichtung_bei_schreibfehler_ohne_uebernahmezeile(tmp_path, monkeypatch, caplog):
    store = _store(tmp_path, BESTAND_1_1_0)

    def kaputt(*args, **kwargs):
        raise OSError("Datentraeger kaputt")

    monkeypatch.setattr("smartheat_runtime.backup_store.save_backup", kaputt)
    with caplog.at_level(logging.INFO, logger="smartheat_runtime.state"), pytest.raises(StorageError):
        bind_to_tenant(store, "s2", KREIS, frueher=FRUEHER)
    assert "Anlagen-Kennung aus dem Heizkreis übernommen" not in caplog.text
    assert load_backup(tmp_path / "backup.json")["plant_id"] == FRUEHER
