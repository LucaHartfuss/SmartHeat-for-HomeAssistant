"""Sollwert-Regel und Schreiben auf die Anlage (override.py)."""
import logging

import pytest

from heizungsbruecke.backup_store import load_backup
from heizungsbruecke.manifest import ChannelManifest
from heizungsbruecke.override import OWN_WRITE_SETTLE_SECONDS, DeviceWriteError, Override

OPTIONS = {
    "curve_min": 0.4, "curve_max": 1.5, "shift_min": 15.0, "shift_max": 25.0,
    "min_flow_min": 20.0, "min_flow_max": 30.0, "boost_curve_value": 1.0, "boost_shift_value": 24.0,
}
MANIFEST = ChannelManifest(entity_ids={
    "curve_current": "number.curve", "shift_current": "number.shift", "min_flow": "number.min_flow",
})
RESTORE_POINT = {"curve_current": 0.9, "shift_current": 22.0}
# RecordingHa.states-Form von RESTORE_POINT, fuer Tests der mypyllant-Verzoegerung (HA kann nach
# einem eigenen Schreibvorgang bis zum naechsten Poll, bis ~30 min, noch den vorherigen Wert zeigen).
RESTORE_POINT_STATES = {"number.curve": 0.9, "number.shift": 22.0}
VALUES = {"restore": (0.9, 22.0), "comfort": (1.0, 24.0), "emergency": (1.5, 25.0)}
# Sollwert-Regel der Spec: (comfort, emergency) -> Zeile
ROW = {(False, False): "restore", (True, False): "comfort", (False, True): "emergency", (True, True): "emergency"}


class RecordingHa:
    def __init__(self, states=None):
        self.states = {"number.curve": 0.7, "number.shift": 21.0, "number.min_flow": 20.0, **(states or {})}
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


def _setup(make_store, backup=None, states=None, manifest=MANIFEST, clock=None, **options):
    store = make_store(backup=backup)
    ha = RecordingHa(states)
    clock_kwargs = {"clock": clock} if clock is not None else {}
    return Override(store, manifest, ha, {**OPTIONS, **options}, **clock_kwargs), store, ha


def _written(row):
    curve, shift = VALUES[row]
    return [("number.curve", curve), ("number.shift", shift)]


def _raise_oserror(*args, **kwargs):
    raise OSError("Datentraeger kaputt")


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
    # Reihenwechsel loest je geschriebener Rolle einen Quota-Check-Read voraus (_write_role).
    assert ha.reads == ([] if ROW[before] == ROW[after] else ["number.curve", "number.shift"])


def test_set_boosts_persists_flags(make_store, tmp_path):
    override, _, _ = _setup(make_store, backup=dict(RESTORE_POINT))

    override.set_boosts(comfort=True, emergency=False)

    assert load_backup(tmp_path / "backup.json")["boost_active"] is True


@pytest.mark.parametrize("comfort,emergency", [(True, False), (False, True)])
def test_starting_boost_saves_restore_point_from_device_before_first_write(make_store, tmp_path, comfort, emergency):
    override, store, ha = _setup(make_store)

    override.set_boosts(comfort=comfort, emergency=emergency)

    assert ha.events[:2] == [("read", "number.curve"), ("read", "number.shift")]
    # Ab hier je Rolle ein Quota-Check-Read direkt vor ihrem Schreiben (_write_role).
    assert [event[0] for event in ha.events[2:]] == ["read", "write", "read", "write"]
    assert (store.state.curve_current, store.state.shift_current) == (0.7, 21.0)
    backup = load_backup(tmp_path / "backup.json")
    assert (backup["curve_current"], backup["shift_current"]) == (0.7, 21.0)


