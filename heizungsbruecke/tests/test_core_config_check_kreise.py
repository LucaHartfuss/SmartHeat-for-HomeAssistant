"""Lokale Pruefung der Konfiguration je Kreis: Anzahl, Raeume, getrennte Schreibziele und Fuehler, Wege."""
import copy

import konfig_form
import pytest
from bindung_beispiele import parameter
from geraet_helpers import gateway_konfiguration, ha_konfiguration, ha_kreis
from rastung_beispiele import fuer

from smartheat_core import config_check as cc
from smartheat_core import kreisansicht
from smartheat_device import fehler, wire

LOCK = ("viessmann_vicare", "Heizkoerper")


def _geprueft(neu, rastung=None, **kwargs):
    ablehnung = cc.pruefen(neu, fuer(rastung, neu), host=kwargs.pop("host", cc.HOST_GATEWAY), **kwargs)
    if ablehnung is not None:
        assert not hasattr(ablehnung, "text") and fehler.gueltig(ablehnung.details), ablehnung
    return ablehnung


def _ieee(nummer: int) -> str:
    return f"0x{nummer:016x}"


def _sensor(nummer: int) -> str:
    return f"zigbee:{_ieee(nummer)}:temperature"


def _gw_kreis(nummer: int, *, heizkreis=None, thermostat=None, sensor=None, **flach) -> dict:
    """Ein Gateway-Kreis mit eigenem Schreibziel (Heizkreis des Treibers), Thermostat und Fuehler."""
    bindung = {"treiber": {"id": "simulation",
                           "parameter": {"heizkreis": nummer if heizkreis is None else heizkreis}},
               "thermostat": _ieee(100 + nummer) if thermostat is None else thermostat}
    quelle = {"bindung": bindung, "raumfuehler": [sensor or _sensor(nummer)],
              "hebelsatz": "viessmann_vicare", "verteilsystem": "Heizkoerper", "abo": "aktiv", **flach}
    return konfig_form.kreis(quelle, nummer=nummer)


def _gw(*kreise, **changes) -> dict:
    return gateway_konfiguration(kreise=list(kreise), **changes)


def _kreis_ohne_raum(kreis: dict, **raum) -> dict:
    neu = copy.deepcopy(kreis)
    neu["raeume"][0].update(raum)
    return neu


def _ha_kreis(nummer: int, **bindung) -> dict:
    return ha_kreis(nummer, **bindung)


# --- Anzahl der Kreise -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("anzahl", [1, 2, 3, 4])
def test_bis_zu_vier_kreise_sind_zulaessig(anzahl):
    assert cc.KREISE_MAX == 4
    assert _geprueft(_gw(*[_gw_kreis(n) for n in range(1, anzahl + 1)]), LOCK) is None


def test_ein_fuenfter_kreis_wird_abgelehnt():
    ablehnung = _geprueft(_gw(*[_gw_kreis(n) for n in range(1, 6)]), LOCK)
    assert ablehnung == cc.Ablehnung(cc.UNGUELTIG, cc.befund("kreise", "anzahl", min=1, max=4, anzahl=5))


def test_eine_eingerichtete_anlage_ohne_kreis_wird_abgelehnt():
    ablehnung = _geprueft(_gw(), LOCK)
    assert ablehnung == cc.Ablehnung(cc.UNGUELTIG, cc.befund("kreise", "anzahl", min=1, max=4, anzahl=0))


def test_die_leere_konfiguration_hat_keine_kreise_und_ist_gueltig():
    leer = konfig_form.nach_form({"anlage": None, "mindest_software": "1.0.0"})
    assert leer["kreise"] == [] and _geprueft(leer, LOCK) is None


@pytest.mark.parametrize("nummern", [(2, 1), (0, 1), (1, 1), (1, 2**31)])
def test_kreisnummern_sind_aufsteigend_eindeutig_und_im_bereich(nummern):
    kreise = [_gw_kreis(n) for n in nummern]
    for kreis, nummer in zip(kreise, nummern, strict=True):
        kreis["kreis"] = nummer
    ablehnung = _geprueft(_gw(*kreise), LOCK)
    assert ablehnung is not None and ablehnung.grund == cc.UNGUELTIG and ablehnung.details["feld"] == "kreis"


# --- getrennte Schreibziele und Fuehler --------------------------------------------------------------------------

