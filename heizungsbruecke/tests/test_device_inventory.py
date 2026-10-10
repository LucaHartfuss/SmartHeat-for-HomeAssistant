"""Spec 5b 2/3.4: Inventur in Teilen unter der Nachrichtengrenze (AN-10: 120 KB)."""
import json

import pytest

from smartheat_device import inventory, wire


def _server_merge(parts):
    """Zusammenfuehren wie der Dienst (Plan S2 Task 5, geraete/inventur.merge)."""
    merged = {}
    for part in parts:
        for key, value in part.items():
            if key in merged and isinstance(merged[key], list) and isinstance(value, list):
                merged[key] = merged[key] + value
            else:
                merged.setdefault(key, value)
    return merged


def test_a_small_inventory_is_one_part():
    assert inventory.nachrichten("c1", {"k": [1]}) == [
        {"schema": 1, "command_id": "c1", "teil": 1, "teile": 1, "daten": {"k": [1]}}]


def test_without_command_id():
    assert "command_id" not in inventory.nachrichten(None, {"k": [1]})[0]


def test_large_lists_are_split_and_single_values_stay_in_the_first_part():
    daten = {"treiber": "simulation", "proben": [{"i": i, "x": "y" * 1000} for i in range(300)], "leer": []}
    teile = inventory.teilen(daten, limit=50_000)
    assert len(teile) > 1 and teile[0]["treiber"] == "simulation" and teile[0]["leer"] == []
    assert all("treiber" not in teil for teil in teile[1:])
    assert all(inventory.groesse(teil) <= 50_000 for teil in teile)
    assert _server_merge(teile) == daten


def test_two_lists_keep_their_order():
    daten = {"a": [{"x": "y" * 900, "i": i} for i in range(40)], "b": [{"x": "z" * 900, "i": i} for i in range(40)]}
    assert _server_merge(inventory.teilen(daten, limit=20_000)) == daten


def test_every_message_fits_the_broker_limit():
    daten = {"proben": [{"i": i, "x": "y" * 1000} for i in range(500)]}
    messages = inventory.nachrichten("c1", daten)
    assert len(messages) > 1 and [m["teil"] for m in messages] == list(range(1, len(messages) + 1))
    assert all(len(json.dumps(m, separators=(",", ":")).encode()) <= wire.MAX_MESSAGE_BYTES for m in messages)


def test_too_large_inventories_are_refused():
    with pytest.raises(inventory.InventurZuGross):
        inventory.teilen({"proben": ["x" * 200]}, limit=100)
    with pytest.raises(inventory.InventurZuGross):
        inventory.teilen({"proben": ["x" * 60] * 20}, limit=100)  # je Teil ein Eintrag: mehr als TEILE_MAX Teile
