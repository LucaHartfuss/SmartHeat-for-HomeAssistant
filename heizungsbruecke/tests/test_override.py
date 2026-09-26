"""Sollwert-Regel und Schreiben auf die Anlage (override.py)."""
import logging

import pytest

from heizungsbruecke.backup_store import load_backup
from heizungsbruecke.manifest import ChannelManifest
from heizungsbruecke.override import DeviceWriteError, Override

OPTIONS = {
    "curve_min": 0.4, "curve_max": 1.5, "offset_min": 20.0, "offset_max": 30.0,
    "boost_curve_value": 1.0, "boost_offset_value": 25.0,
}
MANIFEST = ChannelManifest(entity_ids={"curve_current": "number.curve", "offset_current": "number.offset"})
RESTORE_POINT = {"curve_current": 0.9, "offset_current": 22.0}
VALUES = {"restore": (0.9, 22.0), "comfort": (1.0, 25.0), "emergency": (1.5, 30.0)}
# Sollwert-Regel der Spec: (comfort, emergency) -> Zeile
ROW = {(False, False): "restore", (True, False): "comfort", (False, True): "emergency", (True, True): "emergency"}


class RecordingHa:
    def __init__(self, states=None):
        self.states = {"number.curve": 0.7, "number.offset": 21.0, **(states or {})}
        self.events = []
        self.write_error = None

    def get_state(self, entity_id):
        self.events.append(("read", entity_id))
        value = self.states[entity_id]
        if isinstance(value, Exception):
            raise value
        return value

    def set_number_value(self, entity_id, value):
        if self.write_error is not None:
            raise self.write_error
        self.events.append(("write", entity_id, value))

    @property
    def writes(self):
        return [(event[1], event[2]) for event in self.events if event[0] == "write"]

    @property
    def reads(self):
        return [event[1] for event in self.events if event[0] == "read"]


def _setup(make_store, backup=None, states=None, manifest=MANIFEST, **options):
    store = make_store(backup=backup)
    ha = RecordingHa(states)
    return Override(store, manifest, ha, {**OPTIONS, **options}), store, ha


def _written(row):
    curve, offset = VALUES[row]
    return [("number.curve", curve), ("number.offset", offset)]


def _raise_oserror(*args, **kwargs):
    raise OSError("SD-Karte kaputt")


# --- set_boosts ---

@pytest.mark.parametrize("before", list(ROW))
@pytest.mark.parametrize("after", list(ROW))
def test_set_boosts_writes_the_target_row_only_when_it_changes(make_store, before, after):
    override, store, ha = _setup(
        make_store, backup={**RESTORE_POINT, "boost_active": before[0], "emergency_boost_active": before[1]},
    )

    assert override.set_boosts(comfort=after[0], emergency=after[1]) == after

    assert (store.state.boost_active, store.state.emergency_boost_active) == after
    assert ha.writes == ([] if ROW[before] == ROW[after] else _written(ROW[after]))
    assert ha.reads == []


def test_set_boosts_persists_flags(make_store, tmp_path):
    override, _, _ = _setup(make_store, backup=dict(RESTORE_POINT))

    override.set_boosts(comfort=True, emergency=False)

    assert load_backup(tmp_path / "backup.json")["boost_active"] is True


@pytest.mark.parametrize("comfort,emergency", [(True, False), (False, True)])
def test_starting_boost_saves_restore_point_from_device_before_first_write(make_store, tmp_path, comfort, emergency):
    override, store, ha = _setup(make_store)

    override.set_boosts(comfort=comfort, emergency=emergency)

    assert ha.events[:2] == [("read", "number.curve"), ("read", "number.offset")]
    assert [event[0] for event in ha.events[2:]] == ["write", "write"]
    assert (store.state.curve_current, store.state.offset_current) == (0.7, 21.0)
    backup = load_backup(tmp_path / "backup.json")
    assert (backup["curve_current"], backup["offset_current"]) == (0.7, 21.0)