def test_dasselbe_schreibziel_in_zwei_kreisen_wird_abgelehnt():
    ablehnung = _geprueft(_gw(_gw_kreis(1), _gw_kreis(2, heizkreis=1)), LOCK)
    assert ablehnung == cc.Ablehnung(cc.DOPPELT_BELEGT, cc.befund("bindung", "doppelt"))


def test_dasselbe_thermostat_in_zwei_kreisen_wird_abgelehnt():
    ablehnung = _geprueft(_gw(_gw_kreis(1), _gw_kreis(2, thermostat=_ieee(101))), LOCK)
    assert ablehnung == cc.Ablehnung(cc.DOPPELT_BELEGT, cc.befund("bindung", "doppelt"))


def test_derselbe_fuehler_in_zwei_kreisen_wird_abgelehnt():
    ablehnung = _geprueft(_gw(_gw_kreis(1), _gw_kreis(2, sensor=_sensor(1))), LOCK)
    assert ablehnung == cc.Ablehnung(cc.DOPPELT_BELEGT, cc.befund("fuehler", "doppelt"))


def test_getrennte_ziele_und_fuehler_sind_zulaessig_gegenprobe():
    assert _geprueft(_gw(_gw_kreis(1), _gw_kreis(2)), LOCK) is None


def test_ein_kontrollfuehler_zaehlt_fuer_die_doppelbelegung_mit():
    kreis2 = _gw_kreis(2)
    kreis2["raeume"].append(konfig_form.raum([_sensor(1)], nummer=2, name="Bad", rolle="kontroll"))
    ablehnung = _geprueft(_gw(_gw_kreis(1), kreis2), LOCK)
    assert ablehnung == cc.Ablehnung(cc.DOPPELT_BELEGT, cc.befund("fuehler", "doppelt"))


def test_derselbe_fuehler_in_zwei_raeumen_eines_kreises_wird_abgelehnt():
    kreis = _gw_kreis(1)
    kreis["raeume"].append(konfig_form.raum([_sensor(1)], nummer=2, name="Bad", rolle="kontroll"))
    ablehnung = _geprueft(_gw(kreis), LOCK)
    assert ablehnung == cc.Ablehnung(cc.DOPPELT_BELEGT, cc.befund("fuehler", "doppelt"))


def test_zwei_ha_kreise_mit_derselben_rollen_entity_werden_abgelehnt():
    kreis2 = _ha_kreis(2)
    kreis2["bindung"]["rollen"]["curve"] = ha_konfiguration()["kreise"][0]["bindung"]["rollen"]["curve"]
    ablehnung = _geprueft(ha_konfiguration(kreise=[ha_konfiguration()["kreise"][0], kreis2]), host=cc.HOST_HA)
    assert ablehnung == cc.Ablehnung(cc.DOPPELT_BELEGT, cc.befund("bindung", "doppelt"))


def test_zwei_ha_kreise_mit_getrennten_entities_sind_zulaessig_und_der_host_sieht_jeden_kreis():
    gesehen = []
    kreise = [ha_konfiguration()["kreise"][0], _ha_kreis(2)]
    neu = ha_konfiguration(kreise=kreise)
    neu["kreise"][0]["raeume"].append(konfig_form.raum(["sensor.bad_temperatur"], nummer=2, name="Bad",
                                                       rolle="kontroll"))
    assert _geprueft(neu, host=cc.HOST_HA, treiber_pruefen=lambda k: gesehen.append(k)) is None
    sichten = [kreisansicht.erste(dokument) for dokument in gesehen]  # je Kreis ein Dokument mit einem Kreis
    assert [s["bindung"]["kreis"]["nummer"] for s in sichten] == ["0", "2"]
    # der Host prueft die Entities aller Raeume des Kreises (auch der Kontrollraeume)
    assert sichten[0]["raumfuehler"] == ["sensor.wohnzimmer_thermostat_temperatur", "sensor.bad_temperatur"]


def test_derselbe_ha_fuehler_in_zwei_kreisen_wird_abgelehnt():
    kreis2 = _ha_kreis(2)
    kreis2["raeume"][0]["fuehler"] = [ha_konfiguration()["kreise"][0]["raeume"][0]["fuehler"][0]]
    ablehnung = _geprueft(ha_konfiguration(kreise=[ha_konfiguration()["kreise"][0], kreis2]), host=cc.HOST_HA)
    assert ablehnung == cc.Ablehnung(cc.DOPPELT_BELEGT, cc.befund("fuehler", "doppelt"))


