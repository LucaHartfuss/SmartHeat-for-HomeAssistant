import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from smartheat_runtime import entitlement

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone(timedelta(hours=2)))


def test_mark_inactive_sets_once_and_is_idempotent(tmp_path):
    path = tmp_path / "entitlement_state.json"

    first, newly = entitlement.mark_inactive(path, NOW)
    second, newly_again = entitlement.mark_inactive(path, NOW + timedelta(days=3))

    assert (first, newly) == (NOW, True)
    assert (second, newly_again) == (NOW, False)
    assert json.loads(path.read_text()) == {"inactive_since": NOW.isoformat()}


@pytest.mark.parametrize("content", ["{kaputt", "[]", '{"inactive_since": "gestern"}',
                                     '{"inactive_since": "2026-09-01T12:00:00"}', '{"inactive_since": 5}'])
def test_corrupt_state_is_treated_as_newly_inactive(tmp_path, content):
    path = tmp_path / "entitlement_state.json"
    path.write_text(content)

    assert entitlement.load_inactive_since(path) is None
    since, newly = entitlement.mark_inactive(path, NOW)
    assert (since, newly) == (NOW, True)


def test_load_inactive_since_missing_file(tmp_path):
    assert entitlement.load_inactive_since(tmp_path / "fehlt.json") is None


def test_clear_removes_state_and_tolerates_missing_file(tmp_path):
    path = tmp_path / "entitlement_state.json"
    entitlement.mark_inactive(path, NOW)

    entitlement.clear(path)
    entitlement.clear(path)

    assert not path.exists()


def test_grace_arithmetic():
    assert entitlement.grace_end(NOW) == NOW + timedelta(days=30)
    assert entitlement.grace_expired(NOW, NOW + timedelta(days=30) - timedelta(seconds=1)) is False
    assert entitlement.grace_expired(NOW, NOW + timedelta(days=30)) is True
    # Unterschiedliche Zeitzonen-Offsets (Sommer-/Winterzeit) muessen korrekt vergleichen.
    later_utc = (NOW + timedelta(days=31)).astimezone(UTC)
    assert entitlement.grace_expired(NOW, later_utc) is True

