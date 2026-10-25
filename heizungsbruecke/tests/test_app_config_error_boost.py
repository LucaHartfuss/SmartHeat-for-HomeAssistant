"""Konfigurationsfehler bei laufendem Boost (Geraete-Pfad: Konfigurationsdokument mit
ungueltigem Tagestick)."""
from types import SimpleNamespace

import pytest
from bindung_beispiele import BINDINGS, VAILLANT_MYPYLLANT
from runtime_helpers import _start_bridge

from smartheat_runtime import app
from smartheat_runtime.backup_store import load_backup, save_backup

FEHLER = {"entity_shift_current": "climate.zone", "daily_trigger_time": "25:00"}  # ungueltig -> Konfigurationsfehler


def _boost(env):
    save_backup(env.paths["BACKUP_PATH"], {
        "boost_active": True, "restore_point": {"curve": 1.0, "room_setpoint": 20.0, "heat_limit": 16.0}})


def test_a_config_error_takes_back_a_running_boost(env):
    _boost(env)
    env.ha.states["climate.zone"] = "heat_cool"  # Zonen-Modus fuer das Vorbereiten vor dem Schreiben (FakeHa kennt ihn nicht)

    bridge = _start_bridge(env, **FEHLER)

    assert bridge.reason == "konfigurationsfehler"
    assert env.ha.writes  # Rueckkehr auf den Wiederherstellungspunkt
    assert not load_backup(env.paths["BACKUP_PATH"]).get("boost_active")


def test_a_config_error_without_restore_says_the_boost_values_remain(env, monkeypatch):
    _boost(env)
    monkeypatch.setattr("heizungsbruecke.host.HaHost.sign_off_parts", lambda self: None)

    _start_bridge(env, **FEHLER)

    texts = [text for _, text in env.ha.persistent]
    assert any("Boost-Werten" in text for text in texts)
    assert not any("behält ihre letzten Werte" in text for text in texts)


def test_config_error_boost_message_without_description_uses_neutral_names(env, monkeypatch):
    """Am Ablauf: ohne Beschreibung der Anlage nennt die Meldung zum Konfigurationsfehler alle Werte des
    Wiederherstellungspunkts unter neutralen Namen, auch die Heizgrenze."""
    _boost(env)
    monkeypatch.setattr("heizungsbruecke.host.HaHost.sign_off_parts", lambda self: None)

    _start_bridge(env, **FEHLER)

    texts = [text for _, text in env.ha.persistent]
    assert any("Heizkurve 1, Raum-Soll 20, Heizgrenze 16" in text for text in texts)


_WERTE = SimpleNamespace(restore_point={"curve": 0.9, "room_setpoint": 22.0, "level": 3.0, "heat_limit": 16.0},
                         originals={})


@pytest.mark.parametrize(("hebelsatz", "erwartet"), [
    ("vaillant_vrc720", "Heizkurve 0,9, Wunschtemperatur der Zone 22, Heizgrenze 16"),
    ("viessmann_vicare", "Neigung der Heizkurve 0,9, Niveau der Heizkurve 3, Raumtemperatur Normal 22"),
    ("weishaupt_wwp", "Heizkennlinie 0,9, Raumsolltemperatur Normal 22, Sommer-Winter-Umschaltung 16"),
])
def test_meldung_nennt_jeden_hebel_des_hebelsatzes(hebelsatz, erwartet):
    """Die Werte zum Von-Hand-Einstellen tragen den Anzeigenamen jedes Hebels des Hebelsatzes (Viessmann: das
    Niveau, Vaillant und Weishaupt: die Heizgrenze), nicht fest Kurve und Parallelverschiebung."""
    text = app._restore_values_text(_WERTE, BINDINGS[hebelsatz])

    assert text == erwartet


def test_abmeldung_nennt_den_ursprung_der_heizgrenze():
    """Beim Abmelden geht die Heizgrenze (restore_originals) auf ihren Ursprungswert zurueck; die Meldung nennt
    ihn (`originals`), nicht den gelernten Wert. Ohne `originals` (Konfigurationsfehler) und fuer Hebel ausserhalb von
    `originals` zaehlt der Wiederherstellungspunkt; fehlt der dort, der lokale Ursprungswert."""
    state = SimpleNamespace(restore_point={"curve": 0.9, "room_setpoint": 22.0, "heat_limit": 16.0},
                            originals={"heat_limit": 18.0, "curve": 1.2})
    description = VAILLANT_MYPYLLANT

    assert app._restore_values_text(state, description, originals={"heat_limit": 18.0}) == (
        "Heizkurve 0,9, Wunschtemperatur der Zone 22, Heizgrenze 18")
    assert app._restore_values_text(state, description) == (
        "Heizkurve 0,9, Wunschtemperatur der Zone 22, Heizgrenze 16")
    ohne_punkt = SimpleNamespace(restore_point={"curve": 0.9, "room_setpoint": 22.0}, originals={"heat_limit": 18.0})
    assert app._restore_values_text(ohne_punkt, description) == (
        "Heizkurve 0,9, Wunschtemperatur der Zone 22, Heizgrenze 18")
    # restore_originals-Hebel ohne Ursprungswert (leeres `originals`): der Wiederherstellungspunkt bleibt maßgeblich
    assert app._restore_values_text(
        SimpleNamespace(restore_point=state.restore_point, originals={}), description, originals={},
    ) == "Heizkurve 0,9, Wunschtemperatur der Zone 22, Heizgrenze 16"


def test_ohne_beschreibung_nennen_neutrale_namen_die_werte_des_wiederherstellungspunkts():
    """Konfiguration ungueltig (keine Beschreibung): neutrale Namen, Werte des Wiederherstellungspunkts, auch
    beim Abmelden."""
    state = SimpleNamespace(restore_point={"curve": 0.9, "level": 3.0, "room_setpoint": 22.0, "heat_limit": 16.0},
                            originals={"heat_limit": 18.0})

    assert app._restore_values_text(state, None, originals={"heat_limit": 18.0}) == "Heizkurve 0,9, Niveau 3, Raum-Soll 22, Heizgrenze 16"
    assert app._restore_values_text(SimpleNamespace(restore_point={}, originals={}), None) == (
        "Werte unbekannt")