# --- Hebelsatz anlagenweit ---------------------------------------------------------------------------------------

def test_kreise_einer_anlage_haben_denselben_hebelsatz():
    kreis2 = _gw_kreis(2, hebelsatz="vaillant_vrc720")
    ablehnung = _geprueft(_gw(_gw_kreis(1), kreis2), None)
    assert ablehnung == cc.Ablehnung(cc.UNGUELTIG, cc.befund("hebelsatz", "ungueltig"))


# --- Wege --------------------------------------------------------------------------------------------------------

def test_die_wege_des_vertrags():
    assert cc.WEGE == wire.WEGE and "vaillant_ebus" not in cc.WEGE
    assert cc.WEG_DER_PLATTFORM == wire.WEG_DER_PLATTFORM and cc.WEG_DES_TREIBERS == wire.WEG_DES_TREIBERS


@pytest.mark.parametrize("weg", ["vaillant_ebus", "frei_erfunden"])
def test_ein_unbekannter_weg_wird_abgelehnt(weg):
    ablehnung = _geprueft(gateway_konfiguration(weg=weg), LOCK)
    assert ablehnung == cc.Ablehnung(cc.WEG_UNBEKANNT, cc.befund("weg", "unbekannt"))


def test_ein_treiber_ohne_weg_wird_abgelehnt():
    neu = gateway_konfiguration()
    neu["kreise"][0]["bindung"]["treiber"]["id"] = "unbekannter_treiber"
    ablehnung = _geprueft(neu, LOCK)
    assert ablehnung == cc.Ablehnung(cc.WEG_UNBEKANNT, cc.befund("weg", "unbekannt"))


def test_eine_unbekannte_ha_plattform_wird_abgelehnt():
    neu = ha_konfiguration()
    neu["kreise"][0]["bindung"]["kreis"]["plattform"] = "shelly"
    ablehnung = _geprueft(neu, host=cc.HOST_HA)
    assert ablehnung == cc.Ablehnung(cc.WEG_UNBEKANNT, cc.befund("weg", "unbekannt"))


def test_kreise_mit_verschiedenen_wegen_werden_abgelehnt():
    kreis2 = _ha_kreis(2)
    kreis2["bindung"]["kreis"]["plattform"] = "vicare"
    neu = ha_konfiguration(kreise=[ha_konfiguration()["kreise"][0], kreis2])
    assert _geprueft(neu, host=cc.HOST_HA) == cc.Ablehnung(cc.WEG_MEHRFACH, cc.befund("weg", "doppelt"))


def test_der_weg_der_anlage_stimmt_mit_dem_der_bindung_ueberein():
    neu = gateway_konfiguration(weg="vaillant_myvaillant", hebelsatz="vaillant_vrc720",
                                bindungsparameter=parameter("vaillant_vrc720"))
    ablehnung = _geprueft(neu, None)
    assert ablehnung == cc.Ablehnung(cc.WEG_MEHRFACH, cc.befund("weg", "doppelt"))


def test_der_weg_wird_vor_den_bindungsparametern_geprueft():
    ablehnung = _geprueft(gateway_konfiguration(weg="vaillant_ebus", bindungsparameter=None), LOCK)
    assert ablehnung == cc.Ablehnung(cc.WEG_UNBEKANNT, cc.befund("weg", "unbekannt"))


@pytest.mark.parametrize("hebelsatz", ["vaillant_vrc720", "weishaupt_wwp", "weishaupt_wwp_basis"])
def test_ein_hebelsatz_ohne_zeile_am_weg_der_bindung_wird_abgelehnt(hebelsatz):
    neu = gateway_konfiguration(hebelsatz=hebelsatz, bindungsparameter=parameter(hebelsatz))
    neu["kreise"][0]["bindung"]["treiber"]["id"] = "vicare_cloud"
    neu["weg"] = "viessmann_vicare"
    ablehnung = _geprueft(neu, None)
    assert ablehnung == cc.Ablehnung(cc.SICHERHEITSWERTE_FEHLEN, cc.befund("hebelsatz", "unbekannt"))


def test_ein_hebelsatz_am_eigenen_weg_wird_angenommen():
    neu = ha_konfiguration(hebelsatz="viessmann_vicare", bindungsparameter=parameter("viessmann_vicare"))
    neu["kreise"][0]["bindung"]["kreis"]["plattform"] = "vicare"
    neu["weg"] = "viessmann_vicare"
    assert _geprueft(neu, None, host=cc.HOST_HA) is None


