"""Bedienwunsch begrenzen, runden, mit basis_version senden, puffern bis zum PUBACK."""
import json
import math

import pytest

from smartheat_device import nachrichten, wire, wish

WALL = 1_800_000_000.0  # 2027-01-15T08:00:00+00:00


@pytest.mark.parametrize("wert, erwartet", [
    (21.0, (21.0, None)), (15, (15.0, None)), (14.0, (15.0, 14.0)), (26.3, (25.0, 26.3)), (20.3, (20.5, 20.3)),
    (20.2, (20.0, 20.2)), (20.25, (20.5, 20.25)), (5, (15.0, 5.0)),
    (1e308, (25.0, 1e308)), (10**308, (25.0, float(10**308))), (-1e308, (15.0, -1e308)),
    (-(10**308), (15.0, -float(10**308))), (24.9, (25.0, 24.9)), (25.2, (25.0, 25.2)),
])
def test_begrenzen(wert, erwartet):
    assert wish.begrenzen(wert) == erwartet


@pytest.mark.parametrize("wert", [math.nan, math.inf, True, "20", None, 10**400])
def test_begrenzen_refuses_what_is_no_finite_number(wert):
    with pytest.raises(ValueError):
        wish.begrenzen(wert)


def test_payload_has_the_contract_fields_and_omits_unknown_extras():
    plain = wish.neu(21.5, None, kreis=1, basis_version=3, uhr_synchron=True, herkunft=None, durch=None, wall=WALL)
    assert set(plain.payload()) == set(wire.WISH_FIELDS)
    assert plain.payload()["ts"] == "2027-01-15T08:00:00+00:00" and plain.payload()["werte"] == {"raum_soll": 21.5}
    limited = wish.neu(15.0, 14.0, kreis=2, basis_version=3, uhr_synchron=False, herkunft="thermostat", durch="nutzer",
                       wall=WALL)
    assert set(limited.payload()) == set(wire.WISH_FIELDS) | set(wire.WISH_OPTIONAL)
    assert (limited.payload()["roh"], limited.payload()["uhr_synchron"]) == (14.0, False)
    assert len(plain.wish_id) <= 64 and plain.wish_id != limited.wish_id


def test_the_values_have_exactly_the_contract_keys():
    assert set(_wunsch().payload()["werte"]) == set(wire.WISH_WERTE)


@pytest.mark.parametrize("feld, wert", [("WISH_FIELDS", "neu"), ("WISH_WERTE", "neu")])
def test_a_wish_outside_the_contract_is_a_contract_error(monkeypatch, feld, wert):
    monkeypatch.setattr(wire, feld, (*getattr(wire, feld), wert))
    with pytest.raises(nachrichten.Vertragsfehler) as fehlt:
        _wunsch().payload()
    assert wert in str(fehlt.value) and "21" not in str(fehlt.value)  # Feldnamen, nie Werte


def test_the_local_messages_of_the_wish_are_contract_keys():
    assert wish.soll_quelle_aus_text(None) == (
        "Thermostat ist aus, SmartHeat regelt weiter; Heizung aus über die Sommersperre bzw. im Portal")
    assert {wish.BEGRENZT_KEY, wish.SOLL_QUELLE_AUS_KEY} <= set(wire.MELDUNGEN)


@pytest.mark.parametrize("herkunft, durch", [("app", None), (None, "katze")])
def test_origin_and_actor_are_contract_values(herkunft, durch):
    with pytest.raises(ValueError):
        wish.neu(21.0, None, kreis=1, basis_version=0, uhr_synchron=True, herkunft=herkunft, durch=durch, wall=WALL)
    with pytest.raises(ValueError):
        wish.pruefen_herkunft(herkunft, durch)


@pytest.mark.parametrize("herkunft", [None, "thermostat", "soll_entity", "ha"])
@pytest.mark.parametrize("durch", [None, "nutzer", "automation"])
def test_every_contract_origin_and_actor_passes_the_check(herkunft, durch):
    assert wish.pruefen_herkunft(herkunft, durch) is None


def _wunsch(value=21.0):
    return wish.neu(value, None, kreis=1, basis_version=1, uhr_synchron=True, herkunft="thermostat", durch=None, wall=WALL)


def test_the_buffer_keeps_only_the_latest_and_survives_a_restart(tmp_path):
    puffer = wish.WunschPuffer(tmp_path)
    puffer.setzen(_wunsch(20.0))
    latest = _wunsch(21.0)
    puffer.setzen(latest)
    assert wish.WunschPuffer(tmp_path).wunsch(1) == latest and puffer.offen(1)