def test_starting_boost_reads_only_the_missing_role(make_store):
    override, store, ha = _setup(make_store, backup={"curve_current": 0.9})

    override.set_boosts(comfort=True, emergency=False)

    # Wiederherstellungspunkt: nur die fehlende Rolle. Danach je geschriebener Rolle noch ein
    # Quota-Check-Read (_write_role).
    assert ha.reads == ["number.shift", "number.curve", "number.shift"]
    assert (store.state.curve_current, store.state.shift_current) == (0.9, 21.0)


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
    # Kein Wiederherstellungspunkt-Read (Boost lief schon); nur je Rolle ein Quota-Check-Read
    # (_write_role) vor dem Schreiben -- der fuer curve_current scheitert (RuntimeError), wird
    # aber trotzdem als Read gezaehlt, bevor _write_role trotzdem schreibt.
    assert ha.reads == ["number.curve", "number.shift"]


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
        override.set_boosts(comfort=True, emergency=False)  # Check 2: Datentraeger weiter kaputt

    assert ha.writes == []
    assert store.state.boost_active is False
    assert "Boost ausgesetzt" in caplog.text
    assert not (tmp_path / "backup.json").exists()

    monkeypatch.undo()
    assert override.set_boosts(comfort=True, emergency=False) == (True, False)  # Datentraeger wieder ok

    # Wiederherstellungspunkt nur beim ersten Check gelesen, danach je Rolle ein Quota-Check-Read
    # vor dem Schreiben (_write_role, nur der dritte Check schreibt tatsaechlich).
    assert ha.reads == ["number.curve", "number.shift", "number.curve", "number.shift"]
    assert ha.writes == _written("comfort")
    backup = load_backup(tmp_path / "backup.json")
    assert (backup["curve_current"], backup["shift_current"], backup["boost_active"]) == (0.7, 21.0, True)


def test_unsaved_boost_flag_alone_does_not_block_a_new_boost(make_store, monkeypatch):
    # Steht der Wiederherstellungspunkt auf dem Datentraeger, blockiert ein nur im Speicher
    # stehendes Boost-Flag (Datentraeger kaputt) keinen neuen Boost.
    override, _, ha = _setup(make_store, backup=dict(RESTORE_POINT))
    monkeypatch.setattr("heizungsbruecke.backup_store.save_backup", _raise_oserror)
    for comfort in (True, False):
        with pytest.raises(OSError):
            override.set_boosts(comfort=comfort, emergency=False)

    with pytest.raises(OSError):
        override.set_boosts(comfort=True, emergency=False)

    assert ha.writes == _written("comfort") + _written("restore") + _written("comfort")
    # Wiederherstellungspunkt liegt schon vollstaendig im Backup -- keine Restore-Point-Reads,
    # aber je geschriebener Rolle ein Quota-Check-Read (_write_role), 3x (curve, shift).
    assert ha.reads == ["number.curve", "number.shift"] * 3


@pytest.mark.parametrize("before", [(True, False), (False, True)])
def test_second_boost_never_reads_the_device_for_a_restore_point(make_store, before):
    # M2: laeuft schon ein Boost, steht die Anlage auf Boost-Werten, nicht auf gelernten.
    # Fehlt der Wiederherstellungspunkt, wird er dann nicht gelesen; der zweite Boost startet
    # trotzdem, und das Boost-Ende schreibt nur bekannte Werte.
    override, store, ha = _setup(
        make_store, backup={"boost_active": before[0], "emergency_boost_active": before[1]},
        states={"number.curve": 1.0, "number.shift": 20.0},
    )

    assert override.set_boosts(comfort=True, emergency=True) == (True, True)

    # before=(False, True): Zeile bleibt "emergency" -> kein Schreiben, also auch kein
    # Quota-Check-Read; before=(True, False): Zeile wechselt, je Rolle ein Quota-Check-Read
    # (_write_role) -- kein Wiederherstellungspunkt-Read (Boost lief schon).
    assert ha.reads == ([] if before == (False, True) else ["number.curve", "number.shift"])
    assert ha.writes == ([] if before == (False, True) else _written("emergency"))
    assert (store.state.curve_current, store.state.shift_current) == (None, None)


def test_write_failure_leaves_flags_unchanged(make_store, tmp_path):
    override, store, ha = _setup(make_store, backup=dict(RESTORE_POINT))
    ha.write_error = RuntimeError("myVAILLANT-Cloud nicht erreichbar")

    with pytest.raises(DeviceWriteError, match=r"^curve_current \(number\.curve\): myVAILLANT-Cloud nicht erreichbar$"):
        override.set_boosts(comfort=True, emergency=False)

    assert store.state.boost_active is False
    assert load_backup(tmp_path / "backup.json").get("boost_active", False) is False