def test_starting_boost_reads_only_the_missing_role(make_store):
    override, store, ha = _setup(make_store, backup={"curve_current": 0.9})

    override.set_boosts(comfort=True, emergency=False)

    assert ha.reads == ["number.offset"]
    assert (store.state.curve_current, store.state.offset_current) == (0.9, 21.0)


@pytest.mark.parametrize("curve_state", [RuntimeError("Cloud nicht erreichbar"), float("nan")])
@pytest.mark.parametrize("comfort,emergency,message", [
    (True, False, "Boost ausgesetzt"), (False, True, "Notfall-Boost ausgesetzt"),
])
def test_starting_boost_without_restore_point_is_refused(make_store, caplog, curve_state, comfort, emergency, message):
    override, store, ha = _setup(make_store, states={"number.curve": curve_state})

    with caplog.at_level(logging.WARNING):
        assert override.set_boosts(comfort=comfort, emergency=emergency) == (False, False)

    assert ha.writes == []
    assert (store.state.boost_active, store.state.emergency_boost_active) == (False, False)
    assert store.state.curve_current is None
    assert message in caplog.text


def test_running_boost_is_never_refused(make_store):
    # Jeder Boost-Start hat den Wiederherstellungspunkt gesichert. Fehlt er trotzdem (kaputte
    # Datei), wird der Wechsel Notfall -> Comfort nicht abgelehnt und nichts gelesen.
    override, _, ha = _setup(
        make_store, backup={"boost_active": True, "emergency_boost_active": True},
        states={"number.curve": RuntimeError("Cloud nicht erreichbar")},
    )

    assert override.set_boosts(comfort=True, emergency=False) == (True, False)
    assert ha.writes == _written("comfort")
    assert ha.reads == []


def test_restore_point_not_yet_on_the_card_blocks_a_new_boost(make_store, tmp_path, monkeypatch, caplog):
    # M1: der von der Anlage gelesene Wiederherstellungspunkt steht nur im Speicher, weil
    # backup.json nicht geschrieben werden konnte. Der naechste Check darf den Boost dann
    # nicht schreiben, sondern holt zuerst das Speichern nach.
    override, store, ha = _setup(make_store)
    monkeypatch.setattr("heizungsbruecke.backup_store.save_backup", _raise_oserror)

    with pytest.raises(OSError):
        override.set_boosts(comfort=True, emergency=False)  # Check 1: gelesen, Speichern scheitert
    assert ha.writes == []

    with caplog.at_level(logging.WARNING), pytest.raises(OSError):
        override.set_boosts(comfort=True, emergency=False)  # Check 2: Karte weiter kaputt

    assert ha.writes == []
    assert store.state.boost_active is False
    assert "Boost ausgesetzt" in caplog.text
    assert not (tmp_path / "backup.json").exists()

    monkeypatch.undo()
    assert override.set_boosts(comfort=True, emergency=False) == (True, False)  # Karte wieder ok

    assert ha.reads == ["number.curve", "number.offset"]  # nur beim ersten Check gelesen
    assert ha.writes == _written("comfort")
    backup = load_backup(tmp_path / "backup.json")
    assert (backup["curve_current"], backup["offset_current"], backup["boost_active"]) == (0.7, 21.0, True)


def test_unsaved_boost_flag_alone_does_not_block_a_new_boost(make_store, monkeypatch):
    # Steht der Wiederherstellungspunkt auf der Karte, blockiert ein nur im Speicher
    # stehendes Boost-Flag (Karte kaputt) keinen neuen Boost.
    override, _, ha = _setup(make_store, backup=dict(RESTORE_POINT))
    monkeypatch.setattr("heizungsbruecke.backup_store.save_backup", _raise_oserror)
    for comfort in (True, False):
        with pytest.raises(OSError):
            override.set_boosts(comfort=comfort, emergency=False)

    with pytest.raises(OSError):
        override.set_boosts(comfort=True, emergency=False)

    assert ha.writes == _written("comfort") + _written("restore") + _written("comfort")
    assert ha.reads == []


