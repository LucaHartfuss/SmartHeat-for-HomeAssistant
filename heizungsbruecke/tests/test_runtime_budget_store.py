"""Schreibbudgets bei mehreren Kreisen: Schluessel der ganzen Anlage in `runtime/anlage_budget.json`, Schluessel je Kreis
im Zustand des Kreises. Die Hebel-Pipeline und die Durchsetzung sehen davon nichts: sie lesen und schreiben ueber
dieselben Aufrufe wie am einfachen Speicher."""
import dataclasses
import json
import logging

import pytest
from bindung_beispiele import WEISHAUPT_MODBUS

from heizungsbruecke.ha_binding import HaPlantBinding
from smartheat_core import write_budget
from smartheat_core.pipeline import LeverPipeline
from smartheat_core.safety import resolve_local_safety
from smartheat_runtime import backup_store
from smartheat_runtime.budget_store import AnlagenBudget, BudgetStore
from smartheat_runtime.roles import ChannelManifest
from smartheat_runtime.state import (
    ANLAGE_SCHLUESSEL,
    BridgeState,
    StateStore,
    StorageError,
    bind_to_tenant,
    gehoert_der_anlage,
)

DAY = "2026-10-03"
GRENZE = 10  # BindingDescription.daily_write_limit des Weishaupt-Beispiels
MANIFEST = ChannelManifest(refs={
    "curve_current": "number.hk", "shift_current": "number.normal", "heat_limit": "number.swu",
})
SAFETY = resolve_local_safety("weishaupt_wwp", "Heizkoerper")
BINDUNG = dataclasses.replace(WEISHAUPT_MODBUS, aux_originals=())


@pytest.fixture(autouse=True)
def _tag(monkeypatch):
    monkeypatch.setattr(write_budget, "today", lambda: DAY)


@pytest.fixture
def runtime_dir(tmp_path):
    return tmp_path / "runtime"


def _kreis_store(runtime_dir, kreis, backup=None):
    ordner = runtime_dir / f"kreis-{kreis}"
    if backup is not None:
        backup_store.save_backup(ordner / "backup.json", backup)
    return StateStore(ordner / "backup.json", ordner / "failsafe_state.json")


def _budget(runtime_dir):
    return AnlagenBudget(runtime_dir / "anlage_budget.json")


def _anlage_datei(runtime_dir):
    return json.loads((runtime_dir / "anlage_budget.json").read_text())


def _kreis_datei(runtime_dir, kreis):
    return json.loads((runtime_dir / f"kreis-{kreis}" / "backup.json").read_text())


def _eintrag(count=1):
    return {"day": DAY, "count": count}


def test_die_schluessel_der_anlage_stehen_in_der_anlagendatei_und_die_des_kreises_im_kreis(runtime_dir):
    store = BudgetStore(_kreis_store(runtime_dir, 1), _budget(runtime_dir))
    for key in (*ANLAGE_SCHLUESSEL, write_budget.BOOST_END, write_budget.RESTORE, "enforce:curve+heat_limit"):
        write_budget.put(store, key, _eintrag())
    assert set(_anlage_datei(runtime_dir)["write_budget"]) == {
        "writes:total", "zone_prepare", "boost_start", "emergency_start", "enforce:curve+heat_limit"}
    assert set(_kreis_datei(runtime_dir, 1)["write_budget"]) == {"boost_end", "restore"}
    assert set(store.state.write_budget) == set(_anlage_datei(runtime_dir)["write_budget"]) | set(
        _kreis_datei(runtime_dir, 1)["write_budget"])


def test_die_anlage_schluessel_sind_genau_die_der_ganzen_anlage():
    erwartet = {write_budget.TOTAL, write_budget.ZONE_PREPARE, write_budget.BOOST_START, write_budget.EMERGENCY_START}
    assert set(ANLAGE_SCHLUESSEL) == erwartet