def test_flag_save_failure_after_device_write_keeps_memory_consistent(make_store, tmp_path, monkeypatch):
    # Review Focus 1: Datentraeger schreibt nicht, nachdem die Anlage schon auf Boost-Werten steht.
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
    override, _, ha = _setup(make_store, backup={"curve_current": 2.0, "shift_current": 10.0, "boost_active": True})

    override.set_boosts(comfort=False, emergency=False)

    assert ha.writes == [("number.curve", 1.5), ("number.shift", 15.0)]


def test_boost_values_outside_clamps_are_clamped(make_store):
    override, _, ha = _setup(make_store, backup=dict(RESTORE_POINT), boost_curve_value=9.9, boost_shift_value=0.0)

    override.set_boosts(comfort=True, emergency=False)

    assert ha.writes == [("number.curve", 1.5), ("number.shift", 15.0)]


def test_restore_writes_only_known_values(make_store):
    override, _, ha = _setup(make_store, backup={"curve_current": 0.9, "boost_active": True})

    override.set_boosts(comfort=False, emergency=False)

    assert ha.writes == [("number.curve", 0.9)]


def test_unmapped_roles_are_neither_read_nor_written(make_store):
    override, _, ha = _setup(make_store, manifest=ChannelManifest(entity_ids={"curve_current": "number.curve"}))

    override.set_boosts(comfort=True, emergency=False)

    # Wiederherstellungspunkt-Read plus ein Quota-Check-Read (_write_role), beide nur curve_current.
    assert ha.reads == ["number.curve", "number.curve"]
    assert ha.writes == [("number.curve", 1.0)]


# --- apply_server_values ---

def test_server_values_are_clamped_stored_and_written(make_store, tmp_path, caplog):
    override, store, ha = _setup(make_store)

    with caplog.at_level(logging.WARNING):
        override.apply_server_values(9.0, 23.0)

    assert ha.writes == [("number.curve", 1.5), ("number.shift", 23.0)]
    assert (store.state.curve_current, store.state.shift_current) == (1.5, 23.0)
    backup = load_backup(tmp_path / "backup.json")
    assert (backup["curve_current"], backup["shift_current"]) == (1.5, 23.0)
    assert "geclampt" in caplog.text


@pytest.mark.parametrize("flags", [{"boost_active": True}, {"emergency_boost_active": True}])
def test_server_values_during_boost_are_only_stored(make_store, flags):
    # Review Focus 3: kein Schreibversuch, also auch kein Fehler bei gestoerter Anlage.
    override, store, ha = _setup(make_store, backup={**RESTORE_POINT, **flags})
    ha.write_error = RuntimeError("myVAILLANT-Cloud nicht erreichbar")

    override.apply_server_values(0.95, 40.0)

    assert ha.writes == []
    assert (store.state.curve_current, store.state.shift_current) == (0.95, 25.0)


def test_server_values_are_stored_before_a_failing_write(make_store):
    override, store, ha = _setup(make_store)
    ha.write_error = RuntimeError("x" * 500)

    with pytest.raises(DeviceWriteError) as error:
        override.apply_server_values(0.95, 23.0)

    assert (store.state.curve_current, store.state.shift_current) == (0.95, 23.0)
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
    assert "Datentraeger kaputt" in caplog.text


# --- N6: Notfall-Boost auf dem zuletzt gespeicherten Wiederherstellungspunkt ---

def test_emergency_boost_starts_on_the_older_saved_point_when_the_new_one_cannot_be_saved(
    make_store, monkeypatch, caplog,
):
    # N6: Datentraeger nicht beschreibbar, Serverwerte nur im Speicher -- der Notfall-Boost startet
    # trotzdem und setzt am Ende auf den gespeicherten (aelteren) Punkt zurueck.
    override, store, ha = _setup(make_store, backup=dict(RESTORE_POINT))
    monkeypatch.setattr("heizungsbruecke.backup_store.save_backup", _raise_oserror)
    with pytest.raises(OSError):
        override.apply_server_values(1.0, 25.0)

    with caplog.at_level(logging.WARNING), pytest.raises(OSError):  # Flags nicht speicherbar
        override.set_boosts(comfort=False, emergency=True)

    assert ha.writes == _written("emergency")
    assert store.state.emergency_boost_active is True
    assert (store.state.curve_current, store.state.shift_current) == (0.9, 22.0)
    assert "zuletzt gespeicherten Wiederherstellungspunkt" in caplog.text

    monkeypatch.undo()
    override.set_boosts(comfort=False, emergency=False)

    assert ha.writes[-2:] == _written("restore")