@pytest.mark.parametrize("before", [(True, False), (False, True)])
def test_second_boost_never_reads_the_device_for_a_restore_point(make_store, before):
    # M2: laeuft schon ein Boost, steht die Anlage auf Boost-Werten, nicht auf gelernten.
    # Fehlt der Wiederherstellungspunkt, wird er dann nicht gelesen; der zweite Boost startet
    # trotzdem, und das Boost-Ende schreibt nur bekannte Werte.
    override, store, ha = _setup(
        make_store, backup={"boost_active": before[0], "emergency_boost_active": before[1]},
        states={"number.curve": 1.0, "number.offset": 25.0},
    )

    assert override.set_boosts(comfort=True, emergency=True) == (True, True)

    assert ha.reads == []
    assert ha.writes == ([] if before == (False, True) else _written("emergency"))
    assert (store.state.curve_current, store.state.offset_current) == (None, None)


def test_write_failure_leaves_flags_unchanged(make_store, tmp_path):
    override, store, ha = _setup(make_store, backup=dict(RESTORE_POINT))
    ha.write_error = RuntimeError("myVAILLANT-Cloud nicht erreichbar")

    with pytest.raises(DeviceWriteError, match=r"^curve_current \(number\.curve\): myVAILLANT-Cloud nicht erreichbar$"):
        override.set_boosts(comfort=True, emergency=False)

    assert store.state.boost_active is False
    assert load_backup(tmp_path / "backup.json").get("boost_active", False) is False


def test_flag_save_failure_after_device_write_keeps_memory_consistent(make_store, tmp_path, monkeypatch):
    # Review Focus 1: SD-Karte schreibt nicht, nachdem die Anlage schon auf Boost-Werten steht.
    override, store, ha = _setup(make_store, backup=dict(RESTORE_POINT))
    monkeypatch.setattr("heizungsbruecke.backup_store.save_backup", _raise_oserror)

    with pytest.raises(OSError):
        override.set_boosts(comfort=True, emergency=False)

    assert ha.writes == _written("comfort")
    assert store.state.boost_active is True

    monkeypatch.undo()
    override.set_boosts(comfort=True, emergency=False)  # naechster Check

    assert ha.writes == _written("comfort")  # Anlage nicht erneut beschrieben
    assert load_backup(tmp_path / "backup.json")["boost_active"] is True


def test_restore_point_outside_clamps_is_clamped(make_store):
    override, _, ha = _setup(make_store, backup={"curve_current": 2.0, "offset_current": 10.0, "boost_active": True})

    override.set_boosts(comfort=False, emergency=False)

    assert ha.writes == [("number.curve", 1.5), ("number.offset", 20.0)]


def test_boost_values_outside_clamps_are_clamped(make_store):
    override, _, ha = _setup(make_store, backup=dict(RESTORE_POINT), boost_curve_value=9.9, boost_offset_value=0.0)

    override.set_boosts(comfort=True, emergency=False)

    assert ha.writes == [("number.curve", 1.5), ("number.offset", 20.0)]


def test_restore_writes_only_known_values(make_store):
    override, _, ha = _setup(make_store, backup={"curve_current": 0.9, "boost_active": True})

    override.set_boosts(comfort=False, emergency=False)

    assert ha.writes == [("number.curve", 0.9)]


def test_unmapped_roles_are_neither_read_nor_written(make_store):
    override, _, ha = _setup(make_store, manifest=ChannelManifest(entity_ids={"curve_current": "number.curve"}))

    override.set_boosts(comfort=True, emergency=False)

    assert ha.reads == ["number.curve"]
    assert ha.writes == [("number.curve", 1.0)]


# --- apply_server_values ---

