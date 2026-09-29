import logging

import pytest

from heizungsbruecke import backup_store
from heizungsbruecke.backup_store import load_backup, save_backup
from heizungsbruecke.delivery import SOURCE_LOCAL, DataFault, DeliveryState, PendingTick
from heizungsbruecke.state import BridgeState, StateStore
from heizungsbruecke.target_history import sanitize_history

# Vollstaendige backup.json, wie 0.16.0 sie schreibt.
V016_BACKUP = {
    "curve_current": 0.95, "offset_current": 23.0, "boost_active": True, "emergency_boost_active": False,
    "last_room_target": 21.0, "target_history": [[1000.0, 20.0], [2000.0, 21.0]],
    "last_published_target_rt": 21.0, "last_daily_trigger_date": "2026-09-26",
}


def _raise_oserror(*args, **kwargs):
    raise OSError("Datentraeger kaputt")


def _count_saves(monkeypatch) -> list:
    saves = []
    original = backup_store.save_backup

    def _spy(path, values):
        saves.append(path.name)
        original(path, values)

    monkeypatch.setattr("heizungsbruecke.backup_store.save_backup", _spy)
    return saves


# --- Laden ---

def test_reads_files_written_by_0_16_0(make_store):
    store = make_store(
        backup=V016_BACKUP,
        failsafe={"failsafe_active": True, "datenfehler": None, "pending": {"seq": "s1", "trigger": "daily"}},
    )

    assert store.state == BridgeState(
        curve_current=0.95, offset_current=23.0, boost_active=True, emergency_boost_active=False,
        last_room_target=21.0, target_history=[[1000.0, 20.0], [2000.0, 21.0]],
        last_published_target_rt=21.0, last_daily_trigger_date="2026-09-26",
        delivery=DeliveryState(pending=PendingTick("s1", "daily"), notbetrieb=True),
    )


def test_missing_files_give_defaults_and_nothing_is_written(make_store, tmp_path):
    store = make_store()

    assert store.state == BridgeState()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("content", ["{kaputt", "[1, 2]", ""])
def test_broken_backup_gives_defaults_but_keeps_failsafe_file(make_store, tmp_path, caplog, content):
    save_backup(tmp_path / "failsafe_state.json", {"failsafe_active": True})
    (tmp_path / "backup.json").write_text(content)

    with caplog.at_level(logging.WARNING):
        store = make_store()

    assert store.state == BridgeState(delivery=DeliveryState(notbetrieb=True))
    assert "backup.json" in caplog.text


def test_broken_failsafe_file_keeps_backup(make_store, tmp_path):
    (tmp_path / "failsafe_state.json").write_text("{kaputt")

    store = make_store(backup={"emergency_boost_active": True})

    assert store.state.emergency_boost_active is True
    assert store.state.delivery == DeliveryState()


def test_failsafe_file_without_object_is_warned_and_ignored(make_store, tmp_path, caplog):
    (tmp_path / "failsafe_state.json").write_text("[1, 2]")

    with caplog.at_level(logging.WARNING):
        store = make_store()

    assert store.state.delivery == DeliveryState()
    assert "failsafe_state.json enthaelt kein JSON-Objekt" in caplog.text


@pytest.mark.parametrize("key,value", [
    ("curve_current", "0.9"), ("curve_current", float("nan")), ("offset_current", True),
    ("boost_active", "ja"), ("emergency_boost_active", 1), ("last_room_target", [21]),
    ("last_published_target_rt", {}), ("last_daily_trigger_date", 20260926),
])
def test_field_with_wrong_type_falls_back_only_for_that_field(make_store, caplog, key, value):
    with caplog.at_level(logging.WARNING):
        store = make_store(backup={**V016_BACKUP, key: value})

    assert getattr(store.state, key) == getattr(BridgeState(), key)
    assert store.state.target_history == V016_BACKUP["target_history"]
    assert key in caplog.text


@pytest.mark.parametrize("raw", [None, "x", [["x", 1]], [[1.0]]])
def test_invalid_target_history_is_reset_with_warning(make_store, caplog, raw):
    with caplog.at_level(logging.WARNING):
        store = make_store(backup={"target_history": raw})

    assert store.state.target_history == []
    assert "target_history" in caplog.text


def test_null_last_room_target_from_0_16_0_is_accepted_silently(make_store, caplog):
    # 0.16.0 schrieb last_room_target=null, wenn ein Boost vor dem ersten Sollwert abgelehnt wurde.
    with caplog.at_level(logging.WARNING):
        store = make_store(backup={"last_room_target": None})

    assert store.state.last_room_target is None
    assert caplog.text == ""


def test_notify_states_round_trip(make_store, tmp_path):
    store = make_store()

    store.update(notify_states={"notbetrieb": "aktiv", "batterie:sensor.wz_battery": "niedrig"})
    reloaded = StateStore(tmp_path / "backup.json", tmp_path / "failsafe_state.json")

    assert reloaded.state.notify_states == {"notbetrieb": "aktiv", "batterie:sensor.wz_battery": "niedrig"}


