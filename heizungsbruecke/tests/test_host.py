"""HA-Host der Laufzeit (Plan SHG G1, Praezisierung 6) im Geraete-Pfad: Konfiguration aus dem Konfigurationsdokument."""
import logging
from unittest.mock import MagicMock

import pytest
from fakes import FakeHa
from geraet_helpers import FakeGeraet, konfiguration_aus_optionen
from test_runtime import OPTIONS, _start_bridge, env  # noqa: F401  (Fixture)

from heizungsbruecke.derived_sensors import DerivedSensors
from heizungsbruecke.ha_api import HomeAssistantApi
from heizungsbruecke.host import DERIVED_SENSORS_RETRY_DELAYS_SECONDS, HaHost
from smartheat_runtime.app import IdleBridge, StartFailure
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


# --- A6-02: Positivliste und Formen auch fuer die uebernommene Konfiguration beim Start ---

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
    env.ha.states["climate.programm"] = "auto"
    loaded = _host(env, entity_operating_mode="climate.programm").load()
    assert loaded.manifest.refs["operating_mode"] == "climate.programm"
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
    """Spec 3.2 Nachtrag: die Entity heilt nicht von selbst; der Start wartet nicht, die Referenz fehlt in diesem Lauf im
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


@pytest.mark.parametrize("rolle_option", ["entity_heat_limit", "entity_curve_current"])
def test_eine_optionale_hebelrolle_ohne_zustand_wird_nie_fallen_gelassen(env, angelegt, sleeps, rolle_option):
    """Review Fixrunde A: eine Hebelrolle gehoert zur Anlagenidentitaet (lever_refs_identity). Fiele sie weg, meldete
    bind_to_tenant eine andere Anlage und setzte Wiederherstellungspunkt, Originale und Schreibzaehler zurueck. Ein
    Schreibziel ohne Zustand ist daher nicht pruefbar (Wiederholung, StartFailure), nie ungelesen."""
    env.ha.states.update(BASIS_ZUSTAENDE)
    with pytest.raises(StartFailure) as fehler:
        _host(env, **{**BASIS, rolle_option: "number.swu"}).load()
    assert fehler.value.key == "rollen_nicht_pruefbar"
    assert fehler.value.grund.endswith("zugeordnete Entity fehlt in Home Assistant – bitte SmartHeat neu konfigurieren")
    assert sleeps == list(DERIVED_SENSORS_RETRY_DELAYS_SECONDS) and angelegt == []


def test_gegenprobe_eine_optionale_hebelrolle_mit_zustand_bleibt_im_manifest(env, angelegt, sleeps):
    env.ha.states.update({**BASIS_ZUSTAENDE, "number.swu": 18.0})
    loaded = _host(env, **{**BASIS, "entity_heat_limit": "number.swu"}).load()
    assert loaded.manifest.refs["heat_limit"] == "number.swu" and sleeps == []


def test_eine_fallen_gelassene_lese_rolle_aendert_die_anlagenidentitaet_nicht(env, angelegt, sleeps):
    """Gegenprobe zur Hebelrolle: eine optionale Lese-Rolle (Energie) ohne Zustand startet, wird ungelesen gemeldet, und
    die Kennung aus den Hebelreferenzen bleibt dieselbe wie mit Zustand."""
    env.ha.states.update(BASIS_ZUSTAENDE)
    ohne = _host(env, **BASIS, entity_energy_thermal_heating="sensor.waerme").load()
    assert "energy_thermal_heating" not in ohne.manifest.refs and sleeps == []
    assert [n.key for n in ohne.notices] == ["entity_fehlt:energy_thermal_heating"]
    env.ha.states.update({"sensor.waerme": 8.0, "sensor.waerme::device_class": "energy",
                          "sensor.waerme::unit_of_measurement": "kWh"})
    mit = _host(env, **BASIS, entity_energy_thermal_heating="sensor.waerme").load()
    assert mit.manifest.refs["energy_thermal_heating"] == "sensor.waerme"
    assert lever_refs_identity(ohne.manifest) == lever_refs_identity(mit.manifest)


def test_gegenprobe_dieselbe_optionale_rolle_mit_zustand_bleibt_im_manifest(env, angelegt, sleeps):
    env.ha.states.update({"sensor.vorlauf": 35.0, "sensor.vorlauf::device_class": "temperature"})
    loaded = _host(env, entity_flow_temperature="sensor.vorlauf").load()
    assert loaded.manifest.refs["flow_temperature"] == "sensor.vorlauf" and loaded.notices == ()


def test_eine_batterie_ohne_zustand_wird_nicht_gelesen_und_gemeldet(env, angelegt, sleeps):
    env.ha.states.update({"sensor.gut": 80, "sensor.gut::device_class": "battery"})
    loaded = _host(env, battery_entities=["sensor.gut", "binary_sensor.weg_battery_low"]).load()
    assert [ref.ref for ref in loaded.config.battery_refs] == ["sensor.gut"]
    assert [n.key for n in loaded.notices] == ["entity_fehlt:batterien"]
    assert "binary_sensor.weg_battery_low" not in loaded.notices[0].message


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
