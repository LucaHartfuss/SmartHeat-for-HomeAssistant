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


def test_eine_rolle_ohne_zustand_wird_wiederholt_und_scheitert_mit_festem_schluessel(env, angelegt, sleeps):
    """Optionale Rolle: die Entity existiert (entity_exists), hat aber keinen Zustand: nicht pruefbar, kein Urteil.
    Wiederholung wie bei fehlenden Entities, nach dem Budget StartFailure mit stabilem Schluessel."""
    host = _host(env, entity_operating_mode="climate.programm")
    with pytest.raises(StartFailure) as fehler:
        host.load()
    assert fehler.value.key == "rollen_nicht_pruefbar"
    assert sleeps == list(DERIVED_SENSORS_RETRY_DELAYS_SECONDS)
    assert angelegt == []


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
    assert any("1" in m for m in meldungen) and not any("notify.email" in m for m in meldungen)
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
