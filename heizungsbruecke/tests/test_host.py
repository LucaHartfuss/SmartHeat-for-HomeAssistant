"""HA-Host der Laufzeit im Geraete-Pfad: Konfiguration aus dem Konfigurationsdokument."""
import dataclasses
import logging
from unittest.mock import MagicMock

import pytest
from fakes import FakeHa, schreibziel_eintrag
from geraet_helpers import FakeGeraet, konfiguration_aus_optionen
from runtime_helpers import OPTIONS, _start_bridge

from heizungsbruecke import dokument
from heizungsbruecke.derived_sensors import DerivedSensors
from heizungsbruecke.ha_api import HomeAssistantApi
from heizungsbruecke.host import DERIVED_SENSORS_RETRY_DELAYS_SECONDS, HaHost
from smartheat_runtime.app import IdleBridge, StartFailure
from smartheat_runtime.backup_store import load_backup, save_backup
from smartheat_runtime.notifier import STATE_OK
from smartheat_runtime.roles import lever_refs_identity


def test_boot_info_without_a_document_is_not_configured():
    boot = HaHost(FakeGeraet(None), MagicMock(spec=HomeAssistantApi)).boot_info()
    assert not boot.configured and not boot.signed_off


def test_sign_off_with_broken_configuration_restores_nothing_and_reports_why(env):
    bridge = _start_bridge(env, abgemeldet=True, verteilsystem=None)
    assert isinstance(bridge, IdleBridge) and bridge.reason == "abgemeldet"
    assert env.ha.writes == []
    assert bridge.status is not None and bridge.status.flags.grund.startswith("Konfiguration ungültig")


def test_sign_off_parts_are_none_for_a_broken_configuration():
    geraet = FakeGeraet(konfiguration_aus_optionen({**OPTIONS, "verteilsystem": None}))
    assert HaHost(geraet, FakeHa()).sign_off_parts() is None


# --- Positivliste und Formen auch fuer die uebernommene Konfiguration beim Start ---

@pytest.fixture
def sleeps(monkeypatch):
    """Wartezeiten der Wiederholung auf 0: die Aufrufe von time.sleep werden aufgezeichnet, nicht gewartet."""
    recorded = []
    monkeypatch.setattr("heizungsbruecke.host.time.sleep", recorded.append)
    return recorded


@pytest.fixture
def angelegt(env, monkeypatch):
    """Die Aufrufe von derived_sensors.ensure_all: jeder legt in HA einen Template-Sensor an."""
    aufrufe = []

    def ensure_all(**kwargs):
        aufrufe.append(kwargs)
        return DerivedSensors({"room_actual": "sensor.room_actual"}, (), "fp1")

    monkeypatch.setattr("heizungsbruecke.derived_sensors.ensure_all", ensure_all)
    return aufrufe


def _host(env, geraet=None, **overrides):
    geraet = geraet or FakeGeraet(konfiguration_aus_optionen({**OPTIONS, **overrides}))
    return HaHost(geraet, env.ha)


#: Kennung des Kreises der Test-Dokumente (geraet_helpers.konfiguration_aus_optionen), woertlich.
KREIS_KENNUNG = '{"anlage": "S", "nummer": "0", "plattform": "mypyllant"}'


