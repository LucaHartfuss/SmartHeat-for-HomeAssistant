"""Rastung des Sicherheitssatzes je Kreis; eine kaputte oder altformatige Datei sperrt."""
import contextlib
import json
import logging
import os
import stat
import threading

import pytest
from rastung_beispiele import einzel

from smartheat_core import rastung

PAAR = ("vaillant_vrc720", "Heizkoerper")
SCHREIBZIELE = ("treiber=vicare", 'parameter={"heizkreis": 0}')


def _gw_bindung(heizkreis=0, ieee=None, treiber="vicare"):
    return {"treiber": {"id": treiber, "parameter": {"heizkreis": heizkreis}}, "thermostat": ieee}


def _ha_bindung(nummer=0, config_entry="abc", **rollen):
    rollen = {"room_target": "climate.wohnzimmer", "curve": "number.kurve", "outdoor_temp": "sensor.aussen", **rollen}
    kreis = {"plattform": "mypyllant", "anlage": "zuhause", "nummer": str(nummer)}
    if config_entry:
        kreis["config_entry"] = config_entry
    return {"kreis": kreis, "rollen": rollen}


def _konf(*kreise, hebelsatz=PAAR[0], verteilsystem=PAAR[1]):
    """Konfiguration mit Kreisen (nummer, bindung)."""
    return {"kreise": [{"kreis": nummer, "name": f"k{nummer}", "bindung": bindung, "raeume": [],
                        "hebelsatz": hebelsatz, "verteilsystem": verteilsystem, "abo": False}
                       for nummer, bindung in kreise]}


def _lock(*eintraege):
    """Rastung aus (kreis, ziele, soll_entity)-Tripeln fuer das Paar PAAR."""
    return rastung.Rastung(tuple(rastung.KreisRastung(k, *PAAR, tuple(sorted(z)), s) for k, z, s in eintraege))


def _gw_lock(*kreise):
    """Rastung, wie sie aus einer Gateway-Konfiguration entsteht: (nummer, heizkreis)."""
    return _lock(*[(n, rastung.ziele(_gw_bindung(h)), None) for n, h in kreise])


def _gw_konf(*kreise):
    return _konf(*[(n, _gw_bindung(h)) for n, h in kreise])


# --- Datei -----------------------------------------------------------------------------------------------------

def test_missing_file_means_no_lock(tmp_path):
    assert rastung.lesen(tmp_path / rastung.DATEI) is None
    assert rastung.passt(None, _gw_konf((1, 0)))


def test_written_lock_is_read_back_privately(tmp_path):
    pfad = tmp_path / rastung.DATEI
    lock = _lock((1, SCHREIBZIELE, None), (2, ("kreis=x", "curve=number.a"), "climate.b"))
    rastung.schreiben(pfad, lock, "2026-10-14T12:00:00+02:00")
    assert rastung.lesen(pfad) == lock
    roh = json.loads(pfad.read_text())
    assert roh["seit"] == "2026-10-14T12:00:00+02:00"
    assert [k["kreis"] for k in roh["kreise"]] == [1, 2]
    assert roh["kreise"][1]["soll_entity"] == "climate.b"
    assert stat.S_IMODE(pfad.stat().st_mode) == 0o600


@pytest.mark.parametrize("lock", [
    rastung.Rastung(()), einzel(ziele=()), einzel(ziele=("b=1", "a=1")), einzel(ziele=("a=1", "a=1")),
    einzel(ziele=("",)), einzel(kreis=0),
])
def test_a_lock_the_reader_would_block_cannot_be_written(tmp_path, lock):
    with pytest.raises(ValueError):
        rastung.schreiben(tmp_path / rastung.DATEI, lock, "2026-10-14T12:00:00+02:00")
    assert not (tmp_path / rastung.DATEI).exists()


def _eintrag(**ueber):
    return {"kreis": 1, "hebelsatz": PAAR[0], "verteilsystem": PAAR[1], "ziele": ["a=1"], "soll_entity": None, **ueber}