def test_comfort_boost_is_still_refused_when_only_an_older_point_is_saved(make_store, monkeypatch):
    override, store, ha = _setup(make_store, backup=dict(RESTORE_POINT))
    monkeypatch.setattr("heizungsbruecke.backup_store.save_backup", _raise_oserror)
    with pytest.raises(OSError):
        override.apply_server_values(1.0, 25.0)

    with pytest.raises(OSError):
        override.set_boosts(comfort=True, emergency=False)

    assert ha.writes == []
    assert store.state.boost_active is False
    assert (store.state.curve_current, store.state.shift_current) == (1.0, 25.0)


def test_emergency_boost_without_any_saved_point_is_still_refused(make_store, monkeypatch):
    override, store, ha = _setup(make_store)
    monkeypatch.setattr("heizungsbruecke.backup_store.save_backup", _raise_oserror)
    with pytest.raises(OSError):
        override.apply_server_values(1.0, 25.0)

    with pytest.raises(OSError):
        override.set_boosts(comfort=False, emergency=True)

    assert ha.writes == []
    assert store.state.emergency_boost_active is False


# --- expected_values / write_roles / write_min_flow / prepare_zone (Task 11) ---

def test_expected_values_follow_the_row(make_store):
    override, store, _ = _setup(make_store, backup=RESTORE_POINT)
    assert override.expected_values() == {"curve_current": 0.9, "shift_current": 22.0}
    override.set_boosts(comfort=True, emergency=False)
    assert override.expected_values() == {"curve_current": 1.0, "shift_current": 24.0}


def test_write_roles_writes_only_the_given_roles(make_store):
    override, _, ha = _setup(make_store, backup=RESTORE_POINT)
    override.write_roles(("shift_current",))
    assert ha.writes == [("number.shift", 22.0)]


def test_write_min_flow_is_clamped_rounded_and_timed(make_store, clock):
    store = make_store(backup=RESTORE_POINT)
    # number.min_flow bewusst abweichend vom Zielwert (20.0), sonst wuerde der Quota-Check
    # (_write_role) den Schreibvorgang als unveraendert ueberspringen.
    ha = RecordingHa({"number.min_flow": 25.0})
    override = Override(store, MANIFEST, ha, OPTIONS, clock=clock)
    assert override.write_min_flow(19.2) == 20.0
    assert ha.writes == [("number.min_flow", 20.0)]
    clock.advance(60)
    assert override.settled("min_flow") is False
    clock.advance(OWN_WRITE_SETTLE_SECONDS)
    assert override.settled("min_flow") is True


def test_restore_point_with_inactive_zone_refuses_boost(make_store):
    override, _, ha = _setup(make_store, backup={"curve_current": 0.9}, states={"number.shift": 0.0})
    assert override.set_boosts(comfort=True, emergency=False) == (False, False)
    assert ha.writes == []


def test_prepare_zone_writes_start_shift_when_zone_is_off(make_store):
    manifest = ChannelManifest(entity_ids={**MANIFEST.entity_ids, "shift_current": "climate.zone::temperature"})
    store = make_store(backup={"curve_current": 0.9})
    ha = RecordingHa({"climate.zone": "auto", "climate.zone::temperature": 0.0})
    ha.set_hvac_mode = lambda entity, mode: ha.events.append(("hvac", entity, mode))
    ha.get_raw_state = lambda entity: ha.states[entity]
    ha.set_climate_temperature = lambda entity, value: ha.events.append(("write", entity, value))
    Override(store, manifest, ha, OPTIONS).prepare_zone(start_shift=20.6)
    assert ("hvac", "climate.zone", "heat_cool") in ha.events
    assert ("climate.zone", 20.5) in ha.writes
    assert store.state.shift_current == 20.5


