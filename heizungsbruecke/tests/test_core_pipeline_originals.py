"""Hebel-Pipeline, Plan 3b: Ursprungswerte aller angefassten Hebel und Hilfs-Ursprungswerte (Betriebsart,
Komfort-/Absenk-Soll) -- gemerkt vor dem ersten Schreiben, zurueckgestellt bei Abo-Ende und Abmelden (Spec 5.4)."""
import math

import pytest
from bindung_beispiele import WEISHAUPT_MODBUS

from heizungsbruecke.ha_binding import HaPlantBinding
from smartheat_core.pipeline import DeviceWriteError, LeverPipeline
from smartheat_core.safety import resolve_local_safety
from smartheat_runtime.backup_store import load_backup
from smartheat_runtime.roles import ChannelManifest

MANIFEST = ChannelManifest(refs={"curve_current": "number.hk", "shift_current": "number.normal", "heat_limit": "number.swu"})
SAFETY = resolve_local_safety("weishaupt_wwp", "Heizkoerper")
AUX = {"mode_select": "Automatik", "setpoint_comfort": 22.0, "setpoint_setback": 18.0}


class Ha:
    def __init__(self):
        self.states = {"number.hk": 0.75, "number.normal": 20.0, "number.swu": 18.0}
        self.writes = []

    def get_state(self, entity_id):
        return self.states[entity_id]

    def get_raw_state(self, entity_id):
        return "on"

    def set_number_value(self, entity_id, value):
        self.states[entity_id] = value
        self.writes.append((entity_id, value))


class AuxBinding(HaPlantBinding):
    """HaPlantBinding mit Hilfswerten wie das Weishaupt-Binding (Task 7), ohne Betriebsart-Umschaltung."""

    def __init__(self, ha):
        super().__init__(ha, MANIFEST, WEISHAUPT_MODBUS)
        self.aux = dict(AUX)
        self.aux_error: Exception | None = None
        self.restored: list[dict] = []

    def read_aux(self):
        if self.aux_error is not None:
            raise self.aux_error
        return dict(self.aux)

    def restore_aux(self, values, levers=None):
        self.restored.append(dict(values))
        self.physical_writes += 1

    def aux_zulaessig(self, name, wert):
        return name == "mode_select" and wert in ("hz_operationmode_comfort", "hz_operationmode_automatic")


def _setup(make_store, clock, backup=None, server=None):
    store = make_store(backup=backup)
    ha = Ha()
    binding = AuxBinding(ha)
    pipeline = LeverPipeline(store, binding, SAFETY, clock=clock,
                             server_originals=(lambda: server) if server is not None else None)
    clock.advance(WEISHAUPT_MODBUS.settle_seconds + 1)
    return pipeline, store, ha, binding


def test_capture_remembers_every_lever_and_the_aux_values_once(make_store, clock, tmp_path):
    pipeline, store, ha, binding = _setup(make_store, clock)
    pipeline.capture_originals()
    assert store.state.originals == {"curve": 0.75, "room_setpoint": 20.0, "heat_limit": 18.0}
    assert store.state.aux_originals == AUX
    assert load_backup(tmp_path / "backup.json")["aux_originals"] == AUX
    binding.aux = {**AUX, "mode_select": "Normal"}
    pipeline.capture_originals()
    assert store.state.aux_originals == AUX  # nur einmal


def test_server_values_capture_before_the_first_write(make_store, clock):
    pipeline, store, ha, binding = _setup(make_store, clock)
    pipeline.apply_server_values({"curve": 0.8, "room_setpoint": 21.0, "heat_limit": 17.0})
    assert store.state.originals == {"curve": 0.75, "room_setpoint": 20.0, "heat_limit": 18.0}
    assert store.state.aux_originals == AUX
    assert ha.writes == [("number.hk", 0.8), ("number.normal", 21.0), ("number.swu", 17.0)]


def test_unreadable_aux_values_block_every_write_until_they_are_captured(make_store, clock):
    pipeline, store, ha, binding = _setup(make_store, clock)
    binding.aux_error = RuntimeError("select nicht verfuegbar")
    with pytest.raises(DeviceWriteError) as error:
        pipeline.apply_server_values({"curve": 0.8, "room_setpoint": 21.0, "heat_limit": 17.0})
    assert error.value.lever == "room_setpoint"
    assert ha.writes == []  # die Pruefung laeuft vor dem ersten Hebel
    assert store.state.restore_point == {"curve": 0.8, "room_setpoint": 21.0, "heat_limit": 17.0}
    binding.aux_error = None
    pipeline.apply_server_values({"curve": 0.8, "room_setpoint": 21.0, "heat_limit": 17.0})
    assert store.state.aux_originals == AUX
    assert len(ha.writes) == 3