@pytest.mark.parametrize("inhalt", [
    "", "{", "[]", '{"kreise": []}', '{"kreise": {}}', '{"kreise": [1]}', '{"kreise": [{}]}',
    json.dumps({"kreise": [_eintrag(hebelsatz=1)]}),
    json.dumps({"kreise": [_eintrag(verteilsystem="Kachelofen")]}),
    json.dumps({"kreise": [_eintrag(hebelsatz="", verteilsystem="")]}),
    json.dumps({"kreise": [_eintrag(kreis=0)]}), json.dumps({"kreise": [_eintrag(kreis=True)]}),
    json.dumps({"kreise": [_eintrag(kreis="1")]}),
    json.dumps({"kreise": [_eintrag(ziele=[])]}), json.dumps({"kreise": [_eintrag(ziele="a=1")]}),
    json.dumps({"kreise": [_eintrag(ziele=["b=1", "a=1"])]}),   # nicht in kanonischer Reihenfolge
    json.dumps({"kreise": [_eintrag(ziele=["a=1", "a=1"])]}),   # doppelt
    json.dumps({"kreise": [_eintrag(ziele=[""])]}), json.dumps({"kreise": [_eintrag(ziele=[1])]}),
    json.dumps({"kreise": [_eintrag(soll_entity=3)]}),
    json.dumps({"kreise": [{k: v for k, v in _eintrag().items() if k != "soll_entity"}]}),
    "[" * 50000, "x" * 100000,
])
def test_a_broken_lock_blocks_every_configuration(tmp_path, inhalt):
    pfad = tmp_path / rastung.DATEI
    pfad.write_text(inhalt)
    gelesen = rastung.lesen(pfad)
    assert gelesen == rastung.UNBEKANNT
    assert not rastung.passt(gelesen, _gw_konf((1, 0)))
    assert not rastung.hebel_passt(gelesen, *PAAR)


def test_the_old_one_pair_format_blocks_loudly_with_its_own_code(tmp_path, caplog):
    pfad = tmp_path / rastung.DATEI
    pfad.write_text(json.dumps({"hebelsatz": PAAR[0], "verteilsystem": PAAR[1], "seit": "x"}))
    with caplog.at_level(logging.ERROR, logger="smartheat_core.rastung"):
        gelesen = rastung.lesen(pfad)
    assert gelesen == rastung.UNBEKANNT
    assert gelesen.sperre == "altes_format"
    assert not rastung.passt(gelesen, _gw_konf((1, 0)))
    assert "altes_format" in caplog.text
    assert PAAR[0] not in caplog.text
    assert "Migration" in rastung.text_zu(gelesen)
    assert "festgelegt" in rastung.text_zu(gelesen)


@pytest.mark.parametrize(("inhalt", "code"), [
    ("{", "nicht_lesbar"), ('{"kreise": []}', "unvollstaendig"),
    (json.dumps({"kreise": [_eintrag(verteilsystem="Kachelofen")]}), "ohne_sicherheitswerte"),
])
def test_other_blocks_carry_a_code_but_no_names_in_the_log(tmp_path, caplog, inhalt, code):
    pfad = tmp_path / rastung.DATEI
    pfad.write_text(inhalt)
    with caplog.at_level(logging.ERROR, logger="smartheat_core.rastung"):
        gelesen = rastung.lesen(pfad)
    assert gelesen.sperre == code
    assert code in caplog.text
    assert "Kachelofen" not in caplog.text
    assert "festgelegt" in rastung.text_zu(gelesen)
    assert "Migration" not in rastung.text_zu(gelesen)


def test_an_unreadable_lock_blocks(tmp_path):
    pfad = tmp_path / rastung.DATEI
    pfad.mkdir()  # IsADirectoryError ist ein OSError
    assert rastung.lesen(pfad) == rastung.UNBEKANNT


def test_a_symlink_counts_as_manipulated_even_to_a_valid_lock(tmp_path):
    echt = tmp_path / "echt.json"
    rastung.schreiben(echt, _gw_lock((1, 0)), "2026-10-14T12:00:00+02:00")
    pfad = tmp_path / rastung.DATEI
    pfad.symlink_to(echt)
    assert rastung.lesen(pfad) == rastung.UNBEKANNT