def test_prepare_zone_leaves_a_plausible_manual_zone_alone(make_store):
    manifest = ChannelManifest(entity_ids={**MANIFEST.entity_ids, "shift_current": "climate.zone::temperature"})
    store = make_store(backup={"curve_current": 0.9})
    ha = RecordingHa({"climate.zone": "heat_cool", "climate.zone::temperature": 21.0})
    ha.get_raw_state = lambda entity: ha.states[entity]
    Override(store, manifest, ha, OPTIONS).prepare_zone(start_shift=20.5)
    assert ha.writes == []


# --- Quota-Check: nur bei tatsaechlicher Aenderung schreiben (_write_role) ---

def test_write_role_skips_the_device_when_the_current_value_already_matches(make_store, clock):
    # number.shift steht schon (innerhalb eines halben Schritts) auf dem Restore-Zielwert 22.0 --
    # kein Schreiben, kein Zeitstempel, aber curve_current (0.7 != 0.9) wird weiter geschrieben.
    # (Laufzeit > Schonfrist: erst dann gilt ein Read ohne eigenes Schreiben seit dem Start.)
    override, store, ha = _setup(
        make_store, backup={**RESTORE_POINT, "boost_active": True}, states={"number.shift": 22.2}, clock=clock,
    )
    clock.advance(OWN_WRITE_SETTLE_SECONDS + 1)

    override.set_boosts(comfort=False, emergency=False)

    assert ha.writes == [("number.curve", 0.9)]
    assert override.settled("shift_current") is True  # uebersprungen: kein Zeitstempel


def test_write_role_writes_when_the_current_value_differs(make_store):
    override, _, ha = _setup(
        make_store, backup={**RESTORE_POINT, "boost_active": True}, states={"number.shift": 10.0},
    )

    override.set_boosts(comfort=False, emergency=False)

    assert ha.writes == [("number.curve", 0.9), ("number.shift", 22.0)]


def test_write_role_writes_when_the_current_value_cannot_be_read(make_store):
    override, _, ha = _setup(
        make_store, backup={**RESTORE_POINT, "boost_active": True},
        states={"number.shift": RuntimeError("Cloud nicht erreichbar")},
    )

    override.set_boosts(comfort=False, emergency=False)

    assert ha.writes == [("number.curve", 0.9), ("number.shift", 22.0)]


# --- Quota-Check: mypyllant-Verzoegerung (bis zum naechsten Poll), HA-Read kann nach eigenem Schreiben veraltet sein ---

def test_write_role_does_not_trust_a_stale_ha_read_after_our_own_recent_write(make_store, clock):
    # A (restore 0.9/22) -> B (comfort 1.0/24) -> A, alles innerhalb OWN_WRITE_SETTLE_SECONDS: HA
    # zeigt wegen der mypyllant-Verzoegerung die ganze Zeit noch A, obwohl die Anlage zwischendurch
    # auf B stand. Der zweite A-Schreibvorgang darf trotzdem nicht uebersprungen werden, sonst
    # bleibt die Anlage auf B stehen.
    override, _, ha = _setup(make_store, backup=dict(RESTORE_POINT), states=dict(RESTORE_POINT_STATES), clock=clock)

    override.set_boosts(comfort=True, emergency=False)  # A -> B
    clock.advance(60)  # weit innerhalb der Settle-Zeit
    override.set_boosts(comfort=False, emergency=False)  # B -> A, HA zeigt (stale) weiter A

    assert ha.writes == _written("comfort") + _written("restore")


def test_write_role_trusts_a_stale_ha_read_again_after_the_settle_window(make_store, clock):
    # Dieselbe Lage wie oben, aber der zweite Versuch kommt erst NACH der Settle-Zeit: dann darf
    # HA wieder als eingeschwungen gelten, der (zufaellig) passende Read wird vertraut, kein
    # erneutes Schreiben.
    override, _, ha = _setup(make_store, backup=dict(RESTORE_POINT), states=dict(RESTORE_POINT_STATES), clock=clock)

    override.set_boosts(comfort=True, emergency=False)  # A -> B
    clock.advance(OWN_WRITE_SETTLE_SECONDS + 1)
    override.set_boosts(comfort=False, emergency=False)  # B -> A, jetzt wird der stale Read vertraut

    assert ha.writes == _written("comfort")