def test_ein_ha_hebelsatz_ohne_zeile_am_weg_der_plattform_wird_abgelehnt():
    neu = ha_konfiguration()  # vaillant_vrc720
    neu["kreise"][0]["bindung"]["kreis"]["plattform"] = "vicare"
    neu["weg"] = "viessmann_vicare"
    ablehnung = _geprueft(neu, None, host=cc.HOST_HA)
    assert ablehnung == cc.Ablehnung(cc.SICHERHEITSWERTE_FEHLEN, cc.befund("hebelsatz", "unbekannt"))


def test_beide_gruende_gehoeren_zu_den_gruenden_des_vertrags():
    assert {cc.WEG_UNBEKANNT, cc.WEG_MEHRFACH} <= set(cc.GRUENDE) <= set(wire.DOKUMENT_ERRORS)


# --- Raeume ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raum, feld", [
    ({"rolle": "kontroll", "soll_quelle": True}, "soll_quelle"),
    ({"rolle": "kontroll", "versatz_k": 1.5}, "versatz_k"),
    ({"ziel_fest": 21.0}, "ziel_fest"),
    ({"soll_quelle": True, "versatz_k": 1.5}, "versatz_k"),
    ({"rolle": "unbekannt"}, "rolle"),
    ({"name": ""}, "name"),
    ({"name": "x" * 41}, "name"),
    ({"versatz_k": float("nan")}, "versatz_k"),
    ({"versatz_k": 10**400}, "versatz_k"),  # JSON kennt beliebig grosse Ganzzahlen: kein OverflowError
    ({"raum": 0}, "raum"),
    ({"fuehler": ["a"] * 9}, "fuehler"),
])
def test_ungueltige_raeume_werden_abgelehnt(raum, feld):
    kreis = _kreis_ohne_raum(_gw_kreis(1), **raum)
    if "rolle" in raum:  # ein Kontrollraum braucht daneben einen Referenzraum
        kreis["raeume"].append(konfig_form.raum([_sensor(50)], nummer=2, name="Bad"))
    ablehnung = _geprueft(_gw(kreis), LOCK)
    assert ablehnung is not None and ablehnung.grund == cc.UNGUELTIG and ablehnung.details["feld"] == feld


def test_ein_kreis_braucht_mindestens_einen_referenzfuehler():
    kreis = _gw_kreis(1)
    kreis["raeume"][0]["rolle"] = "kontroll"
    ablehnung = _geprueft(_gw(kreis), LOCK)
    assert ablehnung == cc.Ablehnung(cc.UNGUELTIG, cc.befund("fuehler", "anzahl", min=1, max=8, anzahl=0))


def test_ein_kreis_hat_hoechstens_acht_referenzfuehler():
    kreis = _gw_kreis(1)
    kreis["raeume"] = [konfig_form.raum([_sensor(10 * n + i) for i in range(2)], nummer=n, name=f"R{n}")
                       for n in range(1, 6)]
    ablehnung = _geprueft(_gw(kreis), LOCK)
    assert ablehnung == cc.Ablehnung(cc.UNGUELTIG, cc.befund("fuehler", "anzahl", min=1, max=8, anzahl=10))


def test_doppelte_raumnummern_werden_abgelehnt():
    kreis = _gw_kreis(1)
    kreis["raeume"].append(konfig_form.raum([_sensor(50)], nummer=1, name="Bad", rolle="kontroll"))
    ablehnung = _geprueft(_gw(kreis), LOCK)
    assert ablehnung == cc.Ablehnung(cc.UNGUELTIG, cc.befund("raum", "doppelt"))


def test_ein_fremdes_feld_im_kreis_oder_raum_wird_abgelehnt():
    kreis = _gw_kreis(1)
    kreis["fremd"] = 1
    assert _geprueft(_gw(kreis), LOCK).grund == cc.UNGUELTIG
    kreis = _gw_kreis(1)
    kreis["raeume"][0]["fremd"] = 1
    assert _geprueft(_gw(kreis), LOCK).grund == cc.UNGUELTIG


def test_ein_gueltiger_kontrollraum_wird_uebernommen_gegenprobe():
    kreis = _gw_kreis(1)
    kreis["raeume"].append(konfig_form.raum([_sensor(50)], nummer=2, name="Bad", rolle="kontroll", ziel_fest=19.0))
    assert _geprueft(_gw(kreis), LOCK) is None


