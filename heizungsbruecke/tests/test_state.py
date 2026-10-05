import json
import logging

import pytest

from smartheat_runtime import backup_store
from smartheat_runtime.backup_store import load_backup, save_backup
from smartheat_runtime.delivery import SOURCE_LOCAL, DataFault, DeliveryState, PendingTick
from smartheat_runtime.state import BridgeState, StateStore, StorageError
from smartheat_runtime.waerme import WaermeState

# Vollstaendige backup.json, wie 0.16.0 sie schreibt (vor TP11: die Parallelverschiebung hiess
# noch "offset_current", target_history gab es noch als aktiv gefuehrtes Feld).
V016_BACKUP = {
    "curve_current": 0.95, "offset_current": 23.0, "boost_active": True, "emergency_boost_active": False,
    "last_room_target": 21.0, "target_history": [[1000.0, 20.0], [2000.0, 21.0]],
    "last_published_target_rt": 21.0, "last_daily_trigger_date": "2026-09-26",
}
# Dieselben Werte im aktuellen Schema (fuer Tests, die store.update(**...) direkt aufrufen statt
# aus einer Datei zu laden -- update() akzeptiert nur echte BridgeState-Felder, keine Legacy-Keys).
CURRENT_BACKUP = {
    "restore_point": {"curve": 0.95, "room_setpoint": 23.0}, "boost_active": True, "emergency_boost_active": False,
    "last_room_target": 21.0,
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

    monkeypatch.setattr("smartheat_runtime.backup_store.save_backup", _spy)
    return saves


# --- Laden ---

def test_reads_files_written_by_0_16_0(make_store):
    # offset_current/target_history sind seit TP11 keine erkannten Felder mehr (siehe
    # test_pre_tp11_backup_keeps_unknown_keys_and_drops_the_old_override): die Wunschtemperatur fehlt
    # im Wiederherstellungspunkt, curve_current wird zum Hebel curve (Plan 2), die restlichen Felder
    # werden wie gewohnt erkannt.
    store = make_store(
        backup=V016_BACKUP,
        failsafe={"failsafe_active": True, "datenfehler": None, "pending": {"seq": "s1", "trigger": "daily"}},
    )

    assert store.state == BridgeState(
        restore_point={"curve": 0.95}, boost_active=True, emergency_boost_active=False,
        last_room_target=21.0,
        last_published_target_rt=21.0, last_daily_trigger_date="2026-09-26",
        delivery=DeliveryState(pending=PendingTick("s1", "daily"), notbetrieb=True),
    )


def test_pre_tp11_backup_keeps_unknown_keys_and_drops_the_old_override(make_store, tmp_path, caplog):
    # Ein Backup von vor TP11: offset_current/target_history sind keine erkannten Felder mehr, der
    # alte manual_override (Schluessel "offset" statt "shift") ist ungueltig. Alles davon darf beim
    # Laden weder abstuerzen noch stillschweigend verschwinden.
    old_backup = {
        "curve_current": 0.95, "offset_current": 23.0, "target_history": [[1000.0, 20.0]],
        "manual_override": {"curve": 1.3, "offset": 24.5, "erkannt": "2026-10-01T08:00:00+02:00"},
    }
    with caplog.at_level(logging.WARNING):
        store = make_store(backup=old_backup)

    assert store.state.restore_point.get("room_setpoint") is None
    assert store.state.restore_point.get("curve") == 0.95
    assert store.state.manual_override is None
    assert "manual_override" in caplog.text

    store.update(boost_active=True)  # erzwingt ein Schreiben -- extra muss erhalten bleiben

    backup = load_backup(tmp_path / "backup.json")
    assert backup["offset_current"] == 23.0
    assert backup["target_history"] == [[1000.0, 20.0]]
    assert "manual_override" not in backup


def test_unknown_top_level_key_survives_a_save_so_a_rollback_keeps_write_budget(make_store, tmp_path):
    # Rollback von 0.25.0 auf 0.24.0: dort ist write_budget ein unbekannter Schluessel. Ein Parser
    # mit dieser Semantik (unbekannt -> extra, beim Speichern zurueckgeschrieben) verliert den
    # Zaehler nicht; hier am Beispiel eines Schluessels, den der Parser nicht kennt (Spec 5.1).
    budget = {"boost": {"day": "2026-09-30", "count": 2}}
    store = make_store(backup={"restore_point": {"curve": 0.95}, "write_budget": budget, "kommt_spaeter": {"a": 1}})

    assert store.state.write_budget == budget
    store.update(boost_active=True)

    backup = load_backup(tmp_path / "backup.json")
    assert backup["kommt_spaeter"] == {"a": 1}
    assert backup["write_budget"] == budget


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
    ("restore_point", {"curve": "0.9"}), ("restore_point", {"curve": float("nan")}),
    ("restore_point", {"room_setpoint": True}),
    ("boost_active", "ja"), ("emergency_boost_active", 1), ("last_room_target", [21]),
    ("last_published_target_rt", {}), ("last_daily_trigger_date", 20260926),
])
def test_field_with_wrong_type_falls_back_only_for_that_field(make_store, caplog, key, value):
    # Ohne den alten Schluessel curve_current: neben restore_point gaelte er als neuerer Wert (Rueckweg, Ruling Task 8).
    base = {k: v for k, v in V016_BACKUP.items() if key != "restore_point" or k != "curve_current"}
    with caplog.at_level(logging.WARNING):
        store = make_store(backup={**base, key: value})

    assert getattr(store.state, key) == getattr(BridgeState(), key)
    assert key in caplog.text


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
        "zukunft": {"x": 1}, "boost_active": True, "emergency_boost_active": False,
    }