def test_server_values_are_clamped_stored_and_written(make_store, tmp_path, caplog):
    override, store, ha = _setup(make_store)

    with caplog.at_level(logging.WARNING):
        override.apply_server_values(9.0, 23.0)

    assert ha.writes == [("number.curve", 1.5), ("number.offset", 23.0)]
    assert (store.state.curve_current, store.state.offset_current) == (1.5, 23.0)
    backup = load_backup(tmp_path / "backup.json")
    assert (backup["curve_current"], backup["offset_current"]) == (1.5, 23.0)
    assert "geclampt" in caplog.text


@pytest.mark.parametrize("flags", [{"boost_active": True}, {"emergency_boost_active": True}])
def test_server_values_during_boost_are_only_stored(make_store, flags):
    # Review Focus 3: kein Schreibversuch, also auch kein Fehler bei gestoerter Anlage.
    override, store, ha = _setup(make_store, backup={**RESTORE_POINT, **flags})
    ha.write_error = RuntimeError("myVAILLANT-Cloud nicht erreichbar")

    override.apply_server_values(0.95, 40.0)

    assert ha.writes == []
    assert (store.state.curve_current, store.state.offset_current) == (0.95, 30.0)


def test_server_values_are_stored_before_a_failing_write(make_store):
    override, store, ha = _setup(make_store)
    ha.write_error = RuntimeError("x" * 500)

    with pytest.raises(DeviceWriteError) as error:
        override.apply_server_values(0.95, 23.0)

    assert (store.state.curve_current, store.state.offset_current) == (0.95, 23.0)
    assert (error.value.role, error.value.entity_id) == ("curve_current", "number.curve")
    assert str(error.value) == "curve_current (number.curve): " + "x" * 200


def test_nan_server_value_is_rejected_without_storing(make_store):
    override, store, ha = _setup(make_store, backup=dict(RESTORE_POINT))

    with pytest.raises(ValueError):
        override.apply_server_values(float("nan"), 23.0)

    assert store.state.curve_current == 0.9
    assert ha.writes == []


# --- restore_and_clear ---

def test_restore_and_clear_forced_without_boost_writes_restore_point(make_store):
    override, _, ha = _setup(make_store, backup=dict(RESTORE_POINT))

    assert override.restore_and_clear(always_restore=True) is True
    assert ha.writes == _written("restore")


def test_restore_and_clear_not_forced_without_boost_writes_nothing(make_store):
    override, _, ha = _setup(make_store, backup=dict(RESTORE_POINT))

    assert override.restore_and_clear(always_restore=False) is True
    assert ha.writes == []


@pytest.mark.parametrize("flags", [{"boost_active": True}, {"emergency_boost_active": True}])
def test_restore_and_clear_ends_boosts(make_store, tmp_path, flags):
    override, _, ha = _setup(make_store, backup={**RESTORE_POINT, **flags})

    assert override.restore_and_clear(always_restore=False) is True

    assert ha.writes == _written("restore")
    backup = load_backup(tmp_path / "backup.json")
    assert (backup["boost_active"], backup["emergency_boost_active"]) == (False, False)


def test_restore_and_clear_keeps_flags_when_write_fails(make_store):
    override, store, ha = _setup(make_store, backup={**RESTORE_POINT, "emergency_boost_active": True})
    ha.write_error = RuntimeError("HA nicht erreichbar")

    assert override.restore_and_clear(always_restore=True) is False
    assert store.state.emergency_boost_active is True


def test_restore_and_clear_counts_as_done_when_only_saving_flags_fails(make_store, monkeypatch, caplog):
    override, store, ha = _setup(make_store, backup={**RESTORE_POINT, "emergency_boost_active": True})
    monkeypatch.setattr("heizungsbruecke.backup_store.save_backup", _raise_oserror)

    with caplog.at_level(logging.ERROR):
        assert override.restore_and_clear(always_restore=True) is True

    assert ha.writes == _written("restore")
    assert store.state.emergency_boost_active is False
    assert "SD-Karte kaputt" in caplog.text