def test_incomplete_aux_values_stay_open(make_store, clock):
    pipeline, store, ha, binding = _setup(make_store, clock)
    binding.aux = {"mode_select": "Automatik"}
    pipeline.capture_originals()
    assert store.state.aux_originals == {}


def test_restore_and_clear_returns_every_lever_and_the_aux_values(make_store, clock):
    pipeline, store, ha, binding = _setup(make_store, clock)
    pipeline.capture_originals()
    pipeline.apply_server_values({"curve": 0.9, "room_setpoint": 22.0, "heat_limit": 16.0})
    assert pipeline.restore_and_clear(always_restore=False) is True
    assert ha.writes[-3:] == [("number.hk", 0.75), ("number.normal", 20.0), ("number.swu", 18.0)]
    assert binding.restored == [AUX]
    assert store.state.restore_point == {"curve": 0.75, "room_setpoint": 20.0, "heat_limit": 18.0}


def test_restore_with_unknown_originals_keeps_the_learned_values_without_crashing(make_store, clock, caplog):
    pipeline, store, ha, binding = _setup(
        make_store, clock, backup={"restore_point": {"curve": 0.9, "room_setpoint": 22.0, "heat_limit": 16.0}},
    )
    assert pipeline.restore_and_clear(always_restore=True) is True
    assert ha.writes == [("number.hk", 0.9), ("number.normal", 22.0), ("number.swu", 16.0)]
    assert binding.restored == []
    assert "Ursprungswert von curve unbekannt" in caplog.text


def test_restore_runs_even_when_the_aux_values_cannot_be_read(make_store, clock):
    pipeline, store, ha, binding = _setup(
        make_store, clock,
        backup={"restore_point": {"curve": 0.9, "room_setpoint": 22.0, "heat_limit": 16.0},
                "originals": {"curve": 0.75, "room_setpoint": 20.0, "heat_limit": 18.0}, "boost_active": True},
    )
    ha.states.update({"number.hk": 1.0, "number.normal": 25.0, "number.swu": 23.0})  # Boost-Werte
    binding.aux_error = RuntimeError("weg")
    assert pipeline.restore_and_clear(always_restore=False) is True
    assert ha.writes == [("number.hk", 0.75), ("number.normal", 20.0), ("number.swu", 18.0)]
    assert store.state.boost_active is False


def test_failing_aux_restore_is_retried_on_the_return_staircase(make_store, clock):
    pipeline, store, ha, binding = _setup(make_store, clock, backup={"aux_originals": AUX})

    def _fail(values, levers=None):
        raise RuntimeError("Modbus weg")

    binding.restore_aux = _fail
    assert pipeline.restore_and_clear(always_restore=False) is False
    assert store.state.write_budget["restore"]["count"] == 1


def test_aux_originals_loaded_from_backup_are_restored(make_store, clock):
    pipeline, store, ha, binding = _setup(make_store, clock, backup={"aux_originals": AUX})
    assert pipeline.restore_and_clear(always_restore=False) is True
    assert binding.restored == [AUX]


def test_a_successful_restore_clears_the_aux_values_and_is_not_repeated(make_store, clock, tmp_path):
    pipeline, store, ha, binding = _setup(make_store, clock)
    pipeline.capture_originals()
    pipeline.apply_server_values({"curve": 0.9, "room_setpoint": 22.0, "heat_limit": 16.0})
    assert pipeline.restore_and_clear(always_restore=False) is True
    assert store.state.aux_originals == {}
    assert "aux_originals" not in load_backup(tmp_path / "backup.json")
    writes = list(ha.writes)
    binding.aux = {**AUX, "mode_select": "Komfort"}  # danach vom Kunden selbst verstellt
    assert pipeline.restore_and_clear(always_restore=False) is True  # naechster Start: keine Schreibvorgaenge
    assert ha.writes == writes
    assert binding.restored == [AUX]
    pipeline.capture_originals()  # erneutes Abo: Hilfswerte werden neu gemerkt
    assert store.state.aux_originals == {**AUX, "mode_select": "Komfort"}


# --- Spec Stufe 3 S3 5.2 (5b-W-4): Ursprungswerte des Servers ---------------------------------------------------------

