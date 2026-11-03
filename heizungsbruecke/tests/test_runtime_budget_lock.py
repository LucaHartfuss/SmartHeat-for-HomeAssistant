"""Zwei Kreise im selben Prozess teilen sich eine `AnlagenBudget`. Pruefen, Schreiben und Zaehlen des Tageslimits
(und jedes Lesen-Aendern-Schreiben eines Zaehlers der Anlage) laufen als eine Einheit unter dem Lock der Anlage:
gemeinsam gehen sie nie ueber das Tageslimit, und kein Zaehlschritt geht verloren.

Der Treffpunkt macht die Verzahnung reproduzierbar: liest ein Kreis die Zaehler der Anlage, wartet er kurz auf den
anderen. Ohne gemeinsamen Lock lesen beide denselben Stand; mit Lock kommt der zweite erst nach dem ersten und der
Treffpunkt laeuft in die Frist."""
import contextlib
import dataclasses
import json
import threading

import pytest
from bindung_beispiele import WEISHAUPT_MODBUS

from heizungsbruecke.ha_binding import HaPlantBinding
from smartheat_core import write_budget
from smartheat_core.pipeline import LeverPipeline
from smartheat_core.safety import resolve_local_safety
from smartheat_runtime import backup_store
from smartheat_runtime.budget_store import AnlagenBudget, BudgetStore
from smartheat_runtime.roles import ChannelManifest
from smartheat_runtime.state import StateStore

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


class TreffpunktBudget(AnlagenBudget):
    """Wer die Zaehler liest, wartet bis zu einer halben Sekunde auf den zweiten Leser."""

    def __init__(self, path):
        super().__init__(path)
        self._treffpunkt = threading.Barrier(2, timeout=0.5)
        self._treffpunkt_lebensdauer = threading.Barrier(2, timeout=0.5)

    @staticmethod
    def _warten(treffpunkt) -> None:
        with contextlib.suppress(threading.BrokenBarrierError):
            treffpunkt.wait()

    @property
    def write_budget(self) -> dict:
        stand = super().write_budget
        self._warten(self._treffpunkt)
        return stand

    @property
    def lifetime_writes(self) -> int:
        stand = super().lifetime_writes
        self._warten(self._treffpunkt_lebensdauer)
        return stand


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


def _kreis_store(runtime_dir, kreis):
    ordner = runtime_dir / f"kreis-{kreis}"
    return StateStore(ordner / "backup.json", ordner / "failsafe_state.json")


def _pipeline(store, clock):
    ha = Ha()
    pipeline = LeverPipeline(store, HaPlantBinding(ha, MANIFEST, BINDUNG), SAFETY, clock=clock)
    clock.advance(BINDUNG.settle_seconds + 1)
    return pipeline, ha


def _gleichzeitig(*aufgaben):
    fehler = []

    def lauf(aufgabe):
        try:
            aufgabe()
        except BaseException as error:  # noqa: BLE001 - der Test meldet jeden Fehler eines Threads
            fehler.append(error)

    threads = [threading.Thread(target=lauf, args=(aufgabe,)) for aufgabe in aufgaben]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert not any(thread.is_alive() for thread in threads), "Thread haengt (Verklemmung)"
    assert not fehler, fehler


def _anlage_datei(runtime_dir):
    return json.loads((runtime_dir / "anlage_budget.json").read_text())