def test_a_dangling_symlink_blocks_instead_of_meaning_no_lock(tmp_path):
    pfad = tmp_path / rastung.DATEI
    pfad.symlink_to(tmp_path / "gibt-es-nicht.json")
    assert rastung.lesen(pfad) == rastung.UNBEKANNT


def _datei_mit_groesse(pfad, groesse):
    """Gueltiges JSON mit gueltigem Satz und genau `groesse` Bytes (die schliessende Klammer ist das letzte Byte)."""
    kopf = json.dumps({"kreise": [_eintrag()]})[:-1] + ', "fuell": "'
    ende = '"}'
    pfad.write_text(kopf + "x" * (groesse - len(kopf) - len(ende)) + ende)
    assert pfad.stat().st_size == groesse


@pytest.mark.parametrize(("groesse", "gesperrt"), [
    (32 * 1024 - 1, False), (32 * 1024, False),
    (32 * 1024 + 1, False),   # bis 32 KiB + 1 Byte wird vollstaendig gelesen
    (32 * 1024 + 2, True),    # das letzte Byte wird nicht mehr gelesen: abgeschnitten, gesperrt
])
def test_the_lock_file_is_read_up_to_a_hard_size_limit(tmp_path, groesse, gesperrt):
    pfad = tmp_path / rastung.DATEI
    _datei_mit_groesse(pfad, groesse)
    gelesen = rastung.lesen(pfad)
    assert (gelesen == rastung.UNBEKANNT) is gesperrt


def test_four_circles_with_long_targets_fit_into_the_size_limit(tmp_path):
    lange = tuple(f"{rolle}=number.{'x' * 200}_{rolle}" for rolle in rastung.SCHREIBROLLEN_HA)
    lock = _lock(*[(n, ("kreis=" + "y" * 80, *lange), "climate." + "z" * 200) for n in range(1, 5)])
    pfad = tmp_path / rastung.DATEI
    rastung.schreiben(pfad, lock, "2026-10-14T12:00:00+02:00")
    assert rastung.lesen(pfad) == lock


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="keine FIFOs auf dieser Plattform")
def test_a_fifo_as_the_lock_file_does_not_block_and_counts_as_broken(tmp_path):
    """Open auf eine FIFO ohne Schreiber haengt ohne O_NONBLOCK; sie gilt als kaputt (jede eingerichtete
    Konfiguration abgelehnt). Daemon-Thread, damit die Suite auch bei einem Rueckfall nie haengt."""
    pfad = tmp_path / rastung.DATEI
    os.mkfifo(pfad)
    ergebnis: list = []
    thread = threading.Thread(target=lambda: ergebnis.append(rastung.lesen(pfad)), daemon=True)
    thread.start()
    try:
        thread.join(2)
        assert not thread.is_alive()
    finally:
        if thread.is_alive():  # Teardown: ein Schreiber loest das blockierte open
            with contextlib.suppress(OSError):
                os.close(os.open(pfad, os.O_WRONLY | os.O_NONBLOCK))
            thread.join(2)
    assert ergebnis == [rastung.UNBEKANNT]
    assert not rastung.passt(ergebnis[0], _gw_konf((1, 0)))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="keine FIFOs auf dieser Plattform")
def test_only_a_regular_file_counts_even_if_a_fifo_delivers_a_valid_lock(tmp_path):
    """Ein gueltiger Inhalt aus einer FIFO ist kein Sicherheitssatz auf dem Datentraeger: S_ISREG, nicht nur Parsen."""
    pfad = tmp_path / rastung.DATEI
    os.mkfifo(pfad)
    halter = os.open(pfad, os.O_RDWR | os.O_NONBLOCK)  # haelt Leser und Schreiber, nichts blockiert
    try:
        os.write(halter, json.dumps({"kreise": [_eintrag()]}).encode())
        assert rastung.lesen(pfad) == rastung.UNBEKANNT
    finally:
        os.close(halter)