def test_written_backup_has_a_restore_point_only_once_one_is_set(make_store, tmp_path):
    # Bis 0.29.0 fuer den Rueckweg auf 0.16.0 (`"curve_current" in backup`); seit Plan 2 (P2-4) gilt dasselbe fuer
    # restore_point: ein leerer Punkt wird nicht geschrieben, und alte Rollen-Schluessel nie.
    store = make_store()

    store.update(last_room_target=21.0)
    backup = load_backup(tmp_path / "backup.json")

    assert "restore_point" not in backup and "curve_current" not in backup and "shift_current" not in backup
    assert backup.get("boost_active", False) is False
    assert backup["last_room_target"] == 21.0

    store.update(restore_point={"curve": 0.9, "room_setpoint": 22.0})

    backup = load_backup(tmp_path / "backup.json")
    assert (backup["restore_point"]["curve"], backup["restore_point"]["room_setpoint"]) == (0.9, 22.0)
    assert "curve_current" not in backup and "shift_current" not in backup


def test_round_trip_through_a_new_store(make_store, tmp_path):
    store = make_store()
    store.update(**CURRENT_BACKUP)
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
    store = make_store(backup=CURRENT_BACKUP)
    monkeypatch.setattr("smartheat_runtime.backup_store.save_backup", _raise_oserror)

    with pytest.raises(OSError):
        store.update(restore_point={"curve": 1.1, "room_setpoint": 23.0})

    assert store.state.restore_point["curve"] == 1.1
    assert load_backup(tmp_path / "backup.json")["restore_point"]["curve"] == 0.95

    monkeypatch.undo()
    # inhaltlich keine Aenderung, holt das Speichern aber nach
    store.update(restore_point={"curve": 1.1, "room_setpoint": 23.0})

    assert load_backup(tmp_path / "backup.json")["restore_point"]["curve"] == 1.1


