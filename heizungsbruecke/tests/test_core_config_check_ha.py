"""Spec 5c 1.3 / 5b 3.1: lokale Pruefung einer HA-Konfiguration (Aufbau rein, HA-Teil ueber den Rueckruf)."""
import pytest
from geraet_helpers import ha_konfiguration

from smartheat_core import config_check as cc
from smartheat_core.rastung import Rastung
from smartheat_device import fehler, wire


def _geprueft(*args, **kwargs):
    """cc.pruefen; jede Ablehnung hat die Form des Vertrags (Grund und details, kein Text)."""
    ablehnung = cc.pruefen(*args, **kwargs)
    if ablehnung is not None:
        assert not hasattr(ablehnung, "text") and fehler.gueltig(ablehnung.details), ablehnung
    return ablehnung


def _pruefen(konfiguration, host_details=None):
    return _geprueft(konfiguration, None, host=cc.HOST_HA, treiber_pruefen=lambda k: host_details)


def test_the_poll_interval_bounds():
    assert cc.ABFRAGE_S == (10, 3600)


def test_valid_ha_configuration_passes_and_the_host_sees_all_of_it():
    seen = []
    assert _geprueft(ha_konfiguration(), None, host=cc.HOST_HA, treiber_pruefen=lambda k: seen.append(k)) is None
    assert seen[0]["raumfuehler"] == ["sensor.wohnzimmer_thermostat_temperatur"] and "bindung" in seen[0]


def test_host_details_become_role_rejection():
    details = {"feld": "heat_limit", "regel": "unbekannt"}
    ablehnung = _geprueft(ha_konfiguration(), None, host=cc.HOST_HA, treiber_pruefen=lambda k: details)
    assert ablehnung == cc.Ablehnung(cc.ROLLE_UNZULAESSIG, details)


def test_the_host_is_asked_only_for_a_structurally_valid_configuration():
    seen = []
    assert _geprueft(ha_konfiguration(raumfuehler=["switch.licht"]), None, host=cc.HOST_HA,
                      treiber_pruefen=lambda k: seen.append(k)).grund == cc.ROLLE_UNZULAESSIG
    assert seen == []


@pytest.mark.parametrize("changes, grund", [
    ({"bindung": {"rollen": {"curve": "number.c"}}}, cc.ROLLE_UNZULAESSIG),                  # room_target fehlt
    ({"bindung": {"rollen": {"room_target": "climate.w::temperature", "zigbee": "x.y"}}}, cc.ROLLE_UNZULAESSIG),
    ({"bindung": {"rollen": {"room_target": "Climate W"}}}, cc.ROLLE_UNZULAESSIG),            # kein Referenzformat
    ({"bindung": {"rollen": {"room_target": 5}}}, cc.ROLLE_UNZULAESSIG),
    ({"bindung": {"rollen": []}}, cc.ROLLE_UNZULAESSIG),
    ({"bindung": {"kreis": {"plattform": "mypyllant"}}}, cc.ROLLE_UNZULAESSIG),
    ({"bindung": {"kreis": {"plattform": "mypyllant", "config_entry": "e1", "anlage": "", "nummer": "0"}}},
     cc.ROLLE_UNZULAESSIG),
    ({"bindung": {"kreis": "x"}}, cc.ROLLE_UNZULAESSIG),
    ({"raumfuehler": ["switch.licht"]}, cc.ROLLE_UNZULAESSIG),
    ({"raumfuehler": ["climate.w"]}, cc.ROLLE_UNZULAESSIG),
    ({"raumfuehler": ["sensor.a", "sensor.a"]}, cc.DOPPELT_BELEGT),
    ({"bindung": {"rollen": {"room_target": "sensor.a", "curve": "sensor.a"}}}, cc.DOPPELT_BELEGT),
    ({"bindung": {"batterien": ["light.x"]}}, cc.ROLLE_UNZULAESSIG),
    ({"bindung": {"batterien": "sensor.x"}}, cc.ROLLE_UNZULAESSIG),
    ({"bindung": {"benachrichtigung": ["notify.Handy Papa"]}}, cc.ROLLE_UNZULAESSIG),
    ({"bindung": {"hinweise_aus": ["quatsch"]}}, cc.ROLLE_UNZULAESSIG),
    ({"bindung": {"hinweise_aus": ["notbetrieb"]}}, cc.ROLLE_UNZULAESSIG),     # kritische Meldungen nie abschaltbar
    ({"bindung": {"hinweise_aus": ["konfiguration"]}}, cc.ROLLE_UNZULAESSIG),
    ({"bindung": {"hinweise_aus": ["zugang"]}}, cc.ROLLE_UNZULAESSIG),
    ({"bindung": {"hinweise_aus": [["batterie"]]}}, cc.ROLLE_UNZULAESSIG),
    ({"bindung": {"abfrage_s": 5}}, cc.UNGUELTIG),
    ({"bindung": {"abfrage_s": 3601}}, cc.UNGUELTIG),
    ({"bindung": {"abfrage_s": True}}, cc.UNGUELTIG),
    ({"bindung": {"abfrage_s": 60.5}}, cc.UNGUELTIG),
])
def test_structural_rules(changes, grund):
    ablehnung = _pruefen(ha_konfiguration(**changes))
    assert ablehnung is not None and ablehnung.grund == grund