# --- Schreibziele und Soll-Entity -------------------------------------------------------------------------------

def test_gateway_targets_are_the_driver_and_its_parameters_including_the_circuit():
    ziele = rastung.ziele(_gw_bindung(heizkreis=2))
    assert ziele == ('parameter={"heizkreis": 2}', "treiber=vicare")
    assert rastung.ziele(_gw_bindung(heizkreis=2)) != rastung.ziele(_gw_bindung(heizkreis=1))
    assert rastung.ziele(_gw_bindung(treiber="simulation")) != rastung.ziele(_gw_bindung())
    assert rastung.soll_entity(_gw_bindung()) is None


def test_gateway_targets_name_the_thermostat_only_when_there_is_one():
    assert "thermostat=0x00124b0001" in rastung.ziele(_gw_bindung(ieee="0x00124b0001"))
    assert not [z for z in rastung.ziele(_gw_bindung()) if z.startswith("thermostat=")]


def test_gateway_parameter_order_does_not_change_the_targets():
    a = {"treiber": {"id": "t", "parameter": {"a": 1, "b": 2}}, "thermostat": None}
    b = {"treiber": {"id": "t", "parameter": {"b": 2, "a": 1}}, "thermostat": None}
    assert rastung.ziele(a) == rastung.ziele(b)


def test_ha_targets_are_the_write_entities_and_the_circuit_with_its_config_entry():
    ziele = rastung.ziele(_ha_bindung(nummer=0, config_entry="abc"))
    assert "curve=number.kurve" in ziele
    assert not [z for z in ziele if z.startswith(("room_target=", "outdoor_temp="))]   # Soll und Lesewerte: nicht hier
    assert any(z.startswith("kreis=") and "abc" in z for z in ziele)
    assert rastung.ziele(_ha_bindung(config_entry="abc")) != rastung.ziele(_ha_bindung(config_entry="def"))
    assert rastung.ziele(_ha_bindung(nummer=0)) != rastung.ziele(_ha_bindung(nummer=1))
    assert rastung.ziele(_ha_bindung(curve="number.andere")) != rastung.ziele(_ha_bindung())
    assert rastung.ziele(_ha_bindung(curve="number.kurve::wert")) != rastung.ziele(_ha_bindung())
    assert rastung.soll_entity(_ha_bindung()) == "climate.wohnzimmer"


def test_ha_read_roles_are_not_targets_so_a_sensor_swap_stays_possible():
    assert rastung.ziele(_ha_bindung(outdoor_temp="sensor.anderer")) == rastung.ziele(_ha_bindung())
    assert rastung.ziele(_ha_bindung(room_target="climate.anderer")) == rastung.ziele(_ha_bindung())
    assert rastung.soll_entity(_ha_bindung(room_target="climate.anderer")) == "climate.anderer"


@pytest.mark.parametrize("bindung", [
    None, [], "x", {}, {"treiber": []}, {"treiber": {"id": 1, "parameter": {}}},
    {"treiber": {"id": "t", "parameter": object()}}, {"treiber": {"id": "t", "parameter": {}}, "thermostat": 3},
    {"kreis": {}, "rollen": {}}, {"kreis": {"plattform": "p", "anlage": "a", "nummer": 0}, "rollen": {"curve": 3}},
    {"kreis": {"plattform": "p", "anlage": "a", "nummer": 0}, "rollen": {"room_target": 3}},
])
def test_an_unreadable_binding_has_no_targets(bindung):
    assert rastung.ziele(bindung) is None


def test_the_write_roles_are_exactly_the_roles_the_rules_can_write():
    """Drift: eine neue Schreibrolle muss in die Rastung, sonst bleibt ihr Ziel ungerastet."""
    from heizungsbruecke.dokument import SCHREIBREGELN, WEITERE_SCHREIBZIELE, v1_rolle
    from smartheat_runtime.roles import LEVER_ROLES
    erwartet = {v1_rolle(r) for regeln in SCHREIBREGELN.values() for r in regeln}
    erwartet |= set(LEVER_ROLES) | set(WEITERE_SCHREIBZIELE)
    assert erwartet == set(rastung.SCHREIBROLLEN_HA)