def test_die_durchsetzung_je_hebel_gehoert_der_anlage_und_sonst_nur_die_festen_schluessel():
    for key in (*ANLAGE_SCHLUESSEL, "enforce:curve", "enforce:curve+level", "enforce:zone_mode"):
        assert gehoert_der_anlage(key), key
    for key in (write_budget.BOOST_END, write_budget.RESTORE, "enforce", "xenforce:curve"):
        assert not gehoert_der_anlage(key), key


def test_der_lebensdauerzaehler_gehoert_der_anlage(runtime_dir):
    store = BudgetStore(_kreis_store(runtime_dir, 1), _budget(runtime_dir))
    store.update(lifetime_writes=7)
    assert store.state.lifetime_writes == 7
    assert _anlage_datei(runtime_dir)["lifetime_writes"] == 7
    assert not (runtime_dir / "kreis-1").exists()


def test_andere_felder_gehen_an_den_kreis_und_nur_dorthin(runtime_dir):
    store = BudgetStore(_kreis_store(runtime_dir, 1), _budget(runtime_dir))
    store.update(learned={"curve": 0.5}, tenant_id="t")
    assert store.state.learned == {"curve": 0.5}
    assert _kreis_datei(runtime_dir, 1)["learned"] == {"curve": 0.5}
    assert not (runtime_dir / "anlage_budget.json").exists()


def test_neustart_liest_beide_orte_zurueck(runtime_dir):
    store = BudgetStore(_kreis_store(runtime_dir, 1), _budget(runtime_dir))
    write_budget.put(store, write_budget.TOTAL, _eintrag(4))
    write_budget.put(store, write_budget.BOOST_END, _eintrag(2))
    store.update(lifetime_writes=9)
    neu = BudgetStore(_kreis_store(runtime_dir, 1), _budget(runtime_dir))
    assert neu.state.write_budget == {"writes:total": _eintrag(4), "boost_end": _eintrag(2)}
    assert neu.state.lifetime_writes == 9


def test_zwei_kreise_zaehlen_den_gesamtzaehler_gemeinsam_und_den_rest_je_kreis(runtime_dir):
    anlage = _budget(runtime_dir)
    eins = BudgetStore(_kreis_store(runtime_dir, 1), anlage)
    zwei = BudgetStore(_kreis_store(runtime_dir, 2), anlage)
    write_budget.put(eins, write_budget.TOTAL, _eintrag(3))
    write_budget.put(zwei, write_budget.TOTAL, write_budget.added(write_budget.get(zwei, write_budget.TOTAL), 2, 0.0, DAY))
    write_budget.put(eins, write_budget.RESTORE, _eintrag(1))
    assert write_budget.get(eins, write_budget.TOTAL)["count"] == 5
    assert write_budget.get(zwei, write_budget.TOTAL)["count"] == 5
    assert write_budget.get(eins, write_budget.RESTORE) is not None
    assert write_budget.get(zwei, write_budget.RESTORE) is None
    assert eins.state.lifetime_writes == zwei.state.lifetime_writes


@pytest.mark.parametrize("vorher,zweiter_darf", [(5, True), (6, False)])
def test_das_tageslimit_der_durchsetzung_gilt_je_hebel_fuer_die_ganze_anlage(runtime_dir, vorher, zweiter_darf):
    """Zwei Kreise zaehlen die Durchsetzung desselben Hebels an einem Eintrag: erst die Summe beider Kreise erreicht
    das Tageslimit (sechs), nicht jeder Kreis fuer sich."""
    anlage = _budget(runtime_dir)
    eins = BudgetStore(_kreis_store(runtime_dir, 1), anlage)
    zwei = BudgetStore(_kreis_store(runtime_dir, 2), anlage)
    regel = write_budget.enforce_rule(write_budget.MAX_PER_DAY)
    for _ in range(3):
        write_budget.before_attempt(eins, "enforce:curve", regel, 0.0, day=DAY)
    for _ in range(vorher - 3):
        write_budget.before_attempt(zwei, "enforce:curve", regel, 100.0, day=DAY)
    spaeter = 100.0 + 2 * write_budget.INTERVAL_SECONDS
    assert write_budget.may_attempt(eins, "enforce:curve", regel, spaeter) is zweiter_darf
    assert write_budget.may_attempt(zwei, "enforce:curve", regel, spaeter) is zweiter_darf
    assert write_budget.get(eins, "enforce:curve")["count"] == vorher
    assert not (runtime_dir / "kreis-1").exists()  # im Kreis steht davon nichts
    assert not (runtime_dir / "kreis-2").exists()
    assert _anlage_datei(runtime_dir)["write_budget"]["enforce:curve"]["count"] == vorher