def _mit_ref(feld: str, ref: str) -> dict:
    """Konfiguration mit `ref` im Feld `feld` (Muster HA_REF, HA_RAUMFUEHLER, HA_BATTERIE, HA_DIENST)."""
    if feld == "room_target":
        return ha_konfiguration(bindung={"rollen": {"room_target": ref}})
    if feld == "raumfuehler":
        return ha_konfiguration(raumfuehler=[ref])
    return ha_konfiguration(bindung={feld: [ref]})


@pytest.mark.parametrize(("feld", "gueltig"), [
    ("room_target", "climate.wohnzimmer_thermostat::temperature"),        # HA_REF
    ("raumfuehler", "sensor.wohnzimmer_thermostat_temperatur"),           # HA_RAUMFUEHLER
    ("batterien", "sensor.wohnzimmer_thermostat_batterie"),               # HA_BATTERIE
    ("benachrichtigung", "notify.mobile_app_handy"),                      # HA_DIENST
])
@pytest.mark.parametrize("anhang", ["\n", "\u202e", "\nWARNING  gefaelscht", " gefaelscht"],
                         ids=["zeilenumbruch", "bidi_202e", "zweite_logzeile", "nachgestellter_text"])
def test_a_reference_with_a_valid_prefix_and_anything_after_it_is_rejected(feld, gueltig, anhang):
    """T6-Rest (Re-Review der Fix-Welle, Minor 1): die Muster gelten fuer die ganze Referenz (`fullmatch`). Ein gueltiger
    Anfang mit Zeilenumbruch, Bidi-Steuerzeichen oder Text dahinter wird abgelehnt; deshalb stehen Entity-IDs der
    Bindung ohne Escape im Log."""
    assert _pruefen(_mit_ref(feld, gueltig)) is None  # Gegenprobe: ohne Anhang gueltig
    assert _pruefen(_mit_ref(feld, gueltig + anhang)) == cc.Ablehnung(
        cc.ROLLE_UNZULAESSIG, {"feld": feld, "regel": "format"})


@pytest.mark.parametrize("attribut", ["longitude", "latitude", "hvac_action"])
def test_a_reference_attribute_other_than_the_temperatures_is_rejected(attribut):
    """A6-02: nach `::` nur temperature und current_temperature; der Befund nennt das Feld der Rolle, nie die Entity."""
    ablehnung = _pruefen(ha_konfiguration(bindung={"rollen": {"room_target": f"zone.home::{attribut}"}}))
    assert ablehnung == cc.Ablehnung(cc.ROLLE_UNZULAESSIG, {"feld": "room_target", "regel": "format"})
    assert "zone" not in repr(ablehnung)
    ablehnung = _pruefen(ha_konfiguration(bindung={"rollen": {"room_target": "climate.w::temperature",
                                                 "curve": f"climate.c::{attribut}"}}))
    assert ablehnung == cc.Ablehnung(cc.ROLLE_UNZULAESSIG, {"feld": "curve", "regel": "format"})