# --- Aus der Konfiguration ---------------------------------------------------------------------------------------

def test_a_lock_is_built_per_circle_from_the_configuration():
    lock = rastung.aus_konfiguration(_konf((1, _ha_bindung(nummer=0)), (3, _ha_bindung(nummer=1, config_entry=None))))
    assert [k.kreis for k in lock.kreise] == [1, 3]
    assert {(k.hebelsatz, k.verteilsystem) for k in lock.kreise} == {PAAR}
    assert lock.kreise[0].soll_entity == "climate.wohnzimmer"
    assert lock.kreise[0].ziele == rastung.ziele(_ha_bindung(nummer=0))


@pytest.mark.parametrize("konfiguration", [
    {}, {"kreise": []}, _konf((1, None)), _konf((0, _gw_bindung())), _konf((True, _gw_bindung())),
    _konf((1, _gw_bindung()), (2, {"treiber": []})),
])
def test_no_lock_comes_from_an_unreadable_configuration(konfiguration):
    assert rastung.aus_konfiguration(konfiguration) is None


# --- Passen ----------------------------------------------------------------------------------------------------------

def test_the_same_configuration_fits_its_own_lock():
    konf = _gw_konf((1, 0), (2, 1))
    assert rastung.passt(rastung.aus_konfiguration(konf), konf)


@pytest.mark.parametrize(("hebelsatz", "verteilsystem", "passt"), [
    (PAAR[0], "Heizkoerper", True), (PAAR[0], "Fussbodenheizung", False),
    ("weishaupt_wwp", "Heizkoerper", False), (None, None, False), (3, "Heizkoerper", False),
])
def test_only_the_same_pair_fits(hebelsatz, verteilsystem, passt):
    lock = _gw_lock((1, 0))
    assert rastung.passt(lock, _konf((1, _gw_bindung(0)), hebelsatz=hebelsatz, verteilsystem=verteilsystem)) is passt
    assert rastung.hebel_passt(lock, hebelsatz, verteilsystem) is passt


def test_the_same_pair_with_other_targets_is_rejected():
    lock = _gw_lock((1, 0))
    assert rastung.passt(lock, _gw_konf((1, 0)))
    assert not rastung.passt(lock, _gw_konf((1, 1)))                                       # anderer Heizkreis
    assert not rastung.passt(lock, _konf((1, _gw_bindung(0, treiber="simulation"))))      # anderer Treiber
    assert not rastung.passt(lock, _konf((1, _gw_bindung(0, ieee="0x1"))))                # Thermostat dazu
    assert not rastung.passt(lock, _konf((1, _gw_bindung(0, ieee="0x1"))))


def test_a_new_circle_number_with_the_same_targets_fits():
    assert rastung.passt(_gw_lock((1, 0)), _gw_konf((7, 0)))


def test_the_soll_entity_is_part_of_the_lock():
    lock = rastung.aus_konfiguration(_konf((1, _ha_bindung())))
    assert rastung.passt(lock, _konf((1, _ha_bindung())))
    assert rastung.passt(lock, _konf((1, _ha_bindung(outdoor_temp="sensor.neu"))))
    assert not rastung.passt(lock, _konf((1, _ha_bindung(room_target="climate.neu"))))
    assert not rastung.passt(lock, _konf((1, _ha_bindung(curve="number.neu"))))
    assert not rastung.passt(lock, _konf((1, _ha_bindung(config_entry="def"))))


def test_more_circles_than_the_lock_knows_are_rejected():
    lock = _gw_lock((1, 0))
    assert not rastung.passt(lock, _gw_konf((1, 0), (2, 1)))
    assert not rastung.passt(lock, _gw_konf((1, 0), (2, 0)))   # auch mit denselben Zielen: ein Eintrag gilt einmal