def test_anlagen_kennung_kommt_aus_dem_kreis(env, angelegt, sleeps):
    """Die Anlage ist der Heizkreis (plattform, anlage, nummer), nicht die Entity-IDs der Hebel. Eine umbenannte
    Hebel-Entity bleibt dieselbe Anlage; der Config-Entry zaehlt nicht (Neuanlage der Integration = dieselbe Anlage)."""
    loaded = _host(env).load()
    assert loaded.plant_id == KREIS_KENNUNG
    env.ha.states["number.heat_limit_neu"] = 16.0
    env.ha.registry.append(schreibziel_eintrag("number.heat_limit_neu", "mypyllant"))
    umbenannt = _host(env, entity_heat_limit="number.heat_limit_neu").load()
    assert umbenannt.manifest.refs["heat_limit"] == "number.heat_limit_neu"
    assert lever_refs_identity(umbenannt.manifest) != lever_refs_identity(loaded.manifest)  # Gegenprobe: bis 1.1.0 neu
    assert umbenannt.plant_id == KREIS_KENNUNG
    b = dokument.bindung(konfiguration_aus_optionen(OPTIONS))
    assert dokument.anlagen_kennung(dataclasses.replace(b, kreis={**b.kreis, "config_entry": "e-neu"})) == KREIS_KENNUNG
    for feld, wert in (("plattform", "vicare"), ("anlage", "T"), ("nummer", "1")):
        assert dokument.anlagen_kennung(dataclasses.replace(b, kreis={**b.kreis, feld: wert})) != KREIS_KENNUNG, feld


def test_eine_uebernommene_rolle_auf_einer_person_startet_nicht_und_legt_nichts_an(env, angelegt, sleeps):
    """Eine schon uebernommene Konfiguration (vor der Positivliste angenommen) gilt beim Start wie beim Annehmen: die
    Rolle zeigt auf eine Person, kein Template-Sensor entsteht, der Text nennt das Feld, nie die Entity."""
    env.ha.states["person.luca"] = "home"
    with pytest.raises(StartFailure) as fehler:
        _host(env, entity_operating_mode="person.luca").load()
    assert fehler.value.grund == "operating_mode: diese Entity darf SmartHeat nicht nutzen (lokale Positivliste)"
    assert "person.luca" not in fehler.value.grund
    assert angelegt == [] and sleeps == []


def test_gegenprobe_dieselbe_rolle_auf_einer_erlaubten_entity_startet(env, angelegt, sleeps):
    env.ha.states["sensor.betriebsart_text"] = "auto"
    for entity_id in ("sensor.betriebsart_text", "number.heat_limit"):  # die Plattform des Kreises liefert Einstellbares
        env.ha.registry.append({"entity_id": entity_id, "platform": "mypyllant", "config_entry_id": "e1"})
    loaded = _host(env, entity_operating_mode="sensor.betriebsart_text").load()
    assert loaded.manifest.refs["operating_mode"] == "sensor.betriebsart_text"
    assert len(angelegt) == 1


def test_eine_pflichtrolle_ohne_zustand_wird_wiederholt_und_scheitert_mit_festem_schluessel(env, angelegt, sleeps):
    """Realistischer Fall: die Entity fehlt (umbenannt, geloescht, deaktiviert) und entity_exists meldet sie trotzdem
    noch. Eine Pflichtrolle ohne Zustand ist kein Urteil, sondern nicht pruefbar: Wiederholung wie bei fehlenden
    Entities, nach dem Budget StartFailure mit stabilem Schluessel und lesbarem Text mit dem Feld (nie der Entity)."""
    del env.ha.states["number.heat_limit"]
    with pytest.raises(StartFailure) as fehler:
        _host(env).load()
    assert fehler.value.key == "rollen_nicht_pruefbar"
    assert fehler.value.grund == ("heat_limit: zugeordnete Entity fehlt in Home Assistant – "
                                  "bitte SmartHeat neu konfigurieren")
    assert sleeps == list(DERIVED_SENSORS_RETRY_DELAYS_SECONDS)
    assert angelegt == []


def test_ein_raumfuehler_ohne_zustand_ist_nicht_pruefbar(env, angelegt, sleeps):
    del env.ha.states["sensor.room_actual"], env.ha.states["sensor.room_actual::device_class"]
    with pytest.raises(StartFailure) as fehler:
        _host(env).load()
    assert fehler.value.key == "rollen_nicht_pruefbar" and fehler.value.grund.startswith("raumfuehler: ")