@pytest.mark.parametrize("raw", [None, "x", [1], {"a": 1}, {"a": ["x"]}])
def test_invalid_notify_states_fall_back_to_empty(make_store, caplog, raw):
    with caplog.at_level(logging.WARNING):
        store = make_store(backup={"notify_states": raw})

    assert store.state.notify_states == {}
    assert "notify_states" in caplog.text


def test_empty_notify_states_are_not_written(make_store, tmp_path):
    store = make_store()

    store.update(boost_active=True, notify_states={})

    assert "notify_states" not in load_backup(tmp_path / "backup.json")


def test_notify_messages_round_trip(make_store, tmp_path):
    store = make_store()

    store.update(notify_messages={"notbetrieb": "SmartHeat: Notbetrieb aktiv"})
    reloaded = StateStore(tmp_path / "backup.json", tmp_path / "failsafe_state.json")

    assert reloaded.state.notify_messages == {"notbetrieb": "SmartHeat: Notbetrieb aktiv"}


@pytest.mark.parametrize("raw", [None, "x", [1], {"a": 1}, {"a": ["x"]}])
def test_invalid_notify_messages_fall_back_to_empty(make_store, caplog, raw):
    with caplog.at_level(logging.WARNING):
        store = make_store(backup={"notify_messages": raw, "notify_states": {"notbetrieb": "aktiv"}})

    assert store.state.notify_messages == {}
    assert store.state.notify_states == {"notbetrieb": "aktiv"}
    assert "notify_messages" in caplog.text


def test_empty_notify_messages_are_not_written(make_store, tmp_path):
    store = make_store()

    store.update(boost_active=True, notify_messages={})

    assert "notify_messages" not in load_backup(tmp_path / "backup.json")


# --- Schreiben ---

def test_update_writes_backup_with_unknown_keys_and_without_unset_fields(make_store, tmp_path):
    store = make_store(backup={"zukunft": {"x": 1}, "last_room_target": None})

    store.update(boost_active=True)

    assert load_backup(tmp_path / "backup.json") == {
        "zukunft": {"x": 1}, "boost_active": True, "emergency_boost_active": False, "target_history": [],
    }


def test_written_backup_is_readable_the_way_0_16_0_reads_it(make_store, tmp_path):
    # Review Focus 2: 0.16.0 prueft vor dem ersten Boost `"curve_current" in backup`.
    store = make_store()

    store.update(last_room_target=21.0, target_history=[[1000.0, 21.0]])
    backup = load_backup(tmp_path / "backup.json")

    assert "curve_current" not in backup and "offset_current" not in backup
    assert backup.get("boost_active", False) is False
    assert backup["last_room_target"] == 21.0
    assert sanitize_history(backup["target_history"]) == [[1000.0, 21.0]]

    store.update(curve_current=0.9, offset_current=22.0)

    assert (load_backup(tmp_path / "backup.json")["curve_current"], load_backup(tmp_path / "backup.json")["offset_current"]) == (0.9, 22.0)


def test_round_trip_through_a_new_store(make_store, tmp_path):
    store = make_store()
    store.update(**V016_BACKUP)
    store.set_delivery(DeliveryState(pending=PendingTick("s1", "daily"), datenfehler=DataFault(SOURCE_LOCAL, ("dat",))))

    reloaded = StateStore(tmp_path / "backup.json", tmp_path / "failsafe_state.json")

    assert reloaded.state == store.state


def test_update_writes_only_when_persisted_content_changes(make_store, monkeypatch):
    store = make_store(backup=V016_BACKUP)
    saves = _count_saves(monkeypatch)

    store.update(boost_active=True)  # unveraendert
    store.update(stable_target=22.0, abo_finished=True)  # nur Laufzeit

    assert saves == []

    store.update(last_room_target=22.0)

    assert saves == ["backup.json"]


def test_update_rejects_delivery(make_store):
    with pytest.raises(ValueError):
        make_store().update(delivery=DeliveryState())


def test_backup_write_failure_keeps_memory_raises_and_is_retried(make_store, tmp_path, monkeypatch):
    store = make_store(backup=V016_BACKUP)
    monkeypatch.setattr("heizungsbruecke.backup_store.save_backup", _raise_oserror)

    with pytest.raises(OSError):
        store.update(curve_current=1.1)

    assert store.state.curve_current == 1.1
    assert load_backup(tmp_path / "backup.json")["curve_current"] == 0.95

    monkeypatch.undo()
    store.update(curve_current=1.1)  # inhaltlich keine Aenderung, holt das Speichern aber nach

    assert load_backup(tmp_path / "backup.json")["curve_current"] == 1.1