def test_ein_anderer_hebel_zaehlt_getrennt(runtime_dir):
    anlage = _budget(runtime_dir)
    eins = BudgetStore(_kreis_store(runtime_dir, 1), anlage)
    zwei = BudgetStore(_kreis_store(runtime_dir, 2), anlage)
    regel = write_budget.enforce_rule(write_budget.MAX_PER_DAY)
    write_budget.before_attempt(eins, "enforce:curve", regel, 0.0, day=DAY)
    write_budget.before_attempt(zwei, "enforce:heat_limit", regel, 0.0, day=DAY)
    assert write_budget.get(zwei, "enforce:curve")["count"] == 1
    assert write_budget.get(eins, "enforce:heat_limit")["count"] == 1


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


def _pipeline(store, clock):
    ha = Ha()
    pipeline = LeverPipeline(store, HaPlantBinding(ha, MANIFEST, BINDUNG), SAFETY, clock=clock)
    clock.advance(BINDUNG.settle_seconds + 1)
    return pipeline, ha


@pytest.mark.parametrize("vorher,erste,zweite", [(4, 2, 3), (5, 2, 3), (7, 2, 1), (8, 2, 0), (9, 1, 0)])
def test_zwei_kreise_ueberschreiten_zusammen_nie_das_tageslimit(runtime_dir, clock, vorher, erste, zweite):
    """Vorher `vorher` Schreibvorgaenge der Anlage; der erste Kreis schreibt zwei Hebel, der zweite drei. Bis zur Grenze
    (zehn) wird alles geschrieben (vier: neun gesamt, fuenf: genau zehn), danach keiner mehr: der zweite Kreis bekommt nur
    den Rest."""
    backup_store.save_backup(runtime_dir / "anlage_budget.json", {"write_budget": {"writes:total": _eintrag(vorher)}})
    anlage = _budget(runtime_dir)
    eins, ha_eins = _pipeline(BudgetStore(_kreis_store(runtime_dir, 1), anlage), clock)
    zwei, ha_zwei = _pipeline(BudgetStore(_kreis_store(runtime_dir, 2), anlage), clock)
    eins.apply_server_values({"curve": 0.8, "room_setpoint": 21.0})
    zwei.apply_server_values({"curve": 0.8, "room_setpoint": 21.0, "heat_limit": 17.0})
    assert (len(ha_eins.writes), len(ha_zwei.writes)) == (erste, zweite)
    assert _anlage_datei(runtime_dir)["write_budget"]["writes:total"]["count"] == min(GRENZE, vorher + 5)


def test_ein_bestand_aus_der_migration_geht_einmal_in_die_anlagendatei_ueber(runtime_dir):
    """Die migrierte Sicherung des Kreises traegt noch Zaehler der ganzen Anlage; sie wandern in die Anlagendatei und
    verschwinden aus dem Kreis, sodass der Tageszaehler und die Lebensdauer nicht von vorn beginnen."""
    kreis = _kreis_store(runtime_dir, 1, backup={
        "write_budget": {"writes:total": _eintrag(4), "boost_end": _eintrag(2)}, "lifetime_writes": 120})
    store = BudgetStore(kreis, _budget(runtime_dir))
    assert store.state.write_budget == {"writes:total": _eintrag(4), "boost_end": _eintrag(2)}
    assert store.state.lifetime_writes == 120
    assert _anlage_datei(runtime_dir) == {"write_budget": {"writes:total": _eintrag(4)}, "lifetime_writes": 120}
    assert _kreis_datei(runtime_dir, 1)["write_budget"] == {"boost_end": _eintrag(2)}
    assert "lifetime_writes" not in _kreis_datei(runtime_dir, 1)