# --- Zusicherung des Kerns: Kontrollraeume beeinflussen die Regelung nie -------------------------------------------

def test_die_regelung_sieht_nur_die_fuehler_der_referenzraeume():
    kreis = _gw_kreis(1)
    kreis["raeume"].append(konfig_form.raum([_sensor(50)], nummer=2, name="Bad", rolle="kontroll"))
    neu = _gw(kreis)
    assert kreisansicht.erste(neu)["raumfuehler"] == [_sensor(1)]
    assert kreisansicht.alle_fuehler(neu["kreise"][0]) == [_sensor(1), _sensor(50)]
    assert kreisansicht.referenz_fuehler(neu["kreise"][0]) == [_sensor(1)]


def test_ein_fuehler_der_nur_im_kontrollraum_steht_gehoert_nicht_zur_regelung():
    kreis = _gw_kreis(1)
    kreis["raeume"][0]["rolle"] = "kontroll"
    kreis["raeume"].append(konfig_form.raum([_sensor(50)], nummer=2, name="Bad"))
    assert kreisansicht.erste(_gw(kreis))["raumfuehler"] == [_sensor(50)]


# --- alte Form ---------------------------------------------------------------------------------------------------

def test_die_alte_flache_form_wird_laut_abgelehnt():
    alt = {"anlage": "kunde3", "profil": "viessmann_gastherme_heizkoerper", "hebelsatz": "viessmann_vicare",
           "verteilsystem": "Heizkoerper",
           "bindung": {"treiber": {"id": "simulation", "parameter": {"heizkreis": 0}}, "thermostat": None},
           "raumfuehler": [_sensor(1)], "tagestick": "12:12", "abo": "aktiv"}
    ablehnung = _geprueft(alt, LOCK)
    assert ablehnung is not None and ablehnung.grund == cc.UNGUELTIG


# --- Grenzen und genaue Befunde des Aufbaus ----------------------------------------------------------------------

def _ungueltig(feld, regel="ungueltig", **zahlen):
    return cc.Ablehnung(cc.UNGUELTIG, cc.befund(feld, regel, **zahlen))


def test_namen_gelten_bis_80_zeichen():
    kreis = _gw_kreis(1)
    kreis["name"] = "k" * 80
    assert _geprueft(_gw(kreis, anlage_name="a" * 80), LOCK) is None
    kreis["name"] = "k" * 81
    assert _geprueft(_gw(kreis), LOCK) == _ungueltig("name")
    assert _geprueft(_gw(_gw_kreis(1), anlage_name="a" * 81), LOCK) == _ungueltig("anlage_name")


def test_die_hoechste_kreis_und_raumnummer_ist_zulaessig():
    kreis = _gw_kreis(1)
    kreis["kreis"] = 2**31 - 1
    kreis["raeume"][0]["raum"] = 2**31 - 1
    assert _geprueft(_gw(kreis), LOCK) is None


def test_kreisnummern_nennen_doppelt_und_ungueltig_genau():
    gleich = [_gw_kreis(1), _gw_kreis(2)]
    gleich[1]["kreis"] = 1
    assert _geprueft(_gw(*gleich), LOCK) == _ungueltig("kreis", "doppelt")
    rueckwaerts = [_gw_kreis(2), _gw_kreis(3)]
    rueckwaerts[1]["kreis"] = 1
    assert _geprueft(_gw(*rueckwaerts), LOCK) == _ungueltig("kreis", "ungueltig")


def test_ein_kreiseintrag_der_kein_objekt_ist_wird_abgelehnt():
    assert _geprueft(_gw("kreis"), LOCK) == _ungueltig("kreise", "ungueltig")


def test_ein_fehlendes_kreisfeld_wird_mit_seinem_namen_gemeldet():
    kreis = _gw_kreis(1)
    del kreis["verteilsystem"]
    assert _geprueft(_gw(kreis), LOCK) == _ungueltig("verteilsystem", "fehlt")


def test_ein_leerer_kreisname_wird_abgelehnt():
    kreis = _gw_kreis(1)
    kreis["name"] = ""
    assert _geprueft(_gw(kreis), LOCK) == _ungueltig("name")


def test_die_abfrage_nennt_ihren_bereich():
    kreis = _gw_kreis(1)
    kreis["bindung"]["abfrage_s"] = 9
    assert _geprueft(_gw(kreis), LOCK) == _ungueltig("abfrage_s", "bereich", min=10, max=3600)