def test_the_buffer_is_cleared_only_by_the_matching_puback(tmp_path):
    puffer = wish.WunschPuffer(tmp_path)
    puffer.setzen(_wunsch())
    puffer.gesendet(1, 5)
    assert not puffer.offen(1)
    assert not puffer.bestaetigt(4) and puffer.wunsch(1) is not None
    assert puffer.bestaetigt(5) and puffer.wunsch(1) is None
    assert not (tmp_path / wish.wunsch_datei(1)).exists()


class _Uhr:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_a_puback_with_an_error_code_sends_the_wish_again_only_after_60_seconds(tmp_path):
    uhr = _Uhr()
    puffer = wish.WunschPuffer(tmp_path, uhr)
    puffer.setzen(_wunsch())
    puffer.gesendet(1, 5)
    assert not puffer.offen(1)
    puffer.abgelehnt(6)  # fremde mid: nichts, keine Sperre
    assert not puffer.offen(1)
    puffer.abgelehnt(5)
    assert puffer.wunsch(1) is not None and not puffer.offen(1)  # nicht bestaetigt, aber gesperrt
    uhr.now += wish.WUNSCH_NACH_ABLEHNUNG_SECONDS - 1
    assert not puffer.offen(1)
    uhr.now += 1
    assert puffer.offen(1)  # nach 60 s geht er erneut hinaus


def test_a_new_wish_and_a_lost_connection_lift_the_block_after_a_rejection(tmp_path):
    uhr = _Uhr()
    puffer = wish.WunschPuffer(tmp_path, uhr)
    for aufheben in (lambda: puffer.setzen(_wunsch()), puffer.verbindung_verloren):
        puffer.setzen(_wunsch())
        puffer.gesendet(1, 5)
        puffer.abgelehnt(5)
        assert not puffer.offen(1)
        aufheben()
        assert puffer.offen(1)


def test_a_lost_connection_sends_again(tmp_path):
    puffer = wish.WunschPuffer(tmp_path)
    puffer.setzen(_wunsch())
    puffer.gesendet(1, 5)
    puffer.verbindung_verloren()
    assert puffer.offen(1) and not puffer.bestaetigt(5)


def test_a_new_wish_replaces_one_in_flight(tmp_path):
    puffer = wish.WunschPuffer(tmp_path)
    puffer.setzen(_wunsch(20.0))
    puffer.gesendet(1, 5)
    puffer.setzen(_wunsch(22.0))
    assert not puffer.bestaetigt(5) and puffer.wunsch(1).raum_soll == 22.0


def test_an_unsent_publish_keeps_the_wish_open(tmp_path):
    puffer = wish.WunschPuffer(tmp_path)
    puffer.setzen(_wunsch())
    puffer.gesendet(1, None)  # keine Verbindung
    assert puffer.offen(1)


def test_a_broken_buffer_file_is_empty(tmp_path):
    (tmp_path / wish.wunsch_datei(1)).write_text(json.dumps({"wish_id": 5}))
    assert wish.WunschPuffer(tmp_path).wunsch(1) is None


def test_texts():
    assert wish.begrenzt_text(14.0, 15.0, "thermostat") == "Thermostat auf 14 °C gestellt, SmartHeat regelt auf 15 °C"
    assert wish.begrenzt_text(20.3, 20.5, "soll_entity") == (
        "Wunschtemperatur auf 20,3 °C gestellt, SmartHeat regelt auf 20,5 °C")
    assert wish.soll_quelle_aus_text(21.0) == (
        "Thermostat ist aus, SmartHeat regelt weiter auf 21 °C; Heizung aus über die Sommersperre bzw. im Portal")
    assert wish.soll_quelle_aus_text(None) == (
        "Thermostat ist aus, SmartHeat regelt weiter; Heizung aus über die Sommersperre bzw. im Portal")
    assert {wish.BEGRENZT_KEY, wish.SOLL_QUELLE_AUS_KEY} <= set(wire.MELDUNGEN)


def test_the_wish_carries_its_circuit():
    assert _wunsch().payload()["kreis"] == 1
    zweiter = wish.neu(21.0, None, kreis=2, basis_version=1, uhr_synchron=True, herkunft=None, durch=None, wall=WALL)
    assert zweiter.payload()["kreis"] == 2
    grenze = wish.neu(21.0, None, kreis=2**31 - 1, basis_version=1, uhr_synchron=True, herkunft=None, durch=None,
                      wall=WALL)
    assert grenze.payload()["kreis"] == 2**31 - 1