def test_eine_optionale_rolle_ohne_zustand_startet_ungelesen_mit_hinweis(env, angelegt, sleeps):
    """Die Entity heilt nicht von selbst; der Start wartet nicht, die Referenz fehlt in diesem Lauf im
    Manifest, ein nicht kritischer Hinweis nennt das Feld (nie die Entity)."""
    loaded = _host(env, entity_flow_temperature="sensor.vorlauf").load()
    assert "flow_temperature" not in loaded.manifest.refs and sleeps == []
    assert [(n.key, n.message) for n in loaded.notices] == [(
        "entity_fehlt:flow_temperature",
        "SmartHeat: flow_temperature: zugeordnete Entity fehlt in Home Assistant und wird bis zum nächsten Start "
        "nicht gelesen.")]
    assert "sensor.vorlauf" not in loaded.notices[0].message


BASIS_ZUSTAENDE = {
    "number.curve_current": 0.75, "number.shift_current": 20.0, "select.betriebsart": "hz_operationmode_automatic",
    "number.komfort": 22.0, "number.absenk": 18.0,
}
BASIS = {
    "lever_set": "weishaupt_wwp_basis", "entity_curve_current": "number.curve_current",
    "entity_shift_current": "number.shift_current", "entity_heat_limit": "", "entity_min_flow": "",
    "entity_mode_select": "select.betriebsart", "entity_setpoint_comfort": "number.komfort",
    "entity_setpoint_setback": "number.absenk",
}


def _basis(env, zustaende):
    """Zustaende des Hebelsatzes weishaupt_wwp_basis; seine Heizkurve und Normaltemperatur sind hier
    number.curve_current und number.shift_current, also Schreibziele von weishaupt_modbus (statt mypyllant)."""
    env.ha.states.update(zustaende)
    env.ha.registry = [schreibziel_eintrag(e["entity_id"], "weishaupt_modbus")
                       if e["entity_id"] in ("number.curve_current", "number.shift_current") else e
                       for e in env.ha.registry]


@pytest.mark.parametrize("rolle_option", ["entity_heat_limit", "entity_curve_current"])
def test_eine_optionale_hebelrolle_ohne_zustand_wird_nie_fallen_gelassen(env, angelegt, sleeps, rolle_option):
    """Eine Hebelrolle ist ein Schreibziel und gehoert bis zum Rueckbau der Uebernahme
    (offene-punkte.md A6-36-Uebernahme) zur frueheren Kennung (lever_refs_identity). Fiele sie weg, verwarf bind_to_tenant einen Bestand
    von 1.1.0 samt Wiederherstellungspunkt, Originalen und Schreibzaehlern. Ein Schreibziel ohne Zustand ist daher nicht
    pruefbar (Wiederholung, StartFailure), nie ungelesen."""
    _basis(env, BASIS_ZUSTAENDE)
    with pytest.raises(StartFailure) as fehler:
        _host(env, **{**BASIS, rolle_option: "number.swu"}).load()
    assert fehler.value.key == "rollen_nicht_pruefbar"
    assert fehler.value.grund.endswith("zugeordnete Entity fehlt in Home Assistant – bitte SmartHeat neu konfigurieren")
    assert sleeps == list(DERIVED_SENSORS_RETRY_DELAYS_SECONDS) and angelegt == []


def test_gegenprobe_eine_optionale_hebelrolle_mit_zustand_bleibt_im_manifest(env, angelegt, sleeps):
    _basis(env, {**BASIS_ZUSTAENDE, "number.swu": 18.0})
    loaded = _host(env, **{**BASIS, "entity_heat_limit": "number.swu"}).load()
    assert loaded.manifest.refs["heat_limit"] == "number.swu" and sleeps == []