def test_server_originals_win_for_levers(make_store, clock):
    """5b-W-4: nach einem Ersetzen hat das neue Geraet SmartHeat-Werte als Ursprung gemerkt; der erste Wert der Anlage
    liegt auf dem Server und gilt."""
    pipeline, store, ha, binding = _setup(
        make_store, clock,
        backup={"originals": {"curve": 0.9, "room_setpoint": 22.0, "heat_limit": 18.0},
                "restore_point": {"curve": 0.9, "room_setpoint": 22.0, "heat_limit": 18.0}},
        server={"curve": 0.6, "room_setpoint": 20.0, "heat_limit": 16.0})
    assert pipeline.restore_and_clear(always_restore=True)
    assert ha.states["number.hk"] == 0.6 and ha.states["number.normal"] == 20.0 and ha.states["number.swu"] == 16.0
    assert store.state.restore_point == {"curve": 0.6, "room_setpoint": 20.0, "heat_limit": 16.0}


@pytest.mark.parametrize("unsinn", ["0.6", math.nan, math.inf, True, None, [1], 10**400, -(10**400)])
def test_unusable_server_lever_value_falls_back_to_the_local_original(make_store, clock, unsinn):
    pipeline, store, ha, binding = _setup(
        make_store, clock, backup={"originals": {"curve": 0.7}, "restore_point": {"curve": 0.9}},
        server={"curve": unsinn})
    assert pipeline.restore_and_clear(always_restore=True)
    assert ha.states["number.hk"] == 0.7


def test_server_lever_value_outside_the_limits_is_clamped(make_store, clock):
    pipeline, store, ha, binding = _setup(make_store, clock, backup={"restore_point": {"curve": 0.9}},
                                          server={"curve": 1e9})
    assert pipeline.restore_and_clear(always_restore=True)
    assert ha.states["number.hk"] == 1.00  # weishaupt_wwp x Heizkoerper: Maximum 1,00


def test_server_aux_values_only_without_local_and_only_checked(make_store, clock):
    pipeline, store, ha, binding = _setup(
        make_store, clock, backup={"restore_point": {"curve": 0.9}},
        server={"mode_select": "hz_operationmode_comfort", "setpoint_comfort": 30.0, "setpoint_setback": 18.0})
    assert pipeline.restore_and_clear(always_restore=True)
    # 30,0 liegt ueber dem lokalen Bereich des Raum-Solls (25) und wird verworfen
    assert binding.restored[-1] == {"mode_select": "hz_operationmode_comfort", "setpoint_setback": 18.0}


def test_local_aux_values_win_over_the_server(make_store, clock):
    pipeline, store, ha, binding = _setup(
        make_store, clock,
        backup={"restore_point": {"curve": 0.9}, "aux_originals": {"mode_select": "hz_operationmode_automatic"}},
        server={"mode_select": "hz_operationmode_comfort"})
    assert pipeline.restore_and_clear(always_restore=True)
    assert binding.restored[-1]["mode_select"] == "hz_operationmode_automatic"


@pytest.mark.parametrize("unsinn", ["boese; rm -rf", True, math.nan, math.inf, None, [1], 14.9, 10**400, -(10**400)])
def test_unusable_server_aux_values_are_dropped(make_store, clock, unsinn):
    pipeline, store, ha, binding = _setup(
        make_store, clock, backup={"restore_point": {"curve": 0.9}},
        server={"mode_select": unsinn, "setpoint_comfort": unsinn})
    assert pipeline.restore_and_clear(always_restore=True)
    assert binding.restored == []


def test_a_failing_or_malformed_server_snapshot_falls_back_to_the_local_values(make_store, clock, caplog):
    def kaputt():
        raise RuntimeError("Konfiguration weg")

    pipeline, store, ha, binding = _setup(make_store, clock,
                                          backup={"originals": {"curve": 0.7}, "restore_point": {"curve": 0.9}})
    pipeline._server_originals = kaputt
    assert pipeline.restore_and_clear(always_restore=True)
    assert ha.states["number.hk"] == 0.7
    assert "Ursprungswerte des Servers nicht lesbar" in caplog.text
    ha.states["number.hk"] = 0.9
    pipeline._server_originals = lambda: ["kein", "Mapping"]
    assert pipeline.restore_and_clear(always_restore=True)
    assert ha.states["number.hk"] == 0.7


def test_server_values_are_not_used_without_the_callback(make_store, clock):
    pipeline, store, ha, binding = _setup(make_store, clock,
                                          backup={"originals": {"curve": 0.7}, "restore_point": {"curve": 0.9}})
    assert pipeline.restore_and_clear(always_restore=True)
    assert ha.states["number.hk"] == 0.7


def test_a_huge_server_integer_does_not_break_the_restore(make_store, clock):
    """Bedrohungsmodell: ein Server (oder Angreifer) liefert 10**400; math.isfinite wirft dort OverflowError."""
    pipeline, store, ha, binding = _setup(
        make_store, clock, backup={"originals": {"curve": 0.7}, "restore_point": {"curve": 0.9}},
        server={"curve": 10**400, "setpoint_comfort": 10**400, "setpoint_setback": -(10**400)})
    assert pipeline.restore_and_clear(always_restore=True)
    assert ha.states["number.hk"] == 0.7
    assert binding.restored == []