@pytest.mark.parametrize("attribut", ["temperature", "current_temperature"])
def test_the_two_temperature_attributes_are_accepted(attribut):
    assert _pruefen(ha_konfiguration(bindung={"rollen": {"room_target": f"climate.w::{attribut}"}})) is None


@pytest.mark.parametrize("dienst", ["notify.email", "notify.telegram", "notify.handy", "notify.mobile_app_"])
def test_a_notification_service_other_than_mobile_app_is_rejected(dienst):
    ablehnung = _pruefen(ha_konfiguration(bindung={"benachrichtigung": [dienst]}))
    assert ablehnung == cc.Ablehnung(cc.ROLLE_UNZULAESSIG, {"feld": "benachrichtigung", "regel": "format"})
    assert dienst not in repr(ablehnung)


def test_a_mobile_app_notification_service_is_accepted():
    assert _pruefen(ha_konfiguration(bindung={"benachrichtigung": ["notify.mobile_app_x"]})) is None


def test_ha_bindung_pruefen_is_the_public_structure_check():
    assert cc.ha_bindung_pruefen(ha_konfiguration()) is None
    assert cc.ha_bindung_pruefen(ha_konfiguration(bindung={"benachrichtigung": ["notify.email"]})) == cc.Ablehnung(
        cc.ROLLE_UNZULAESSIG, {"feld": "benachrichtigung", "regel": "format"})


@pytest.mark.parametrize(("changes", "details"), [
    ({"bindung": {"extra": 1}}, {"feld": "bindung", "regel": "unbekannt"}),
    ({"bindung": {"kreis": {"plattform": "mypyllant", "config_entry": "e1", "anlage": "S", "nummer": "0", "extra": 1}}},
     {"feld": "kreis", "regel": "unbekannt"}),
])
def test_a_foreign_field_in_the_ha_binding_is_a_role_rejection(changes, details):
    """V1 ist auf beiden Seiten strikt (Nutzer-Entscheidung 2026-10-16); der fremde Name geht nicht hoch."""
    assert _pruefen(ha_konfiguration(**changes)) == cc.Ablehnung(cc.ROLLE_UNZULAESSIG, details)


def test_incomplete_binding_is_a_role_rejection():
    konfiguration = ha_konfiguration()
    del konfiguration["bindung"]["batterien"]
    assert _pruefen(konfiguration).grund == cc.ROLLE_UNZULAESSIG


@pytest.mark.parametrize("bindung", [
    {"abfrage_s": 10}, {"abfrage_s": 3600}, {"abfrage_s": None}, {"hinweise_aus": []}, {"batterien": []},
    {"benachrichtigung": []}, {"batterien": ["binary_sensor.fenster_batterie"]},
    {"kreis": {"plattform": "mypyllant", "anlage": "S", "nummer": "0"}},
    {"hinweise_aus": list(wire.HA_HINWEISE)}, {"einheiten": {"energy_electrical_total": "kWh"}},
])
def test_accepted_binding_variants(bindung):
    assert _pruefen(ha_konfiguration(bindung=bindung)) is None


def test_a_missing_poll_interval_is_the_default_of_the_lever_set():
    konfiguration = ha_konfiguration()
    del konfiguration["bindung"]["abfrage_s"]
    assert _pruefen(konfiguration) is None