def test_a_circle_without_an_entry_is_rejected():
    assert not rastung.passt(_gw_lock((1, 0), (2, 1)), _gw_konf((1, 0), (2, 2)))


def test_fewer_circles_than_the_lock_knows_fit():
    lock = _gw_lock((1, 0), (2, 1))
    assert rastung.passt(lock, _gw_konf((2, 1)))
    assert rastung.passt(lock, _gw_konf((1, 0)))


def test_circles_may_swap_their_numbers():
    assert rastung.passt(_gw_lock((1, 0), (2, 1)), _gw_konf((1, 1), (2, 0)))


@pytest.mark.parametrize("konfiguration", [{}, {"kreise": []}, {"kreise": "x"}, _konf((1, None)), _konf((1, {}))])
def test_an_empty_or_unreadable_document_never_loosens_the_lock(konfiguration):
    assert not rastung.passt(_gw_lock((1, 0)), konfiguration)
    assert not rastung.passt(rastung.UNBEKANNT, konfiguration)
    assert rastung.passt(None, konfiguration)


def test_the_pair_check_alone_knows_any_pair_of_the_lock():
    lock = rastung.Rastung((rastung.KreisRastung(1, "a", "Heizkoerper", ("x=1",), None),
                            rastung.KreisRastung(2, "b", "Fussbodenheizung", ("x=2",), None)))
    assert rastung.hebel_passt(lock, "b", "Fussbodenheizung")
    assert not rastung.hebel_passt(lock, "a", "Fussbodenheizung")
    assert rastung.hebel_passt(None, "irgendwas", "egal")


def test_the_one_circle_helper_builds_a_lock_that_survives_the_file(tmp_path):
    pfad = tmp_path / rastung.DATEI
    lock = einzel(soll_entity="climate.x", ziele=("a=1", "b=1"))
    rastung.schreiben(pfad, lock, "x")
    assert rastung.lesen(pfad) == lock


# --- Randfaelle der Form: nie ausnahmeanfaellig, nie lockernd ----------------------------------------------------------

@pytest.mark.parametrize("bindung", [
    {"kreis": {"plattform": "mypyllant", "anlage": "S", "nummer": "0"}},   # Kreis ohne Rollen
    {"rollen": {"curve": "number.kurve"}},                                 # Rollen ohne Kreis
    {"kreis": {"plattform": "mypyllant", "anlage": "S", "nummer": "0"}, "rollen": None},
    {"kreis": None, "rollen": {}},
])
def test_a_home_assistant_binding_needs_both_the_circle_and_the_roles(bindung):
    assert rastung.ziele(bindung) is None


@pytest.mark.parametrize("hebelsatz, verteilsystem", [
    (["vaillant_vrc720"], PAAR[1]), (PAAR[0], ["Heizkoerper"]), (PAAR[0], {"a": 1}), ({"a": 1}, PAAR[1]),
])
def test_a_circle_with_an_unhashable_pair_never_fits_and_never_raises(hebelsatz, verteilsystem):
    sperre = _lock((1, rastung.ziele(_gw_bindung()), None))
    konfiguration = _konf((1, _gw_bindung()))
    assert rastung.passt(sperre, konfiguration) is True
    konfiguration["kreise"][0].update(hebelsatz=hebelsatz, verteilsystem=verteilsystem)
    assert rastung.passt(sperre, konfiguration) is False


def test_the_parameters_are_compared_in_a_stable_text_whatever_their_characters():
    """Die Ziele sind Text: dieselben Parameter ergeben dieselben Ziele, andere nie (auch nicht ausserhalb von ASCII)."""
    a = rastung.ziele(_gw_bindung(treiber="vicäre"))
    assert a == rastung.ziele(_gw_bindung(treiber="vicäre")) and a != rastung.ziele(_gw_bindung(treiber="vicare"))
    assert rastung.ziele({"treiber": {"id": "x", "parameter": {"ä": "ö", "a": 1}}, "thermostat": None}) \
        == rastung.ziele({"treiber": {"id": "x", "parameter": {"a": 1, "ä": "ö"}}, "thermostat": None})