@pytest.mark.parametrize(("name", "wert"), [
    ("mode_select", 20.0),  # Zahl fuer eine Text-Rolle
    ("setpoint_comfort", "hz_operationmode_comfort"),  # Text fuer eine Sollwert-Rolle
    ("setpoint_setback", "abc"),
])
def test_server_aux_value_of_the_wrong_kind_is_dropped_and_logged_by_name(make_store, clock, caplog, name, wert):
    pipeline, store, ha, binding = _setup(make_store, clock, backup={"restore_point": {"curve": 0.9}},
                                          server={name: wert})
    assert pipeline.restore_and_clear(always_restore=True)
    assert binding.restored == []
    assert f"Hilfs-Ursprungswert {name} vom Server verworfen" in caplog.text
    assert str(wert) not in caplog.text


SERVER_ALL = {"curve": 0.6, "room_setpoint": 20.0, "heat_limit": 16.0,
              "mode_select": "hz_operationmode_comfort", "setpoint_comfort": 21.0, "setpoint_setback": 17.0}


def test_server_values_do_not_retrigger_a_successful_restore(make_store, clock):
    """Schluss-Review: die gespiegelten Ursprungswerte bleiben in der Konfiguration. Ob zurueckgesetzt wird, entscheidet
    nur der lokale Zustand; die Werte des Servers gelten nur, wenn ohnehin geschrieben wird (naechster Start nach dem
    Abo-Ende oder Abmelden: nichts mehr schreiben)."""
    pipeline, store, ha, binding = _setup(
        make_store, clock, backup={"restore_point": {"curve": 0.9, "room_setpoint": 22.0, "heat_limit": 16.0}},
        server=SERVER_ALL)
    assert pipeline.restore_and_clear(always_restore=False) is True
    assert ha.writes == [("number.hk", 0.6), ("number.swu", 16.0)]  # room_setpoint steht schon auf 20,0
    assert binding.restored == [{"mode_select": "hz_operationmode_comfort", "setpoint_comfort": 21.0,
                                 "setpoint_setback": 17.0}]
    ha.states["number.hk"] = 0.8  # danach vom Kunden selbst verstellt
    binding.aux = {**AUX, "mode_select": "Komfort"}
    assert pipeline.restore_and_clear(always_restore=False) is True
    assert ha.writes == [("number.hk", 0.6), ("number.swu", 16.0)]
    assert len(binding.restored) == 1


def test_server_values_do_not_retrigger_a_restore_of_local_aux_values(make_store, clock):
    """Wie oben mit lokal gemerkten Hilfswerten (die gewinnen); nach dem Leeren duerfen die Serverwerte das
    Zuruecksetzen nicht neu ausloesen."""
    pipeline, store, ha, binding = _setup(make_store, clock, server=SERVER_ALL)
    pipeline.capture_originals()
    pipeline.apply_server_values({"curve": 0.9, "room_setpoint": 22.0, "heat_limit": 16.0})
    assert pipeline.restore_and_clear(always_restore=False) is True
    assert (ha.states["number.hk"], ha.states["number.normal"], ha.states["number.swu"]) == (0.6, 20.0, 16.0)
    assert binding.restored == [AUX]
    writes = list(ha.writes)
    ha.states["number.hk"] = 0.8
    assert pipeline.restore_and_clear(always_restore=False) is True
    assert ha.writes == writes
    assert binding.restored == [AUX]


def test_server_lever_values_do_not_retrigger_a_successful_restore(make_store, clock):
    """Hebelseite: nach dem Erfolg steht der Wiederherstellungspunkt auf dem Ursprungswert des Servers; ein vom Kunden
    verstellter Hebel loest beim naechsten Aufruf nichts aus."""
    pipeline, store, ha, binding = _setup(
        make_store, clock, backup={"restore_point": {"curve": 0.9, "room_setpoint": 22.0, "heat_limit": 18.0}},
        server={"curve": 0.6, "room_setpoint": 20.0, "heat_limit": 16.0})
    assert pipeline.restore_and_clear(always_restore=False) is True
    assert store.state.restore_point == {"curve": 0.6, "room_setpoint": 20.0, "heat_limit": 16.0}
    writes = list(ha.writes)
    ha.states.update({"number.hk": 0.8, "number.swu": 19.0})
    assert pipeline.restore_and_clear(always_restore=False) is True
    assert ha.writes == writes
    assert binding.restored == []