def test_eine_fallen_gelassene_lese_rolle_aendert_die_anlagenidentitaet_nicht(env, angelegt, sleeps):
    """Gegenprobe zur Hebelrolle: eine optionale Lese-Rolle (Energie) ohne Zustand startet, wird ungelesen gemeldet, und
    die Kennung (aus dem Kreis) wie die fruehere aus den Hebelreferenzen bleibt dieselbe wie mit Zustand."""
    _basis(env, BASIS_ZUSTAENDE)
    ohne = _host(env, **BASIS, entity_energy_thermal_heating="sensor.waerme").load()
    assert "energy_thermal_heating" not in ohne.manifest.refs and sleeps == []
    assert [n.key for n in ohne.notices] == ["entity_fehlt:energy_thermal_heating"]
    env.ha.states.update({"sensor.waerme": 8.0, "sensor.waerme::device_class": "energy",
                          "sensor.waerme::unit_of_measurement": "kWh"})
    mit = _host(env, **BASIS, entity_energy_thermal_heating="sensor.waerme").load()
    assert mit.manifest.refs["energy_thermal_heating"] == "sensor.waerme"
    assert lever_refs_identity(ohne.manifest) == lever_refs_identity(mit.manifest) and ohne.plant_id == mit.plant_id


def test_gegenprobe_dieselbe_optionale_rolle_mit_zustand_bleibt_im_manifest(env, angelegt, sleeps):
    env.ha.states.update({"sensor.vorlauf": 35.0, "sensor.vorlauf::device_class": "temperature"})
    loaded = _host(env, entity_flow_temperature="sensor.vorlauf").load()
    assert loaded.manifest.refs["flow_temperature"] == "sensor.vorlauf"
    assert [(n.key, n.state) for n in loaded.notices] == [("entity_fehlt:flow_temperature", STATE_OK)]  # nur Entwarnung


def test_eine_batterie_ohne_zustand_wird_nicht_gelesen_und_gemeldet(env, angelegt, sleeps):
    env.ha.states.update({"sensor.gut": 80, "sensor.gut::device_class": "battery"})
    loaded = _host(env, battery_entities=["sensor.gut", "binary_sensor.weg_battery_low"]).load()
    assert [ref.ref for ref in loaded.config.battery_refs] == ["sensor.gut"]
    assert [n.key for n in loaded.notices] == ["entity_fehlt:batterien"]
    assert "binary_sensor.weg_battery_low" not in loaded.notices[0].message


def test_entity_fehlt_meldet_nach_rueckkehr_und_erneutem_fehlen_wieder(env, sleeps):
    """Kehrt die Entity zurueck, entwarnt der Start den Hinweis; fehlt sie spaeter erneut, kommt er wieder.
    Ohne die Entwarnung bliebe der Zustand „fehlt“ offen und der Melder schwiege beim zweiten Mal."""
    def fehlt_meldungen():
        return [push for push in env.ha.pushes if "zugeordnete Entity fehlt" in push]

    _start_bridge(env, entity_flow_temperature="sensor.vorlauf")  # Zustand fehlt
    assert len(fehlt_meldungen()) == 1
    env.ha.states.update({"sensor.vorlauf": 35.0, "sensor.vorlauf::device_class": "temperature"})
    _start_bridge(env, entity_flow_temperature="sensor.vorlauf")  # da: Entwarnung
    assert len(fehlt_meldungen()) == 1 and "wieder vorhanden" in env.ha.pushes[-1]
    del env.ha.states["sensor.vorlauf"], env.ha.states["sensor.vorlauf::device_class"]
    _start_bridge(env, entity_flow_temperature="sensor.vorlauf")  # fehlt wieder
    assert len(fehlt_meldungen()) == 2


@pytest.mark.parametrize("feld_option, ref", [("entity_operating_mode", "person.luca"),
                                              ("entity_flow_temperature", "binary_sensor.tuer"),
                                              ("entity_flow_temperature", "lock.haustuer")])