def test_die_durchsetzung_eines_alten_kreisbestands_geht_in_die_vorhandene_anlagendatei_wenn_sie_dort_fehlt(runtime_dir):
    """Beim Umstieg auf die Zaehlung je Anlage steht die Durchsetzung eines Hebels noch im Kreis: sie wandert in die
    Anlagendatei, ein dort schon vorhandener Eintrag desselben Hebels bleibt unberuehrt."""
    backup_store.save_backup(runtime_dir / "anlage_budget.json", {"write_budget": {
        "writes:total": _eintrag(8), "enforce:heat_limit": _eintrag(2)}})
    kreis = _kreis_store(runtime_dir, 1, backup={"write_budget": {
        "enforce:curve": _eintrag(3), "enforce:heat_limit": _eintrag(5), "restore": _eintrag(1)}})
    store = BudgetStore(kreis, _budget(runtime_dir))
    assert write_budget.get(store, "enforce:curve") == _eintrag(3)
    assert write_budget.get(store, "enforce:heat_limit") == _eintrag(2)
    assert write_budget.get(store, "writes:total") == _eintrag(8)
    assert _kreis_datei(runtime_dir, 1)["write_budget"] == {"restore": _eintrag(1)}
    assert set(_anlage_datei(runtime_dir)["write_budget"]) == {"writes:total", "enforce:curve", "enforce:heat_limit"}


def test_ein_alter_bestand_ueberschreibt_nie_eine_vorhandene_anlagendatei(runtime_dir):
    backup_store.save_backup(runtime_dir / "anlage_budget.json",
                             {"write_budget": {"writes:total": _eintrag(8)}, "lifetime_writes": 500})
    kreis = _kreis_store(runtime_dir, 2, backup={
        "write_budget": {"writes:total": _eintrag(1), "restore": _eintrag(1)}, "lifetime_writes": 3})
    store = BudgetStore(kreis, _budget(runtime_dir))
    assert write_budget.get(store, "writes:total") == _eintrag(8)
    assert store.state.lifetime_writes == 500
    assert _kreis_datei(runtime_dir, 2)["write_budget"] == {"restore": _eintrag(1)}
    assert _anlage_datei(runtime_dir)["lifetime_writes"] == 500


def test_ein_kreis_ohne_alte_zaehler_wird_beim_start_nicht_geschrieben(runtime_dir):
    BudgetStore(_kreis_store(runtime_dir, 1), _budget(runtime_dir))
    assert not (runtime_dir / "kreis-1").exists()
    assert not (runtime_dir / "anlage_budget.json").exists()


@pytest.mark.parametrize("inhalt", ["{kaputt", "[1, 2]", '{"write_budget": 5}', '{"write_budget": {"a": {"day": 1}}}',
                                    '{"lifetime_writes": -3}', '{"lifetime_writes": true}'])
def test_eine_unbrauchbare_anlagendatei_gilt_als_leer(runtime_dir, inhalt):
    runtime_dir.mkdir()
    (runtime_dir / "anlage_budget.json").write_text(inhalt)
    store = BudgetStore(_kreis_store(runtime_dir, 1), _budget(runtime_dir))
    assert store.state.write_budget == {}
    assert store.state.lifetime_writes == 0


def test_die_uhr_des_vorigen_laufs_wird_aus_der_anlagendatei_nicht_uebernommen(runtime_dir):
    backup_store.save_backup(runtime_dir / "anlage_budget.json", {"write_budget": {
        "writes:total": {"day": DAY, "count": 4, "last": 123.0, "limit_notified": DAY}}})
    store = BudgetStore(_kreis_store(runtime_dir, 1), _budget(runtime_dir))
    assert write_budget.get(store, "writes:total") == {"day": DAY, "count": 4, "limit_notified": DAY}