def test_is_saved_reports_only_fields_whose_save_is_still_missing(make_store, monkeypatch):
    # Seit Plan 2 ist der Wiederherstellungspunkt EIN Feld (restore_point) statt je Rolle eines.
    store = make_store(backup=CURRENT_BACKUP)
    assert store.is_saved("restore_point", "last_room_target", "boost_active")

    monkeypatch.setattr("smartheat_runtime.backup_store.save_backup", _raise_oserror)
    with pytest.raises(OSError):
        store.update(restore_point={"curve": 1.1, "room_setpoint": 23.0})

    assert not store.is_saved("restore_point")
    assert not store.is_saved("restore_point", "last_room_target")
    assert store.is_saved("last_room_target", "boost_active")  # nur das geaenderte Feld fehlt auf dem Datentraeger

    monkeypatch.undo()
    store.update(restore_point={"curve": 1.1, "room_setpoint": 23.0})

    assert store.is_saved("restore_point", "last_room_target")


def test_is_saved_of_an_unset_field_that_was_never_written(make_store):
    assert make_store().is_saved("restore_point", "originals")


def test_runtime_only_update_never_retries_a_failed_write(make_store, monkeypatch):
    store = make_store()
    monkeypatch.setattr("smartheat_runtime.backup_store.save_backup", _raise_oserror)
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
    monkeypatch.setattr("smartheat_runtime.backup_store.save_backup", _raise_oserror)

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
    monkeypatch.setattr("smartheat_runtime.backup_store.save_backup", _raise_oserror)

    with pytest.raises(OSError):
        store.update_saved(last_room_target=22.0)

    assert store.state.last_room_target == 21.0


REQUIRED = ("curve", "room_setpoint")  # Vaillant: alle Hebel ausser optional_restore (heat_limit)


def test_saved_restore_point_is_the_last_successfully_saved_one(make_store, monkeypatch):
    store = make_store(backup={"restore_point": {"curve": 0.9, "room_setpoint": 22.0}})
    monkeypatch.setattr("smartheat_runtime.backup_store.save_backup", _raise_oserror)
    with pytest.raises(OSError):
        store.update(restore_point={"curve": 1.0, "room_setpoint": 25.0})

    assert store.saved_restore_point(REQUIRED) == {"curve": 0.9, "room_setpoint": 22.0}

    store.revert_to_saved("restore_point")

    assert (store.state.restore_point.get("curve"), store.state.restore_point.get("room_setpoint")) == (0.9, 22.0)
    assert store.is_saved("restore_point")


def test_saved_restore_point_needs_both_values(make_store):
    assert make_store().saved_restore_point(REQUIRED) is None
    assert make_store(backup={"restore_point": {"curve": 0.9}}).saved_restore_point(REQUIRED) is None


def test_status_and_r6_fields_round_trip(make_store, tmp_path):
    store = make_store()
    override = {"levers": {"curve": 1.3, "room_setpoint": 24.5}, "erkannt": "2026-10-01T08:00:00+02:00"}

    store.update(last_ack_at="2026-10-01T12:00:05+02:00", manual_override=override, manual_override_pending=override)

    reread = StateStore(tmp_path / "backup.json", tmp_path / "failsafe_state.json").state
    assert (reread.last_ack_at, reread.manual_override, reread.manual_override_pending) == (
        "2026-10-01T12:00:05+02:00", override, override,
    )