@pytest.mark.parametrize("rollen, raumfuehler", [
    # Referenzformen, die der Server je Rolle annimmt (ha_pruefung._ATTRIBUTE): Raumfuehler sensor bare oder
    # climate::current_temperature, room_target climate::temperature, room_setpoint climate::temperature oder bare,
    # alle anderen bare (auch weather).
    ({"room_target": "climate.w::temperature"}, ["sensor.a", "climate.b::current_temperature"]),
    ({"room_target": "climate.w::temperature", "room_setpoint": "climate.z::temperature"}, ["sensor.a"]),
    ({"room_target": "climate.w::temperature", "room_setpoint": "number.z"}, ["sensor.a"]),
    ({"room_target": "climate.w::temperature", "outdoor_temp": "weather.home", "level": "number.l"}, ["sensor.a"]),
])
def test_every_reference_form_of_the_server_is_accepted(rollen, raumfuehler):
    assert _pruefen(ha_konfiguration(bindung={"rollen": rollen}, raumfuehler=raumfuehler)) is None


def test_every_contract_role_is_accepted():
    rollen = {"room_target": "climate.w::temperature"}
    rollen.update({rolle: f"sensor.{rolle}" for rolle in wire.HA_ROLLEN if rolle != "room_target"})
    assert _pruefen(ha_konfiguration(bindung={"rollen": rollen})) is None


def test_gateway_rules_are_unchanged():
    assert _geprueft(ha_konfiguration(), None, host=cc.HOST_GATEWAY).grund == cc.ROLLE_UNZULAESSIG


def test_an_unknown_host_is_rejected():
    assert _geprueft(ha_konfiguration(), None, host="mars") == cc.Ablehnung(cc.ROLLE_UNZULAESSIG)


def test_lever_set_lock_applies_to_home_assistant_too():
    ablehnung = _geprueft(ha_konfiguration(hebelsatz="viessmann_vicare"), Rastung(ha_konfiguration()["hebelsatz"], ha_konfiguration()["verteilsystem"]),
                            host=cc.HOST_HA)
    assert ablehnung.grund == cc.HEBELSATZ_EINGERASTET


_KREIS = {"plattform": "mypyllant", "anlage": "S", "nummer": "0"}


@pytest.mark.parametrize("kreis", [
    {"plattform": "mypyllant", "anlage": "S"},
    {**_KREIS, "fremd": "x"},
    {**_KREIS, "config_entry": None},
    {**_KREIS, "config_entry": ""},
    {**_KREIS, "nummer": 0},
])
def test_kaputter_kreis_wird_abgelehnt(kreis):
    ablehnung = _pruefen(ha_konfiguration(bindung={"kreis": kreis}))
    assert ablehnung is not None and ablehnung.grund == cc.ROLLE_UNZULAESSIG


@pytest.mark.parametrize("feld", ["plattform", "anlage", "nummer"])  # Pflichtfelder als Literal, nicht die Konstante
@pytest.mark.parametrize("wert", ["fehlt", ""])
def test_jedes_pflichtfeld_des_kreises_fehlt_oder_ist_leer(feld, wert):
    kreis = {k: v for k, v in _KREIS.items() if k != feld}
    if wert == "":
        kreis[feld] = wert
    ablehnung = _pruefen(ha_konfiguration(bindung={"kreis": kreis}))
    assert ablehnung is not None and ablehnung.grund == cc.ROLLE_UNZULAESSIG


def test_config_entry_ist_das_einzige_optionale_feld_des_kreises():
    assert _pruefen(ha_konfiguration(bindung={"kreis": {**_KREIS, "config_entry": "e1"}})) is None
    assert _pruefen(ha_konfiguration(bindung={"kreis": dict(_KREIS)})) is None
    assert _pruefen(ha_konfiguration(bindung={"kreis": {**_KREIS, "fremd": "x"}})) == cc.Ablehnung(
        "rolle_unzulaessig", {"feld": "kreis", "regel": "unbekannt"})


def test_kreis_ohne_config_entry_wird_angenommen():
    assert _pruefen(ha_konfiguration(bindung={"kreis": dict(_KREIS)})) is None