@pytest.mark.parametrize("vorher,schreibvorgaenge", [(0, 2), (7, 2), (8, 2), (9, 1), (10, 0)])
def test_zwei_kreise_gleichzeitig_gehen_nie_ueber_das_tageslimit(tmp_path, clock, vorher, schreibvorgaenge):
    """Beide Kreise schreiben je einen Hebel zur selben Zeit. Bis zur Grenze (zehn) gehen beide durch, bei neun nur
    einer, bei zehn keiner; der Zaehler der Anlage steht danach genau auf Vorher plus den echten Vorgaengen."""
    runtime_dir = tmp_path / "runtime"
    if vorher:
        backup_store.save_backup(
            runtime_dir / "anlage_budget.json", {"write_budget": {"writes:total": {"day": DAY, "count": vorher}}})
    anlage = TreffpunktBudget(runtime_dir / "anlage_budget.json")
    eins, ha_eins = _pipeline(BudgetStore(_kreis_store(runtime_dir, 1), anlage), clock)
    zwei, ha_zwei = _pipeline(BudgetStore(_kreis_store(runtime_dir, 2), anlage), clock)

    def schreiben(pipeline):
        def aufgabe():
            # WriteBudgetExhausted am Limit ist ein gueltiger Ausgang
            with contextlib.suppress(Exception):
                pipeline.apply_server_values({"curve": 0.8})
        return aufgabe

    _gleichzeitig(schreiben(eins), schreiben(zwei))

    assert len(ha_eins.writes) + len(ha_zwei.writes) == schreibvorgaenge
    erwartet = vorher + schreibvorgaenge
    assert erwartet <= GRENZE
    gespeichert = _anlage_datei(runtime_dir)["write_budget"]["writes:total"]["count"] if erwartet else 0
    assert gespeichert == erwartet


def test_zwei_kreise_gleichzeitig_verlieren_keinen_lebensdauerschritt(tmp_path, clock):
    runtime_dir = tmp_path / "runtime"
    anlage = TreffpunktBudget(runtime_dir / "anlage_budget.json")
    eins, _ = _pipeline(BudgetStore(_kreis_store(runtime_dir, 1), anlage), clock)
    zwei, _ = _pipeline(BudgetStore(_kreis_store(runtime_dir, 2), anlage), clock)
    _gleichzeitig(lambda: eins.apply_server_values({"curve": 0.8}), lambda: zwei.apply_server_values({"curve": 0.9}))
    assert _anlage_datei(runtime_dir)["lifetime_writes"] == 2


def test_zwei_kreise_ueberschreiben_sich_nicht_mit_einer_alten_kopie(tmp_path):
    """Jeder Kreis liest die Zaehler der Anlage, aendert einen Schluessel und schreibt sie zurueck: ohne gemeinsamen
    Lock ueberschreibt der zweite den Schluessel des ersten mit seiner alten Kopie."""
    runtime_dir = tmp_path / "runtime"
    anlage = TreffpunktBudget(runtime_dir / "anlage_budget.json")
    eins = BudgetStore(_kreis_store(runtime_dir, 1), anlage)
    zwei = BudgetStore(_kreis_store(runtime_dir, 2), anlage)

    def zaehlen(store, key):
        def aufgabe():
            with store.transaktion():
                write_budget.put(store, key, write_budget.counted(write_budget.get(store, key), 0.0, DAY))
        return aufgabe

    _gleichzeitig(zaehlen(eins, write_budget.BOOST_START), zaehlen(zwei, write_budget.ZONE_PREPARE))

    assert set(_anlage_datei(runtime_dir)["write_budget"]) == {write_budget.BOOST_START, write_budget.ZONE_PREPARE}


def test_ein_kreis_haelt_den_lock_der_anlage_wiederholt_im_selben_thread(tmp_path):
    """Pruefen und Zaehlen verschachteln sich; derselbe Thread darf den Lock mehrfach nehmen."""
    store = BudgetStore(_kreis_store(tmp_path / "runtime", 1), AnlagenBudget(tmp_path / "runtime" / "a.json"))
    with store.transaktion(), store.transaktion():
        write_budget.put(store, write_budget.TOTAL, {"day": DAY, "count": 1})
    assert write_budget.get(store, write_budget.TOTAL)["count"] == 1


def test_der_einfache_speicher_kennt_die_transaktion_ohne_lock(tmp_path):
    store = StateStore(tmp_path / "backup.json", tmp_path / "failsafe_state.json")
    with store.transaktion():
        write_budget.put(store, write_budget.TOTAL, {"day": DAY, "count": 1})
    assert write_budget.get(store, write_budget.TOTAL)["count"] == 1