def test_raeume_eines_kreises_grenzen_und_befunde():
    kreis = _gw_kreis(1)
    kreis["raeume"] = "keine Liste"
    assert _geprueft(_gw(kreis), LOCK) == _ungueltig("raeume", "ungueltig")
    kreis["raeume"] = []
    assert _geprueft(_gw(kreis), LOCK) == _ungueltig("raeume", "anzahl", min=1, max=16, anzahl=0)
    kreis["raeume"] = [konfig_form.raum([_sensor(1)])] + [
        konfig_form.raum([], nummer=n, name=f"R{n}", rolle="kontroll") for n in range(2, 17)]
    assert _geprueft(_gw(kreis), LOCK) is None  # sechzehn Raeume
    kreis["raeume"].append(konfig_form.raum([], nummer=17, name="R17", rolle="kontroll"))
    assert _geprueft(_gw(kreis), LOCK) == _ungueltig("raeume", "anzahl", min=1, max=16, anzahl=17)


def test_acht_referenzfuehler_sind_zulaessig_gegenprobe():
    kreis = _gw_kreis(1)
    kreis["raeume"] = [konfig_form.raum([_sensor(10 * n + i) for i in range(2)], nummer=n, name=f"R{n}")
                       for n in range(1, 5)]
    assert _geprueft(_gw(kreis), LOCK) is None


def test_acht_fuehler_in_einem_raum_sind_zulaessig_und_neun_nennen_die_grenze():
    kreis = _kreis_ohne_raum(_gw_kreis(1), fuehler=[_sensor(10 + i) for i in range(8)])
    assert _geprueft(_gw(kreis), LOCK) is None
    kreis = _kreis_ohne_raum(_gw_kreis(1), fuehler=[_sensor(10 + i) for i in range(9)])
    assert _geprueft(_gw(kreis), LOCK) == _ungueltig("fuehler", "anzahl", min=0, max=8, anzahl=9)


@pytest.mark.parametrize("fuehler", ["sensor", 5, [1], [""], [None]])
def test_fuehler_muessen_eine_liste_von_texten_sein(fuehler):
    kreis = _kreis_ohne_raum(_gw_kreis(1), fuehler=fuehler)
    assert _geprueft(_gw(kreis), LOCK) == _ungueltig("fuehler")


@pytest.mark.parametrize("soll_quelle", [1, "ja", None])
def test_soll_quelle_muss_ein_wahrheitswert_sein(soll_quelle):
    kreis = _kreis_ohne_raum(_gw_kreis(1), soll_quelle=soll_quelle)
    assert _geprueft(_gw(kreis), LOCK) == _ungueltig("soll_quelle")


def test_ziel_fest_eines_kontrollraums_ist_eine_endliche_zahl():
    kreis = _gw_kreis(1)
    kreis["raeume"].append(konfig_form.raum([_sensor(50)], nummer=2, name="Bad", rolle="kontroll", ziel_fest=21.0))
    assert _geprueft(_gw(kreis), LOCK) is None
    for falsch in ("warm", float("inf"), True):
        kreis["raeume"][1]["ziel_fest"] = falsch
        assert _geprueft(_gw(kreis), LOCK) == _ungueltig("ziel_fest"), falsch


def test_der_weg_der_anlage_muss_ein_text_sein():
    assert _geprueft(_gw(_gw_kreis(1), weg=None), LOCK) == _ungueltig("weg")


def test_die_mindest_integration_ist_leer_oder_ein_text():
    assert _geprueft(_gw(_gw_kreis(1), mindest_integration="1.2.3"), LOCK) is None
    for falsch in ("", 5):
        assert _geprueft(_gw(_gw_kreis(1), mindest_integration=falsch), LOCK) == _ungueltig("mindest_integration")


def test_dieselben_treiberparameter_in_anderer_schluesselfolge_sind_dasselbe_schreibziel():
    erster, zweiter = _gw_kreis(1), _gw_kreis(2)
    erster["bindung"]["treiber"]["parameter"] = {"heizkreis": 1, "zone": 2}
    zweiter["bindung"]["treiber"]["parameter"] = {"zone": 2, "heizkreis": 1}
    ablehnung = _geprueft(_gw(erster, zweiter), LOCK)
    assert ablehnung == cc.Ablehnung(cc.DOPPELT_BELEGT, cc.befund("bindung", "doppelt"))