def test_is_saved_reports_only_fields_whose_save_is_still_missing(make_store, monkeypatch):
    store = make_store(backup=V016_BACKUP)
    assert store.is_saved("curve_current", "offset_current", "boost_active")

    monkeypatch.setattr("heizungsbruecke.backup_store.save_backup", _raise_oserror)
    with pytest.raises(OSError):
        store.update(curve_current=1.1)

    assert not store.is_saved("curve_current")
    assert not store.is_saved("curve_current", "offset_current")
    assert store.is_saved("offset_current", "boost_active")  # nur das geaenderte Feld fehlt auf dem Datentraeger

    monkeypatch.undo()
    store.update(curve_current=1.1)

    assert store.is_saved("curve_current", "offset_current")


def test_is_saved_of_an_unset_field_that_was_never_written(make_store):
    assert make_store().is_saved("curve_current", "offset_current")


def test_runtime_only_update_never_retries_a_failed_write(make_store, monkeypatch):
    store = make_store()
    monkeypatch.setattr("heizungsbruecke.backup_store.save_backup", _raise_oserror)
    with pytest.raises(OSError):
        store.update(boost_active=True)

    store.update(stable_target=21.0)  # darf nicht werfen

    assert store.state.stable_target == 21.0


def test_set_delivery_writes_only_on_persisted_change(make_store, tmp_path, monkeypatch):
    store = make_store()
    saves = _count_saves(monkeypatch)

    store.set_delivery(DeliveryState(server_failures=1))  # nur fluechtige Felder

    assert saves == []
    assert store.state.delivery.server_failures == 1

    store.set_delivery(DeliveryState(notbetrieb=True))

    assert saves == ["failsafe_state.json"]
    assert load_backup(tmp_path / "failsafe_state.json") == {"failsafe_active": True, "datenfehler": None, "pending": None}


def test_set_delivery_write_failure_is_logged_and_retried(make_store, tmp_path, monkeypatch, caplog):
    store = make_store()
    monkeypatch.setattr("heizungsbruecke.backup_store.save_backup", _raise_oserror)

    with caplog.at_level(logging.ERROR):
        store.set_delivery(DeliveryState(notbetrieb=True))  # wirft nicht

    assert store.state.delivery.notbetrieb is True
    assert "failsafe_state.json" in caplog.text

    monkeypatch.undo()
    store.set_delivery(DeliveryState(notbetrieb=True))

    assert load_backup(tmp_path / "failsafe_state.json")["failsafe_active"] is True


def test_update_saved_keeps_memory_unchanged_when_the_write_fails(make_store, monkeypatch):
    # N5: der naechste Check sieht dieselbe Aenderung erneut.
    store = make_store(backup={"last_room_target": 21.0})
    monkeypatch.setattr("heizungsbruecke.backup_store.save_backup", _raise_oserror)

    with pytest.raises(OSError):
        store.update_saved(last_room_target=22.0)

    assert store.state.last_room_target == 21.0


def test_saved_restore_point_is_the_last_successfully_saved_one(make_store, monkeypatch):
    store = make_store(backup={"curve_current": 0.9, "offset_current": 22.0})
    monkeypatch.setattr("heizungsbruecke.backup_store.save_backup", _raise_oserror)
    with pytest.raises(OSError):
        store.update(curve_current=1.0, offset_current=25.0)

    assert store.saved_restore_point() == {"curve_current": 0.9, "offset_current": 22.0}

    store.revert_to_saved("curve_current", "offset_current")

    assert (store.state.curve_current, store.state.offset_current) == (0.9, 22.0)
    assert store.is_saved("curve_current", "offset_current")


def test_saved_restore_point_needs_both_values(make_store):
    assert make_store().saved_restore_point() is None
    assert make_store(backup={"curve_current": 0.9}).saved_restore_point() is None


def test_status_and_r6_fields_round_trip(make_store, tmp_path):
    store = make_store()
    override = {"curve": 1.3, "offset": 24.5, "erkannt": "2026-10-01T08:00:00+02:00"}

    store.update(last_ack_at="2026-10-01T12:00:05+02:00", manual_override=override, manual_override_pending=override)

    reread = StateStore(tmp_path / "backup.json", tmp_path / "failsafe_state.json").state
    assert (reread.last_ack_at, reread.manual_override, reread.manual_override_pending) == (
        "2026-10-01T12:00:05+02:00", override, override,
    )


@pytest.mark.parametrize("key,value", [
    ("last_ack_at", 5),
    ("manual_override", {"curve": "x", "offset": 1.0, "erkannt": "t"}),
    ("manual_override_pending", [1.3, 24.5]),
])
def test_invalid_status_and_r6_fields_fall_back(make_store, caplog, key, value):
    store = make_store(backup={key: value})

    assert getattr(store.state, key) is None
    assert key in caplog.text


def test_manual_override_misses_is_runtime_only(make_store, monkeypatch):
    store = make_store()
    saves = _count_saves(monkeypatch)

    store.update(manual_override_misses=1)

    assert saves == []