def test_write_min_flow_does_not_trust_a_stale_ha_read_after_our_own_recent_write(make_store, clock):
    # Dieselbe mypyllant-Verzoegerung fuer den Mindestvorlauf: 21 -> 22 -> 21 innerhalb der
    # Settle-Zeit, HA zeigt die ganze Zeit (stale) 21.
    store = make_store()
    ha = RecordingHa({"number.min_flow": 21.0})
    override = Override(store, MANIFEST, ha, OPTIONS, clock=clock)

    assert override.write_min_flow(22.0) == 22.0
    clock.advance(60)
    assert override.write_min_flow(21.0) == 21.0

    assert ha.writes == [("number.min_flow", 22.0), ("number.min_flow", 21.0)]


# --- Quota-Check nach einem Modus-Wechsel: HA-Read ist dann garantiert veraltet (force=True) ---

def test_prepare_zone_writes_the_start_value_even_if_the_stale_auto_setpoint_already_matches_it(make_store):
    # Repro: Zone "auto" zeigt 21.0, gespeicherter Wiederherstellungspunkt ist ebenfalls 21.0.
    # prepare_zone stellt auf Manuell um; der (stale, noch aus dem Zeitprogramm stammende) Read
    # zeigt zufaellig schon den Zielwert -- ohne force wuerde das Schreiben faelschlich
    # uebersprungen, obwohl der manuelle Sollwert der Anlage noch unbekannt ist.
    manifest = ChannelManifest(entity_ids={**MANIFEST.entity_ids, "shift_current": "climate.zone::temperature"})
    store = make_store(backup={"curve_current": 0.9, "shift_current": 21.0})
    ha = RecordingHa({"climate.zone": "auto", "climate.zone::temperature": 21.0})
    ha.set_hvac_mode = lambda entity, mode: ha.events.append(("hvac", entity, mode))
    ha.get_raw_state = lambda entity: ha.states[entity]
    ha.set_climate_temperature = lambda entity, value: ha.events.append(("write", entity, value))

    Override(store, manifest, ha, OPTIONS).prepare_zone(start_shift=21.0)

    assert ("hvac", "climate.zone", "heat_cool") in ha.events
    assert ("climate.zone", 21.0) in ha.writes


def test_prepare_zone_switches_the_mode_exactly_once(make_store):
    # Ruling #3: der Startwert-Schreibvorgang (ensure_mode=False) darf keinen zweiten
    # set_hvac_mode ausloesen.
    manifest = ChannelManifest(entity_ids={**MANIFEST.entity_ids, "shift_current": "climate.zone::temperature"})
    store = make_store(backup={"curve_current": 0.9})
    ha = RecordingHa({"climate.zone": "auto", "climate.zone::temperature": 0.0})
    ha.set_hvac_mode = lambda entity, mode: ha.events.append(("hvac", entity, mode))
    ha.get_raw_state = lambda entity: ha.states[entity]
    ha.set_climate_temperature = lambda entity, value: ha.events.append(("write", entity, value))

    Override(store, manifest, ha, OPTIONS).prepare_zone(start_shift=20.6)

    assert [event for event in ha.events if event[0] == "hvac"] == [("hvac", "climate.zone", "heat_cool")]


def test_write_forces_the_shift_write_when_the_zone_had_to_be_switched(make_store):
    # Wie oben, aber ueber den normalen Sollwert-Pfad (_write, nicht prepare_zone): eine soeben
    # umgestellte Zone zaehlt als "Parallelverschiebung muss geschrieben werden", auch wenn der
    # (stale) Read schon zufaellig den Zielwert zeigt.
    manifest = ChannelManifest(entity_ids={**MANIFEST.entity_ids, "shift_current": "climate.zone::temperature"})
    store = make_store(backup={"curve_current": 0.9, "shift_current": 22.0})
    ha = RecordingHa({"climate.zone": "auto", "climate.zone::temperature": 22.0})
    ha.set_hvac_mode = lambda entity, mode: ha.events.append(("hvac", entity, mode))
    ha.get_raw_state = lambda entity: ha.states[entity]
    ha.set_climate_temperature = lambda entity, value: ha.events.append(("write", entity, value))
    override = Override(store, manifest, ha, OPTIONS)

    override.write_roles(("shift_current",))

    assert ("hvac", "climate.zone", "heat_cool") in ha.events
    assert ("climate.zone", 22.0) in ha.writes


