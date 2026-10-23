"""Inventur in Teilen unter der Nachrichtengrenze (AN-10: 120 KB)."""
import json
import time

import pytest

from smartheat_device import inventory, nachrichten, wire


def _gesendet(message) -> int:
    """Bytes wie der Sender sie schreibt (Link.publish): kompakt, ensure_ascii=False."""
    return len(json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode())


def _server_merge(parts):
    """Zusammenfuehren wie der Dienst (geraete/inventur.merge)."""
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


def test_every_part_has_exactly_the_contract_fields():
    daten = {"proben": [{"i": i, "x": "y" * 1000} for i in range(300)]}
    teile = inventory.nachrichten("c1", daten)
    assert len(teile) > 1 and all(set(teil) == set(wire.INVENTORY_FIELDS) for teil in teile)


def test_an_inventory_part_outside_the_contract_is_a_contract_error(monkeypatch):
    monkeypatch.setattr(wire, "INVENTORY_FIELDS", (*wire.INVENTORY_FIELDS, "neu"))
    with pytest.raises(nachrichten.Vertragsfehler) as fehlt:
        inventory.nachrichten("c1", {"k": [1]})
    assert isinstance(fehlt.value, ValueError) and "neu" in str(fehlt.value)


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
    assert all(_gesendet(m) <= wire.MAX_MESSAGE_BYTES for m in messages)


def test_non_ascii_inventories_fit_in_the_sent_form_and_merge_back():
    daten = {"proben": [{"i": i, "x": "ä€" * 60} for i in range(2500)], "name": "Küche €"}
    messages = inventory.nachrichten("c1", daten)
    assert len(messages) > 1
    assert all(_gesendet(m) <= wire.MAX_MESSAGE_BYTES for m in messages)
    assert _server_merge([m["daten"] for m in messages]) == daten


def _teilen_neu_serialisiert(daten, limit):
    """Referenz: dieselbe Aufteilung, aber mit vollstaendiger Neuberechnung je Eintrag (langsam, offensichtlich richtig)."""
    teile = [{k: v for k, v in daten.items() if not isinstance(v, list) or not v}]
    for key, values in daten.items():
        if not isinstance(values, list) or not values:
            continue
        for item in values:
            kandidat = {**teile[-1], key: [*teile[-1].get(key, []), item]}
            if inventory.groesse(kandidat) <= limit:
                teile[-1] = kandidat
            else:
                teile.append({key: [item]})
    return teile


def test_the_running_byte_count_matches_a_full_measurement():
    daten = {
        "a": [{"i": i, "x": "ä" * (i % 37)} for i in range(120)], "leer": [], "treiber": "sim €",
        "b": [i * 1000 for i in range(150)], "c": [[i, "€"] for i in range(90)], "d": [], "e": ["z" * 70] * 30,
    }
    for limit in (2000, 3000, 6000):
        teile = inventory.teilen(daten, limit=limit)
        assert teile == _teilen_neu_serialisiert(daten, limit)
        assert all(inventory.groesse(teil) <= limit for teil in teile)
        assert _server_merge(teile) == daten


def test_an_oversize_inventory_is_refused_quickly():
    start = time.monotonic()
    with pytest.raises(inventory.InventurZuGross):
        inventory.teilen({"proben": [{"i": i} for i in range(100_000)]}, limit=200)
    assert time.monotonic() - start < 1.0


def test_too_large_inventories_are_refused():
    with pytest.raises(inventory.InventurZuGross):
        inventory.teilen({"proben": ["x" * 200]}, limit=100)
    with pytest.raises(inventory.InventurZuGross):
        inventory.teilen({"proben": ["x" * 60] * 20}, limit=100)  # je Teil ein Eintrag: mehr als TEILE_MAX Teile
