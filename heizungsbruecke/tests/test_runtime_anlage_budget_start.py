"""Die Laufzeit fuehrt die Zaehler der ganzen Anlage in `runtime/anlage_budget.json`, neben den Kreisordnern; alles
andere bleibt im Zustand des Kreises. Beide Hosts geben den Pfad in der BootInfo mit."""
import json

from runtime_helpers import _is_running, _start_bridge

from smartheat_core import write_budget
from smartheat_runtime.budget_store import BudgetStore

EINTRAG = {"day": "2026-10-03", "count": 2}


def _lesen(pfad):
    return json.loads(pfad.read_text())


def test_die_laufzeit_verteilt_die_zaehler_auf_anlage_und_kreis(env):
    bridge = _start_bridge(env)
    assert _is_running(bridge)
    assert isinstance(bridge.store, BudgetStore)

    bridge.store.update(write_budget={write_budget.TOTAL: EINTRAG, "enforce:curve": EINTRAG}, lifetime_writes=7)

    anlage = _lesen(env.paths["RUNTIME_DIR"] / "anlage_budget.json")
    assert anlage == {"write_budget": {write_budget.TOTAL: EINTRAG}, "lifetime_writes": 7}
    kreis = _lesen(env.paths["BACKUP_PATH"])
    assert kreis["write_budget"] == {"enforce:curve": EINTRAG}
    assert "lifetime_writes" not in kreis or kreis["lifetime_writes"] == 0


def test_der_neustart_liest_beide_orte(env):
    bridge = _start_bridge(env)
    bridge.store.update(write_budget={write_budget.TOTAL: EINTRAG, "enforce:curve": EINTRAG}, lifetime_writes=7)

    neu = _start_bridge(env)

    assert neu.store.state.write_budget == {write_budget.TOTAL: EINTRAG, "enforce:curve": EINTRAG}
    assert neu.store.state.lifetime_writes == 7