@pytest.mark.parametrize("key,value", [
    ("last_ack_at", 5),
    ("manual_override", {"levers": {"curve": "x", "room_setpoint": 1.0}, "erkannt": "t"}),
    ("manual_override", {"levers": {}, "erkannt": "t"}),
    ("manual_override", {"levers": {"curve": True}, "erkannt": "t"}),
    ("manual_override", {"curve": "x", "shift": 1.0, "erkannt": "t"}),
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


def test_failed_backup_write_raises_storage_error_and_sets_storage_failed(make_store, monkeypatch):
    store = make_store(backup=V016_BACKUP)
    assert store.storage_failed is False
    monkeypatch.setattr("smartheat_runtime.backup_store.save_backup", _raise_oserror)

    with pytest.raises(StorageError, match="Datentraeger kaputt"):
        store.update(restore_point={"curve": 1.1})

    assert store.storage_failed is True


def test_failed_failsafe_write_sets_storage_failed_without_raising(make_store, monkeypatch):
    store = make_store()
    monkeypatch.setattr("smartheat_runtime.backup_store.save_backup", _raise_oserror)

    store.set_delivery(DeliveryState(notbetrieb=True))

    assert store.storage_failed is True


def test_flush_writes_dirty_files_and_clears_storage_failed(make_store, tmp_path, monkeypatch):
    store = make_store(backup=V016_BACKUP)
    with monkeypatch.context() as patch:
        patch.setattr("smartheat_runtime.backup_store.save_backup", _raise_oserror)
        with pytest.raises(StorageError):
            store.update(restore_point={"curve": 1.1})
        store.set_delivery(DeliveryState(notbetrieb=True))
        with pytest.raises(StorageError):
            store.flush()
        assert store.storage_failed is True

    store.flush()

    assert store.storage_failed is False
    assert load_backup(tmp_path / "backup.json")["restore_point"]["curve"] == 1.1
    assert load_backup(tmp_path / "failsafe_state.json")["failsafe_active"] is True


def test_flush_without_dirty_files_writes_nothing(make_store, monkeypatch):
    store = make_store(backup=V016_BACKUP)
    saves = _count_saves(monkeypatch)

    store.flush()

    assert saves == []


def test_write_budget_survives_a_restart_without_the_monotonic_time(make_store, tmp_path):
    store = make_store()
    store.update(write_budget={
        "boost_end": {"day": "2026-10-01", "count": 2, "last": 1234.0},
        "enforce:curve": {"day": "2026-10-01", "count": 6, "last": 99.0, "limit_notified": "2026-10-01"},
    })

    reloaded = StateStore(tmp_path / "backup.json", tmp_path / "failsafe_state.json")

    assert reloaded.state.write_budget == {
        "boost_end": {"day": "2026-10-01", "count": 2},
        "enforce:curve": {"day": "2026-10-01", "count": 6, "limit_notified": "2026-10-01"},
    }


@pytest.mark.parametrize("raw", [[1], {"x": 1}, {"x": {"day": 1, "count": 1}}, {"x": {"day": "d", "count": True}},
                                 {"x": {"day": "d", "count": -1}}])
def test_broken_write_budget_falls_back_to_empty(make_store, raw):
    assert make_store(backup={"write_budget": raw}).state.write_budget == {}


def test_empty_write_budget_is_not_written(make_store, tmp_path):
    store = make_store()
    store.update(write_budget={}, restore_point={"curve": 1.0})
    assert "write_budget" not in load_backup(tmp_path / "backup.json")


def test_waerme_fehlt_seit_survives_a_restart(make_store, tmp_path):
    store = make_store()
    store.update(waerme_fehlt_seit="2026-09-30T05:11:00+02:00")

    reloaded = StateStore(tmp_path / "backup.json", tmp_path / "failsafe_state.json")

    assert reloaded.state.waerme_fehlt_seit == "2026-09-30T05:11:00+02:00"
    assert reloaded.state.waerme is None


def test_the_runtime_waerme_phase_is_never_written_to_backup_json(make_store, tmp_path):
    store = make_store()
    store.update(waerme=WaermeState(beobachtung_seit=None, unter_schwelle=True))
    assert not (tmp_path / "backup.json").exists()


@pytest.mark.parametrize("raw", [5, "abc", "2026-09-30T05:11:00"])
def test_an_invalid_waerme_fehlt_seit_falls_back_to_no_flag(make_store, caplog, raw):
    with caplog.at_level(logging.WARNING):
        store = make_store({"waerme_fehlt_seit": raw})
    assert store.state.waerme_fehlt_seit is None
    assert "waerme_fehlt_seit" in caplog.text


def test_a_timezone_aware_waerme_fehlt_seit_loads(make_store):
    store = make_store({"waerme_fehlt_seit": "2026-09-30T05:11:00+02:00"})
    assert store.state.waerme_fehlt_seit == "2026-09-30T05:11:00+02:00"


def test_heat_limit_fields_round_trip_through_backup(make_store):
    store = make_store(backup={"restore_point": {"heat_limit": 16.0}, "originals": {"heat_limit": 15.0}})
    assert (store.state.restore_point.get("heat_limit"), store.state.originals.get("heat_limit")) == (16.0, 15.0)
    store.update(restore_point={"heat_limit": 17.0})
    assert load_backup(store._backup_path)["restore_point"]["heat_limit"] == 17.0


def test_invalid_heat_limit_in_backup_falls_back_to_none(make_store):
    store = make_store(backup={"restore_point": {"heat_limit": "hoch"}, "originals": {"heat_limit": float("nan")}})
    assert (store.state.restore_point.get("heat_limit"), store.state.originals.get("heat_limit")) == (None, None)


# --- Plan 2: Migration der Rollen-Felder bis 0.29.0 auf Hebel (P2-4, Plan-Praezisierung 1) ---

CLIENT1_BACKUP_029 = {
    "curve_current": 1.05, "shift_current": 17.5, "heat_limit": 17.4, "heat_limit_original": 15.0,
    "boost_active": False, "emergency_boost_active": False, "last_room_target": 20.5,
    "write_budget": {"enforce:curve_current": {"day": "2026-10-02", "count": 2},
                     "enforce:shift_current": {"day": "2026-10-02", "count": 1},
                     "enforce:zone_mode": {"day": "2026-10-02", "count": 1}},
    "manual_override": {"curve": 1.2, "shift": 17.5, "erkannt": "2026-10-02T09:00:00+02:00",
                        "rollen": {"curve_current": 1.2}, "signatur": "curve_current=1.2", "gemeldet": "curve_current=1.2"},
}


def test_backup_from_029_migrates_to_levers_and_drops_old_keys(tmp_path):
    backup = tmp_path / "backup.json"
    backup.write_text(json.dumps(CLIENT1_BACKUP_029), encoding="utf-8")
    store = StateStore(backup, tmp_path / "failsafe_state.json")
    state = store.state
    assert state.restore_point == {"curve": 1.05, "room_setpoint": 17.5, "heat_limit": 17.4}
    assert state.originals == {"heat_limit": 15.0}
    assert state.write_budget == {
        "enforce:curve": {"day": "2026-10-02", "count": 2},
        "enforce:room_setpoint": {"day": "2026-10-02", "count": 1},
        "enforce:zone_mode": {"day": "2026-10-02", "count": 1},
    }
    assert state.manual_override["rollen"] == {"curve": 1.2}
    assert state.manual_override["gemeldet"] == state.manual_override["signatur"]  # keine zweite Meldung
    store.update(last_room_target=21.0)
    written = json.loads(backup.read_text(encoding="utf-8"))
    assert not {"curve_current", "shift_current", "heat_limit", "heat_limit_original"} & set(written)
    assert written["restore_point"] == {"curve": 1.05, "room_setpoint": 17.5, "heat_limit": 17.4}


def test_migrated_signature_matches_a_recomputed_one(tmp_path):
    """signatur und gemeldet werden wie `rollen` umbenannt: manual_override bildet die Signatur aus `rollen`
    ("<hebel>=<wert>", sortiert); ein Vergleich mit einem alten Rollen-Text loeste sonst eine zweite Meldung aus."""
    raw = {
        **CLIENT1_BACKUP_029,
        "manual_override": {
            "curve": 1.2, "shift": 18.0, "erkannt": "2026-10-02T09:00:00+02:00",
            "rollen": {"curve_current": 1.2, "shift_current": 18.0, "zone_mode": 0.0},
            "signatur": "curve_current=1.2,shift_current=18,zone_mode=0", "gemeldet": "curve_current=1.2",
        },
    }
    raw["notify_states"] = {
        "manueller_eingriff": "curve_current=1.2,shift_current=18,zone_mode=0", "batterie:sensor.x": "niedrig",
    }
    backup = tmp_path / "backup.json"
    backup.write_text(json.dumps(raw), encoding="utf-8")
    state = StateStore(backup, tmp_path / "failsafe_state.json").state
    override = state.manual_override
    assert override["rollen"] == {"curve": 1.2, "room_setpoint": 18.0, "zone_mode": 0.0}
    recomputed = ",".join(f"{lever}={override['rollen'][lever]:g}" for lever in sorted(override["rollen"]))
    assert override["signatur"] == recomputed == "curve=1.2,room_setpoint=18,zone_mode=0"
    assert override["gemeldet"] == "curve=1.2"
    # Meldezustand des Notifiers: dieselbe Signatur, sonst gaelte sie als neuer Zustand (zweite Meldung).
    assert state.notify_states == {
        "manueller_eingriff": "curve=1.2,room_setpoint=18,zone_mode=0", "batterie:sensor.x": "niedrig",
    }


@pytest.mark.parametrize("state", ["ok", "limit:2026-10-02:Heizkurve, Wunschtemperatur der Zone"])
def test_notify_state_without_a_signature_is_kept(make_store, state):
    store = make_store(backup={"notify_states": {"manueller_eingriff": state}})
    assert store.state.notify_states == {"manueller_eingriff": state}


def test_a_migrated_backup_is_not_rewritten_without_a_change(make_store, tmp_path, monkeypatch):
    # Der erste Schreibanlass schreibt die Hebel-Form (siehe oben); ohne Aenderung bleibt die Datei, ein erneuter
    # Start liest sie wieder gleich.
    store = make_store(backup=CLIENT1_BACKUP_029)
    saves = _count_saves(monkeypatch)
    store.update(boost_active=False, last_room_target=20.5)
    assert saves == []
    assert store.is_saved("restore_point", "originals", "write_budget", "manual_override")
    reloaded = StateStore(tmp_path / "backup.json", tmp_path / "failsafe_state.json")
    assert reloaded.state == store.state


def test_legacy_field_with_wrong_type_is_dropped_only_for_that_field(make_store, caplog):
    # Wie bis 0.29.0 je Feld: ein ungueltiger alter Wert faellt weg, die anderen bleiben.
    with caplog.at_level(logging.WARNING):
        store = make_store(backup={"curve_current": "0.9", "shift_current": 22.0, "heat_limit_original": True})
    assert store.state.restore_point == {"room_setpoint": 22.0}
    assert store.state.originals == {}
    assert "curve_current" in caplog.text and "heat_limit_original" in caplog.text


# 0.30.0 -> Rueckweg auf 0.29.0 -> erneutes Update: 0.29.0 fuehrte die neuen Felder als unbekannte Schluessel mit
# und schrieb daneben wieder die alten; die alten sind dann die neueren Werte (Controller-Ruling Task 8).

def test_legacy_restore_values_win_over_a_stale_restore_point_after_a_rollback(make_store, tmp_path):
    store = make_store(backup={
        "restore_point": {"curve": 0.8, "room_setpoint": 20.0, "heat_limit": 14.0},
        "curve_current": 1.05, "shift_current": 17.5,
    })
    # Alte Werte gewinnen; die Heizgrenze traegt kein altes Feld und behaelt ihren Eintrag.
    assert store.state.restore_point == {"curve": 1.05, "room_setpoint": 17.5, "heat_limit": 14.0}
    store.update(last_room_target=21.0)
    written = load_backup(tmp_path / "backup.json")
    assert not {"curve_current", "shift_current", "heat_limit"} & set(written)
    assert written["restore_point"] == {"curve": 1.05, "room_setpoint": 17.5, "heat_limit": 14.0}


def test_existing_originals_win_over_a_recaptured_heat_limit_original_after_a_rollback(make_store, tmp_path):
    store = make_store(backup={
        "restore_point": {"heat_limit": 14.0}, "originals": {"heat_limit": 12.0},
        "heat_limit": 17.4, "heat_limit_original": 17.4,
    })
    assert store.state.originals == {"heat_limit": 12.0}
    assert store.state.restore_point == {"heat_limit": 17.4}
    store.update(last_room_target=21.0)
    assert not {"heat_limit", "heat_limit_original"} & set(load_backup(tmp_path / "backup.json"))


def test_legacy_enforce_budget_entries_win_after_a_rollback(make_store, tmp_path):
    store = make_store(backup={"write_budget": {
        "enforce:curve": {"day": "2026-10-01", "count": 5},
        "enforce:curve_current": {"day": "2026-10-02", "count": 1},
        "enforce:room_setpoint": {"day": "2026-10-02", "count": 3},
        "enforce:zone_mode": {"day": "2026-10-02", "count": 2},
    }})
    assert store.state.write_budget == {
        "enforce:curve": {"day": "2026-10-02", "count": 1},
        "enforce:room_setpoint": {"day": "2026-10-02", "count": 3},
        "enforce:zone_mode": {"day": "2026-10-02", "count": 2},
    }
    store.update(last_room_target=21.0)
    assert "enforce:curve_current" not in load_backup(tmp_path / "backup.json")["write_budget"]


def test_the_migrated_notify_key_is_the_manual_override_key():
    from smartheat_core import enforce
    from smartheat_runtime import state

    assert state._MANUAL_OVERRIDE_KEY == enforce.KEY


def test_kpi_records_from_029_move_into_levers(tmp_path):
    backup = tmp_path / "backup.json"
    backup.write_text(json.dumps({
        "manual_override": {"curve": 1.2, "shift": 17.5, "erkannt": "2026-10-02T09:00:00+02:00",
                            "rollen": {"curve_current": 1.2}, "signatur": "curve_current=1.2", "gemeldet": None},
        "manual_override_pending": {"curve": 1.2, "shift": 17.5, "erkannt": "2026-10-02T09:00:00+02:00"},
    }), encoding="utf-8")
    state = StateStore(backup, tmp_path / "failsafe_state.json").state
    assert state.manual_override == {
        "levers": {"curve": 1.2, "room_setpoint": 17.5}, "erkannt": "2026-10-02T09:00:00+02:00",
        # signatur wird wie rollen auf Hebel umgestellt (Task 8, test_migrated_signature_matches_a_recomputed_one).
        "rollen": {"curve": 1.2}, "signatur": "curve=1.2", "gemeldet": None,
    }
    assert state.manual_override_pending == {"levers": {"curve": 1.2, "room_setpoint": 17.5},
                                             "erkannt": "2026-10-02T09:00:00+02:00"}


def test_kpi_migration_is_idempotent(make_store, tmp_path):
    # Ein schon umgestelltes backup.json bleibt beim zweiten Laden gleich; ein Eintrag ohne beide alten Schluessel
    # (vor TP11: "offset") wird nicht umgestellt und faellt weiter als ungueltig weg.
    make_store(backup=CLIENT1_BACKUP_029).update(last_room_target=21.0)
    first = load_backup(tmp_path / "backup.json")
    assert first["manual_override"]["levers"] == {"curve": 1.2, "room_setpoint": 17.5}
    assert not {"curve", "shift"} & set(first["manual_override"])
    StateStore(tmp_path / "backup.json", tmp_path / "failsafe_state.json").update(last_room_target=22.0)
    assert load_backup(tmp_path / "backup.json") == {**first, "last_room_target": 22.0}


def test_legacy_kpi_values_win_over_levers_after_a_rollback(make_store):
    store = make_store(backup={"manual_override_pending": {
        "levers": {"curve": 0.8, "room_setpoint": 20.0, "heat_limit": 16.0},
        "curve": 1.2, "shift": 17.5, "erkannt": "2026-10-02T09:00:00+02:00",
    }})
    assert store.state.manual_override_pending == {
        "levers": {"curve": 1.2, "room_setpoint": 17.5, "heat_limit": 16.0}, "erkannt": "2026-10-02T09:00:00+02:00",
    }