def test_eine_optionale_rolle_ohne_zustand_aus_fremder_domaene_ist_ein_konfigurationsfehler(
        env, angelegt, sleeps, feld_option, ref):
    """Auch ohne Zustand zaehlt die Domaene aus der ID (kein Warten, kein Start mit einer Person oder einem Schloss)."""
    with pytest.raises(StartFailure) as fehler:
        _host(env, **{feld_option: ref}).load()
    assert fehler.value.grund.endswith("diese Entity darf SmartHeat nicht nutzen (lokale Positivliste)")
    assert ref not in fehler.value.grund and sleeps == [] and angelegt == []


def test_vor_dem_laufenden_ha_gibt_es_kein_urteil(env, angelegt, sleeps, monkeypatch):
    """Faehrt HA noch hoch (nicht RUNNING), fehlen Zustaende: ein Verstoss gilt erst, wenn HA erreichbar ist."""
    env.ha.states["person.luca"] = "home"
    erreichbar = {"ja": False}
    pruefungen = []
    echte_registry = env.ha.entity_registry

    def registry():
        pruefungen.append(erreichbar["ja"])
        return echte_registry()

    def is_reachable():
        if not erreichbar["ja"] and len(pruefungen) == 0 and is_reachable.aufrufe >= 3:
            erreichbar["ja"] = True
        is_reachable.aufrufe += 1
        return erreichbar["ja"]

    is_reachable.aufrufe = 0
    monkeypatch.setattr(env.ha, "entity_registry", registry)
    monkeypatch.setattr(env.ha, "is_reachable", is_reachable)
    with pytest.raises(StartFailure) as fehler:
        _host(env, entity_operating_mode="person.luca").load()
    assert pruefungen == [True]  # Registry erst gelesen, als HA lief
    assert fehler.value.grund.startswith("operating_mode: diese Entity darf SmartHeat nicht nutzen")


def test_wirft_die_registry_einmal_gelingt_der_zweite_versuch(env, angelegt, sleeps, monkeypatch):
    echte_registry = env.ha.entity_registry
    aufrufe = []

    def registry():
        aufrufe.append(1)
        if len(aufrufe) == 1:
            raise OSError("Registry gerade nicht erreichbar")
        return echte_registry()

    monkeypatch.setattr(env.ha, "entity_registry", registry)
    _host(env).load()
    assert len(aufrufe) == 2 and sleeps == [DERIVED_SENSORS_RETRY_DELAYS_SECONDS[0]]
    assert len(angelegt) == 1


def test_eine_alte_konfiguration_mit_notify_email_startet_nicht_und_die_senke_kennt_den_dienst_nicht(
        env, angelegt, sleeps, caplog):
    gesehen = []

    class Geraet(FakeGeraet):
        def notify_sink_fuer(self, services):
            gesehen.append(list(services))
            return super().notify_sink_fuer(services)

    konfiguration = konfiguration_aus_optionen({**OPTIONS, "notify_services": ["notify.mobile_app_handy", "notify.email"]})
    with caplog.at_level(logging.WARNING):
        host = _host(env, Geraet(konfiguration))
    assert gesehen == [["notify.mobile_app_handy"]]
    meldungen = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert meldungen == ["1 Benachrichtigungsdienste der Konfiguration sind keine Handy-Dienste und werden nicht genutzt."]
    with pytest.raises(StartFailure) as fehler:
        host.load()
    assert "benachrichtigung" in fehler.value.grund and "notify.email" not in fehler.value.grund
    assert angelegt == []


def test_eine_gueltige_dienstliste_bleibt_unveraendert_ohne_warnung(env, caplog):
    gesehen = []

    class Geraet(FakeGeraet):
        def notify_sink_fuer(self, services):
            gesehen.append(list(services))
            return super().notify_sink_fuer(services)

    with caplog.at_level(logging.WARNING):
        _host(env, Geraet(konfiguration_aus_optionen(OPTIONS)))
    assert gesehen == [["notify.mobile_app_handy"]] and caplog.records == []