def test_write_switches_mode_before_writing_curve_and_shift_for_a_climate_zone(make_store):
    # Ruling #11a (Spec 5.2 "Betriebsart -> Steigung -> Parallelverschiebung"): Modus-Wechsel vor
    # der Kurve, die Parallelverschiebung zuletzt.
    manifest = ChannelManifest(entity_ids={**MANIFEST.entity_ids, "shift_current": "climate.zone::temperature"})
    store = make_store(backup={"curve_current": 0.9, "shift_current": 10.0})
    ha = RecordingHa({"climate.zone": "auto", "number.curve": 0.5})
    ha.set_hvac_mode = lambda entity, mode: ha.events.append(("hvac", entity, mode))
    ha.get_raw_state = lambda entity: ha.states[entity]
    ha.set_climate_temperature = lambda entity, value: ha.events.append(("write", entity, value))
    override = Override(store, manifest, ha, OPTIONS)

    override.write_roles(("curve_current", "shift_current"))

    ordered = [event for event in ha.events if event[0] in ("hvac", "write")]
    assert ordered == [
        ("hvac", "climate.zone", "heat_cool"),
        ("write", "number.curve", 0.9),
        ("write", "climate.zone", 15.0),
    ]


# --- Schonfrist-Details (Task-11-Nacharbeit) ---

def _zone_setup(make_store, clock, states):
    manifest = ChannelManifest(entity_ids={**MANIFEST.entity_ids, "shift_current": "climate.zone::temperature"})
    store = make_store(backup={"curve_current": 0.9, "shift_current": 22.0})
    ha = RecordingHa(states)

    def _set_hvac_mode(entity, mode):
        ha.events.append(("hvac", entity, mode))
        ha.states[entity] = mode

    ha.set_hvac_mode = _set_hvac_mode
    ha.get_raw_state = lambda entity: ha.states[entity]
    ha.set_climate_temperature = lambda entity, value: ha.events.append(("write", entity, value))
    return Override(store, manifest, ha, OPTIONS, clock=clock), ha


def test_write_role_skips_inside_the_settle_window_when_our_last_write_is_the_target(make_store, clock):
    # Innerhalb der Schonfrist: HA zeigt den Zielwert UND unser letzter eigener Schreibwert ist der
    # Zielwert -- ueberfluessiger Cloud-Aufruf, wird uebersprungen.
    store = make_store()
    ha = RecordingHa({"number.min_flow": 21.0})
    override = Override(store, MANIFEST, ha, OPTIONS, clock=clock)
    override.write_min_flow(22.0)
    ha.states["number.min_flow"] = 22.0
    clock.advance(60)

    assert override.write_min_flow(22.0) == 22.0

    assert ha.writes == [("number.min_flow", 22.0)]


def test_ensure_manual_zone_forgets_the_last_written_shift_after_a_switch(make_store, clock):
    # Nach einer Umschaltung auf Manuell ist der manuelle Sollwert der Anlage unbekannt: der letzte
    # eigene Schreibwert darf ein folgendes Schreiben innerhalb der Schonfrist nicht mehr ueberspringen.
    override, ha = _zone_setup(
        make_store, clock, {"climate.zone": "heat_cool", "climate.zone::temperature": 10.0},
    )
    override.write_roles(("shift_current",))
    ha.states["climate.zone::temperature"] = 22.0
    ha.states["climate.zone"] = "auto"  # jemand stellt die Zone um
    clock.advance(60)
    assert override.ensure_manual_zone() is True
    clock.advance(60)

    override.write_roles(("shift_current",))

    assert ha.writes == [("climate.zone", 22.0), ("climate.zone", 22.0)]


def test_after_a_restart_a_matching_ha_read_is_not_trusted_within_the_settle_window(make_store, clock):
    # Vor dem Neustart wurde B (Comfort 1.0/24) geschrieben, HA zeigt wegen mypyllant noch A. Kurz
    # nach dem Neustart soll A geschrieben werden: der passende Read darf das nicht ueberspringen.
    override, _, ha = _setup(
        make_store, backup={**RESTORE_POINT, "boost_active": True}, states=dict(RESTORE_POINT_STATES), clock=clock,
    )
    clock.advance(OWN_WRITE_SETTLE_SECONDS - 60)

    override.set_boosts(comfort=False, emergency=False)

    assert ha.writes == _written("restore")