@pytest.mark.parametrize("kreis", [0, -1, True, 1.0, "1", None, 2**31, 10**400])
def test_a_wrong_circuit_is_refused(kreis):
    with pytest.raises(ValueError):
        wish.pruefen_kreis(kreis)
    with pytest.raises(ValueError):
        wish.neu(21.0, None, kreis=kreis, basis_version=1, uhr_synchron=True, herkunft=None, durch=None, wall=WALL)


def test_a_buffered_wish_without_circuit_from_before_is_dropped(tmp_path):
    alt = {"wish_id": "w", "basis_version": 1, "ts": "t", "uhr_synchron": True, "raum_soll": 21.0}
    (tmp_path / wish.wunsch_datei(1)).write_text(json.dumps(alt))
    assert wish.WunschPuffer(tmp_path).wunsch(1) is None
    (tmp_path / wish.wunsch_datei(1)).write_text(json.dumps({**alt, "kreis": 0}))
    assert wish.WunschPuffer(tmp_path).wunsch(1) is None
    (tmp_path / wish.wunsch_datei(2)).write_text(json.dumps({**alt, "kreis": 2}))
    assert wish.WunschPuffer(tmp_path).wunsch(2).kreis == 2


def test_a_buffer_file_of_another_circuit_than_its_name_is_dropped(tmp_path):
    alt = {"wish_id": "w", "basis_version": 1, "ts": "t", "uhr_synchron": True, "raum_soll": 21.0, "kreis": 2}
    (tmp_path / wish.wunsch_datei(1)).write_text(json.dumps(alt))
    puffer = wish.WunschPuffer(tmp_path)
    assert puffer.wunsch(1) is None and puffer.wunsch(2) is None and puffer.kreise() == []


def test_the_old_single_buffer_file_is_never_read(tmp_path):
    alt = {"wish_id": "w", "kreis": 1, "basis_version": 1, "ts": "t", "uhr_synchron": True, "raum_soll": 21.0}
    (tmp_path / "wunsch.json").write_text(json.dumps(alt))
    assert wish.WunschPuffer(tmp_path).kreise() == []


def _wunsch_von(kreis, value):
    return wish.neu(value, None, kreis=kreis, basis_version=1, uhr_synchron=True, herkunft=None, durch=None, wall=WALL)


@pytest.mark.parametrize("zuerst, danach", [(1, 2), (2, 1)])
def test_a_wish_of_one_circuit_never_replaces_the_buffered_wish_of_another(tmp_path, zuerst, danach):
    puffer = wish.WunschPuffer(tmp_path)
    erster, zweiter = _wunsch_von(zuerst, 20.0), _wunsch_von(danach, 23.0)
    puffer.setzen(erster)
    puffer.setzen(zweiter)
    assert (puffer.wunsch(zuerst), puffer.wunsch(danach)) == (erster, zweiter)
    assert puffer.kreise() == [1, 2] and puffer.offen(1) and puffer.offen(2)
    neu = wish.WunschPuffer(tmp_path)  # beide ueberstehen einen Neustart
    assert (neu.wunsch(zuerst), neu.wunsch(danach)) == (erster, zweiter)


def test_puback_and_rejection_act_only_on_the_circuit_whose_wish_is_in_flight(tmp_path):
    uhr = _Uhr()
    puffer = wish.WunschPuffer(tmp_path, uhr)
    puffer.setzen(_wunsch_von(1, 20.0))
    puffer.setzen(_wunsch_von(2, 23.0))
    puffer.gesendet(1, 5)
    puffer.gesendet(2, 6)
    puffer.abgelehnt(6)
    assert not puffer.offen(1) and not puffer.offen(2)  # Kreis 1 unterwegs, Kreis 2 gesperrt
    assert puffer.bestaetigt(5) and puffer.wunsch(1) is None and puffer.wunsch(2) is not None
    assert not (tmp_path / wish.wunsch_datei(1)).exists() and (tmp_path / wish.wunsch_datei(2)).exists()
    uhr.now += wish.WUNSCH_NACH_ABLEHNUNG_SECONDS
    assert puffer.offen(2) and not puffer.offen(1)
    puffer.gesendet(2, 7)
    puffer.verbindung_verloren()
    assert puffer.offen(2) and not puffer.bestaetigt(7)