def test_ein_attribut_ausser_der_temperatur_startet_nicht(env, angelegt, sleeps):
    """Die Form der uebernommenen Bindung (Attribute) gilt beim Start: `::hvac_action` ist kein Temperaturattribut (die
    Schreibregeln der Hebel fangen es nur bei den Hebeln ab, hier eine Lese-Rolle)."""
    with pytest.raises(StartFailure) as fehler:
        _host(env, entity_operating_mode="climate.zone::hvac_action").load()
    assert fehler.value.grund.startswith("operating_mode: ") and "hvac_action" not in fehler.value.grund
    assert angelegt == []


# --- Schreibziele lokal verankert, auch fuer die uebernommene Konfiguration beim Start ---

SCHREIBZIELE = ("number.curve_current", "number.shift_current", "number.min_flow", "number.heat_limit")
THERME = {"platform": "mypyllant", "config_entry_id": "e1", "device_id": "d-therme"}


def _registry(env, eintraege=None):
    """Registry wie bei client1 (Schreibziele aus OPTIONS: mypyllant, ein Config-Entry, ein Geraet der Therme), dazu
    `eintraege` (Entity -> Felder; None = kein Eintrag)."""
    neu = {**dict.fromkeys(SCHREIBZIELE, THERME), **(eintraege or {})}
    env.ha.registry = [e for e in env.ha.registry if e["entity_id"] not in neu] + [
        {"entity_id": entity_id, **felder} for entity_id, felder in neu.items() if felder is not None]


def _startet_nicht(env, angelegt, sleeps, grund):
    """Konfigurationsfehler: load wirft ohne Wiederholung, nichts angelegt; die Laufzeit ruht ohne zu schreiben."""
    with pytest.raises(StartFailure) as fehler:
        _host(env).load()
    assert fehler.value.grund == grund
    assert angelegt == [] and sleeps == []
    bridge = _start_bridge(env)
    assert isinstance(bridge, IdleBridge) and bridge.reason == "konfigurationsfehler"
    assert env.ha.writes == []


def test_uebernommenes_schreibziel_fremder_plattform_startet_nicht(env, angelegt, sleeps):
    """Eine schon uebernommene Konfiguration, deren Heizgrenze heute eine Entity einer anderen Integration ist
    (etwa nach einer Umbelegung in HA), regelt nicht und schreibt nichts."""
    _registry(env, {"number.heat_limit": {**THERME, "platform": "template"}})
    _startet_nicht(env, angelegt, sleeps,
                   "heat_limit: diese Entity darf SmartHeat nicht nutzen (lokale Positivliste)")


def test_schreibziel_anderer_config_entry_startet_nicht(env, angelegt, sleeps):
    _registry(env, {"number.min_flow": {**THERME, "config_entry_id": "e2"}})
    _startet_nicht(env, angelegt, sleeps, "min_flow: diese Entity darf SmartHeat nicht nutzen (lokale Positivliste)")


def test_soll_entity_am_geraet_eines_schreibziels_startet_nicht(env, angelegt, sleeps):
    _registry(env, {"sensor.room_target": THERME})
    _startet_nicht(env, angelegt, sleeps,
                   "room_target: diese Entity darf SmartHeat nicht nutzen (lokale Positivliste)")


def test_ein_kreis_fremder_plattform_startet_nicht(env, angelegt, sleeps):
    """Die Plattform steht lokal je Hebelsatz; ein Dokument mit einer anderen Kreis-Plattform (passend zu
    Schreibzielen von shelly) startet nicht, statt der Plattform des Servers zu folgen."""
    shelly = {"platform": "shelly", "config_entry_id": "e-shelly", "device_id": "d-shelly"}
    _registry(env, dict.fromkeys(SCHREIBZIELE, shelly))
    konfiguration = konfiguration_aus_optionen(OPTIONS)
    konfiguration["bindung"]["kreis"] = {**konfiguration["bindung"]["kreis"], "plattform": "shelly",
                                         "config_entry": "e-shelly"}
    with pytest.raises(StartFailure) as fehler:
        HaHost(FakeGeraet(konfiguration), env.ha).load()
    assert fehler.value.grund == "plattform: diese Entity darf SmartHeat nicht nutzen (lokale Positivliste)"
    assert angelegt == [] and sleeps == []