def test_der_rest_der_pipeline_laeuft_am_kreisspeicher(runtime_dir):
    """Zustellung, Datentraeger-Merker und gespeicherte Felder verhalten sich wie am einfachen Speicher."""
    store = BudgetStore(_kreis_store(runtime_dir, 1), _budget(runtime_dir))
    assert isinstance(store.state, BridgeState)
    store.update(restore_point={"curve": 0.8})
    assert store.is_saved("restore_point")
    store.update_saved(restore_point={"curve": 0.9})
    assert store.saved_restore_point(("curve",)) == {"curve": 0.9}
    store.revert_to_saved("restore_point")
    assert store.state.restore_point == {"curve": 0.9}
    assert not store.storage_failed
    store.flush()
    assert (runtime_dir / "kreis-1" / "backup.json").exists()


@pytest.mark.parametrize("aufruf", ["update_saved", "is_saved", "revert_to_saved"])
def test_die_zaehler_der_anlage_gehen_nie_ueber_die_kreisfunktionen(runtime_dir, aufruf):
    store = BudgetStore(_kreis_store(runtime_dir, 1), _budget(runtime_dir))
    argumente = {"update_saved": {"lifetime_writes": 1}, "is_saved": None, "revert_to_saved": None}[aufruf]
    with pytest.raises(ValueError, match="Anlage"):
        if argumente is not None:
            getattr(store, aufruf)(**argumente)
        else:
            getattr(store, aufruf)("write_budget")


def test_ein_fehler_in_der_anlagendatei_laesst_den_kreis_trotzdem_schreiben_und_wird_nachgeholt(runtime_dir, tmp_path):
    (tmp_path / "datei").write_text("im Weg")
    anlage = AnlagenBudget(tmp_path / "datei" / "anlage_budget.json")  # der Ordner ist eine Datei
    store = BudgetStore(_kreis_store(runtime_dir, 1), anlage)
    with pytest.raises(StorageError):
        store.update(write_budget={"writes:total": _eintrag(2), "restore": _eintrag(1)}, lifetime_writes=2)
    assert store.state.lifetime_writes == 2  # gilt im Speicher weiter
    assert write_budget.get(store, "writes:total") == _eintrag(2)
    assert _kreis_datei(runtime_dir, 1)["write_budget"] == {"restore": _eintrag(1)}
    assert store.storage_failed
    (tmp_path / "datei").unlink()
    store.flush()
    assert not store.storage_failed
    assert json.loads((tmp_path / "datei" / "anlage_budget.json").read_text())["lifetime_writes"] == 2


def test_put_meldet_den_fehler_der_anlagendatei_nicht_nach_oben(runtime_dir, tmp_path, caplog):
    (tmp_path / "datei").write_text("im Weg")
    store = BudgetStore(_kreis_store(runtime_dir, 1), AnlagenBudget(tmp_path / "datei" / "anlage_budget.json"))
    with caplog.at_level(logging.WARNING):
        write_budget.put(store, write_budget.TOTAL, _eintrag(1))
    assert write_budget.get(store, write_budget.TOTAL) == _eintrag(1)
    assert "nicht gespeichert" in caplog.text


def test_eine_andere_anlage_setzt_auch_die_zaehler_der_anlage_zurueck(runtime_dir):
    store = BudgetStore(_kreis_store(runtime_dir, 1), _budget(runtime_dir))
    bind_to_tenant(store, "t", "anlage-a")
    write_budget.put(store, write_budget.TOTAL, _eintrag(6))
    write_budget.put(store, write_budget.RESTORE, _eintrag(1))
    store.update(lifetime_writes=40)
    assert bind_to_tenant(store, "t", "anlage-b") == "andere_anlage"
    assert store.state.write_budget == {}
    assert store.state.lifetime_writes == 0
    assert _anlage_datei(runtime_dir) == {}
    assert "write_budget" not in _kreis_datei(runtime_dir, 1)