def test_after_the_settle_window_of_uptime_a_matching_ha_read_is_trusted(make_store, clock):
    override, _, ha = _setup(
        make_store, backup={**RESTORE_POINT, "boost_active": True}, states=dict(RESTORE_POINT_STATES), clock=clock,
    )
    clock.advance(OWN_WRITE_SETTLE_SECONDS + 1)

    override.set_boosts(comfort=False, emergency=False)

    assert ha.writes == []


def test_settled_counts_from_the_start_and_from_each_own_write(make_store, clock):
    # Eine Regel fuer Quota-Check und Durchsetzung (manual_override.py): HA gilt fuer eine Rolle als
    # eingeschwungen, wenn das letzte eigene Schreiben -- sonst der Start -- laenger als
    # OWN_WRITE_SETTLE_SECONDS zurueckliegt.
    store = make_store()
    ha = RecordingHa({"number.min_flow": 25.0})
    override = Override(store, MANIFEST, ha, OPTIONS, clock=clock)
    assert override.settled("min_flow") is False
    clock.advance(OWN_WRITE_SETTLE_SECONDS + 1)
    assert override.settled("min_flow") is True
    override.write_min_flow(22.0)
    assert override.settled("min_flow") is False
    assert override.settled("curve_current") is True
    clock.advance(OWN_WRITE_SETTLE_SECONDS + 1)
    assert override.settled("min_flow") is True


# --- TP12b: Schreibbudget (AU-015) ---

def _count_write_attempts(ha):
    attempts = []
    original = ha.set_number_value

    def _counting(entity_id, value):
        attempts.append(entity_id)
        original(entity_id, value)

    ha.set_number_value = _counting
    return attempts


def test_failed_boost_end_is_retried_on_the_return_staircase(make_store, clock):
    override, store, ha = _setup(make_store, backup={**RESTORE_POINT, "boost_active": True}, clock=clock)
    attempts = _count_write_attempts(ha)
    ha.write_error = RuntimeError("403 Quota Exceeded")

    with pytest.raises(DeviceWriteError):
        override.set_boosts(comfort=False, emergency=False)  # t=0, Fehlschlag 1
    clock.advance(299)
    assert override.set_boosts(comfort=False, emergency=False) == (True, False)  # zurueckgestellt
    clock.advance(1)
    with pytest.raises(DeviceWriteError):
        override.set_boosts(comfort=False, emergency=False)  # t=300, Fehlschlag 2
    clock.advance(899)
    assert override.set_boosts(comfort=False, emergency=False) == (True, False)
    ha.write_error = None
    clock.advance(1)
    assert override.set_boosts(comfort=False, emergency=False) == (False, False)  # t=1200, Erfolg

    assert attempts == ["number.curve", "number.curve", "number.curve", "number.shift"]
    assert "boost_end" not in store.state.write_budget


def test_failed_boost_start_waits_30_minutes_and_a_success_costs_nothing(make_store, clock):
    override, _, ha = _setup(make_store, backup=dict(RESTORE_POINT), clock=clock)
    ha.write_error = RuntimeError("403 Quota Exceeded")

    with pytest.raises(DeviceWriteError):
        override.set_boosts(comfort=True, emergency=False)
    clock.advance(1799)
    assert override.set_boosts(comfort=True, emergency=False) == (False, False)
    ha.write_error = None
    clock.advance(1)
    assert override.set_boosts(comfort=True, emergency=False) == (True, False)
    assert override.set_boosts(comfort=False, emergency=False) == (False, False)
    assert override.set_boosts(comfort=True, emergency=False) == (True, False)  # zweiter Boost sofort


def test_failed_restore_is_retried_on_the_return_staircase(make_store, clock):
    override, store, ha = _setup(make_store, backup={**RESTORE_POINT, "emergency_boost_active": True}, clock=clock)
    ha.write_error = RuntimeError("HA nicht erreichbar")
    assert override.restore_and_clear(always_restore=True) is False

    clock.advance(299)
    attempts = _count_write_attempts(ha)
    assert override.restore_and_clear(always_restore=True) is False
    assert attempts == []

    ha.write_error = None
    clock.advance(1)
    assert override.restore_and_clear(always_restore=True) is True
    assert store.state.emergency_boost_active is False