def test_passende_schreibziele_starten(env, angelegt, sleeps):
    """Gegenprobe wie client1: alle Schreibziele von mypyllant aus einem Config-Entry, die Soll-Entity an einem anderen
    Geraet (Thermostat im Wohnzimmer) einer anderen Integration."""
    _registry(env, {"sensor.room_target": {"platform": "homematicip_local", "config_entry_id": "e2",
                                           "device_id": "d-wz"}})
    loaded = _host(env).load()
    hebel = {loaded.manifest.refs[rolle] for rolle in ("curve_current", "shift_current", "min_flow", "heat_limit")}
    assert hebel == set(SCHREIBZIELE)
    assert len(angelegt) == 1 and sleeps == []


def test_ein_schreibziel_ohne_registry_eintrag_wird_wiederholt_und_endet_als_startfehler(env, angelegt, sleeps):
    """Ohne Registry-Eintrag ist ein Schreibziel nicht pruefbar (kein Urteil): Wiederholung wie bei fehlenden
    Zustaenden, nach dem Budget der Startfehler rollen_nicht_pruefbar mit dem Feld (nie der Entity)."""
    _registry(env, {"number.shift_current": None})
    with pytest.raises(StartFailure) as fehler:
        _host(env).load()
    assert fehler.value.key == "rollen_nicht_pruefbar"
    assert fehler.value.grund == ("room_setpoint: Entity ohne Eintrag in der Entity-Registry von Home Assistant "
                                  "(lokale Positivliste nicht prüfbar)")
    assert sleeps == list(DERIVED_SENSORS_RETRY_DELAYS_SECONDS) and angelegt == []


def test_ein_spaeter_eingetragenes_schreibziel_startet_im_zweiten_versuch(env, angelegt, sleeps, monkeypatch):
    _registry(env, {"number.shift_current": None})
    echte_registry = env.ha.entity_registry
    aufrufe = []

    def registry():
        aufrufe.append(1)
        if len(aufrufe) == 2:
            _registry(env)
        return echte_registry()

    monkeypatch.setattr(env.ha, "entity_registry", registry)
    _host(env).load()
    assert len(aufrufe) == 2 and sleeps == [DERIVED_SENSORS_RETRY_DELAYS_SECONDS[0]] and len(angelegt) == 1


def test_ein_verstoss_beim_start_mit_laufendem_boost_schreibt_nur_einmal_die_ruecknahme(env):
    """Entscheidung 1: verletzt ein Schreibziel beim Start die Regeln, waehrend ein Boost laeuft, nimmt
    der Konfigurationsfehler den Boost zurueck (sign_off_parts, ohne erneute Schreibziel-Pruefung): genau die Werte des
    Wiederherstellungspunkts, auch auf das verletzende Schreibziel, einmal; danach schreibt keine Neupruefung mehr."""
    save_backup(env.paths["BACKUP_PATH"], {
        "boost_active": True, "restore_point": {"curve": 1.0, "room_setpoint": 20.0, "heat_limit": 16.0}})
    _registry(env, {"number.heat_limit": {**THERME, "platform": "template"}})

    bridge = _start_bridge(env)

    assert isinstance(bridge, IdleBridge) and bridge.reason == "konfigurationsfehler"
    assert sorted(env.ha.writes) == [("number.curve_current", 1.0), ("number.heat_limit", 16.0),
                                     ("number.shift_current", 20.0)]
    assert not load_backup(env.paths["BACKUP_PATH"]).get("boost_active")
    geschrieben = list(env.ha.writes)
    assert _start_bridge(env).reason == "konfigurationsfehler"  # Neupruefung (wieder load)
    assert env.ha.writes == geschrieben