def test_eine_andere_anlage_behaelt_die_zaehler_der_anlage_wenn_weitere_kreise_sie_teilen(runtime_dir):
    """Mehrere Kreise teilen die Zaehler der Anlage: ein Kreis auf einer anderen Anlage setzt nur seinen eigenen
    Zustand zurueck; die Zaehler der Anlage (Tageslimit, Lebensdauer) bleiben, ein Zuruecksetzen wuerde den
    Verschleissschutz der anderen Kreise aufheben."""
    store = BudgetStore(_kreis_store(runtime_dir, 1), _budget(runtime_dir))
    bind_to_tenant(store, "t", "anlage-a")
    write_budget.put(store, write_budget.TOTAL, _eintrag(6))
    write_budget.put(store, write_budget.RESTORE, _eintrag(1))
    store.update(lifetime_writes=40, boost_active=True)
    assert bind_to_tenant(store, "t", "anlage-b", zaehler_der_anlage_behalten=True) == "andere_anlage"
    assert not store.state.boost_active  # der Zustand des Kreises ist zurueckgesetzt
    assert store.state.plant_id == "anlage-b"
    assert write_budget.get(store, write_budget.TOTAL) == _eintrag(6)
    assert store.state.lifetime_writes == 40
    assert _anlage_datei(runtime_dir)["lifetime_writes"] == 40
    assert write_budget.get(store, write_budget.RESTORE) is None  # nur die Zaehler der Anlage bleiben, der des Kreises geht


def test_dieselbe_anlage_behaelt_die_zaehler_der_anlage(runtime_dir):
    store = BudgetStore(_kreis_store(runtime_dir, 1), _budget(runtime_dir))
    bind_to_tenant(store, "t", "anlage-a")
    write_budget.put(store, write_budget.TOTAL, _eintrag(6))
    neu = BudgetStore(_kreis_store(runtime_dir, 1), _budget(runtime_dir))
    assert bind_to_tenant(neu, "t", "anlage-a") == "unveraendert"
    assert write_budget.get(neu, write_budget.TOTAL) == _eintrag(6)


def test_die_anlagendatei_gilt_nach_dem_ersten_schreiben_als_vorhanden(runtime_dir):
    anlage = _budget(runtime_dir)
    assert not anlage.vorhanden
    anlage.update(lifetime_writes=1)
    assert anlage.vorhanden
    assert _budget(runtime_dir).vorhanden


def test_flush_meldet_den_fehler_der_anlagendatei(runtime_dir, tmp_path):
    (tmp_path / "datei").write_text("im Weg")
    store = BudgetStore(_kreis_store(runtime_dir, 1), AnlagenBudget(tmp_path / "datei" / "anlage_budget.json"))
    with pytest.raises(StorageError):
        store.update(lifetime_writes=3)
    assert store.storage_failed
    with pytest.raises(StorageError):
        store.flush()
    assert store.storage_failed


def test_ein_schmutziger_stand_wird_auch_geschrieben_wenn_er_dem_gespeicherten_gleicht(tmp_path):
    (tmp_path / "datei").write_text("im Weg")
    anlage = AnlagenBudget(tmp_path / "datei" / "anlage_budget.json")
    with pytest.raises(StorageError):
        anlage.update(lifetime_writes=5)
    assert anlage.storage_failed
    (tmp_path / "datei").unlink()
    anlage.update(lifetime_writes=0)  # zurueck auf den gespeicherten Stand, aber die Datei fehlt noch
    assert not anlage.storage_failed
    assert anlage.vorhanden
    assert (tmp_path / "datei" / "anlage_budget.json").exists()


def test_flush_meldet_bei_zwei_fehlern_den_der_anlage_zuerst(tmp_path):
    (tmp_path / "a").write_text("im Weg")
    (tmp_path / "k").write_text("im Weg")
    kreis = StateStore(tmp_path / "k" / "backup.json", tmp_path / "k" / "failsafe_state.json")
    store = BudgetStore(kreis, AnlagenBudget(tmp_path / "a" / "anlage_budget.json"))
    with pytest.raises(StorageError):
        store.update(lifetime_writes=2, boost_active=True)
    with pytest.raises(StorageError, match="anlage_budget.json"):
        store.flush()
