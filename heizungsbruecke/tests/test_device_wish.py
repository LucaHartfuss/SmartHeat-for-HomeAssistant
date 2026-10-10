"""Spec 5b 3.2/3.3: Bedienwunsch begrenzen, runden, mit basis_version senden, puffern bis zum PUBACK."""
import json
import math

import pytest

from smartheat_device import wire, wish

WALL = 1_800_000_000.0  # 2027-01-15T08:00:00+00:00


@pytest.mark.parametrize("wert, erwartet", [
    (21.0, (21.0, None)), (15, (15.0, None)), (14.0, (15.0, 14.0)), (26.3, (25.0, 26.3)), (20.3, (20.5, 20.3)),
    (20.2, (20.0, 20.2)), (20.25, (20.5, 20.25)), (5, (15.0, 5.0)),
])
def test_begrenzen(wert, erwartet):
    assert wish.begrenzen(wert) == erwartet


@pytest.mark.parametrize("wert", [math.nan, math.inf, True, "20", None, 10**400])
def test_begrenzen_refuses_what_is_no_finite_number(wert):
    with pytest.raises(ValueError):
        wish.begrenzen(wert)


def test_payload_has_the_contract_fields_and_omits_unknown_extras():
    plain = wish.neu(21.5, None, basis_version=3, uhr_synchron=True, herkunft=None, durch=None, wall=WALL)
    assert set(plain.payload()) == set(wire.WISH_FIELDS)
    assert plain.payload()["ts"] == "2027-01-15T08:00:00+00:00" and plain.payload()["werte"] == {"raum_soll": 21.5}
    limited = wish.neu(15.0, 14.0, basis_version=3, uhr_synchron=False, herkunft="thermostat", durch="nutzer",
                       wall=WALL)
    assert set(limited.payload()) == set(wire.WISH_FIELDS) | set(wire.WISH_OPTIONAL)
    assert (limited.payload()["roh"], limited.payload()["uhr_synchron"]) == (14.0, False)
    assert len(plain.wish_id) <= 64 and plain.wish_id != limited.wish_id


@pytest.mark.parametrize("herkunft, durch", [("app", None), (None, "katze")])
def test_origin_and_actor_are_contract_values(herkunft, durch):
    with pytest.raises(ValueError):
        wish.neu(21.0, None, basis_version=0, uhr_synchron=True, herkunft=herkunft, durch=durch, wall=WALL)


def _wunsch(value=21.0):
    return wish.neu(value, None, basis_version=1, uhr_synchron=True, herkunft="thermostat", durch=None, wall=WALL)


def test_the_buffer_keeps_only_the_latest_and_survives_a_restart(tmp_path):
    puffer = wish.WunschPuffer(tmp_path)
    puffer.setzen(_wunsch(20.0))
    latest = _wunsch(21.0)
    puffer.setzen(latest)
    assert wish.WunschPuffer(tmp_path).wunsch == latest and puffer.offen()


def test_the_buffer_is_cleared_only_by_the_matching_puback(tmp_path):
    puffer = wish.WunschPuffer(tmp_path)
    puffer.setzen(_wunsch())
    puffer.gesendet(5)
    assert not puffer.offen()
    assert not puffer.bestaetigt(4) and puffer.wunsch is not None
    assert puffer.bestaetigt(5) and puffer.wunsch is None
    assert not (tmp_path / wish.WUNSCH_FILE).exists()


def test_a_lost_connection_sends_again(tmp_path):
    puffer = wish.WunschPuffer(tmp_path)
    puffer.setzen(_wunsch())
    puffer.gesendet(5)
    puffer.verbindung_verloren()
    assert puffer.offen() and not puffer.bestaetigt(5)


def test_a_new_wish_replaces_one_in_flight(tmp_path):
    puffer = wish.WunschPuffer(tmp_path)
    puffer.setzen(_wunsch(20.0))
    puffer.gesendet(5)
    puffer.setzen(_wunsch(22.0))
    assert not puffer.bestaetigt(5) and puffer.wunsch.raum_soll == 22.0


def test_an_unsent_publish_keeps_the_wish_open(tmp_path):
    puffer = wish.WunschPuffer(tmp_path)
    puffer.setzen(_wunsch())
    puffer.gesendet(None)  # keine Verbindung
    assert puffer.offen()


def test_a_broken_buffer_file_is_empty(tmp_path):
    (tmp_path / wish.WUNSCH_FILE).write_text(json.dumps({"wish_id": 5}))
    assert wish.WunschPuffer(tmp_path).wunsch is None


def test_texts():
    assert wish.begrenzt_text(14.0, 15.0, "thermostat") == "Thermostat auf 14 °C gestellt, SmartHeat regelt auf 15 °C"
    assert wish.begrenzt_text(20.3, 20.5, "soll_entity") == (
        "Wunschtemperatur auf 20,3 °C gestellt, SmartHeat regelt auf 20,5 °C")
    assert wish.soll_quelle_aus_text(21.0) == (
        "Thermostat ist aus, SmartHeat regelt weiter auf 21 °C; Heizung aus über die Sommersperre bzw. im Portal")
    assert {wish.BEGRENZT_KEY, wish.SOLL_QUELLE_AUS_KEY} <= set(wire.MELDUNGEN)
