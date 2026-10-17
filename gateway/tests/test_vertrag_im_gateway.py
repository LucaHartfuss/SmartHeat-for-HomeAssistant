"""Stufe 3 S8a-2 (A5-16, Audit 5 p5-ci): Vertragspruefungen, die nur Module des Gateways und des Geraetekerns lesen.
Frueher Teile der Checks 52 und 53 des Contract-Checks im Dev-Root (tools/contract_check.py); was dort ueber
Repo-Grenzen geht (Einstellschritte der Simulation gegen den Rundweg des Servers, Entdeckung und Kennung gegen die
Erkennung des Servers), bleibt im Contract-Check. Je Pruefung eine Funktion `_probleme_<nr>`, ein Test auf den echten
Modulen und Gegenproben, die zeigen, dass sie rot werden kann.

Schon als Modul-Tests vorhanden, darum hier ohne Kopie (Check 52): LED-Muster je Zustand
(gateway/host/tests/test_led.py::test_patterns_cover_exactly_the_device_states) und Texte der Diagnoseseite je Zustand
(test_diagnose.py::test_every_state_has_a_text), beide als Gleichheit mit wire.ZUSTAENDE."""
import importlib
import re
import sys
from types import SimpleNamespace

import pytest
from world import GatewayWorld

from smartheat_device import wire
from smartheat_gateway import befehle
from smartheat_gateway.drivers import registry

# Befehle, die der Geraetekern selbst eintraegt (smartheat_device.device.Device), nicht das Gateway.
GERAETEKERN_BEFEHLE = ("hinweis", "new_claim_code", "claim_state")


@pytest.fixture
def world(data_dir, monkeypatch, clock):
    world = GatewayWorld(data_dir, monkeypatch, clock)
    yield world
    world.close()


# --- Check 52 (Gateway-Teil): jeder Befehl des Vertrags hat genau einen Handler ---------------------------------------


def _probleme_52(handlers, kern, commands) -> list[str]:
    """Spec 5b 5.3/9: Jeder Befehl des Vertrags (wire.COMMANDS) hat im Gateway genau einen Handler, im Gateway
    (befehle.HANDLERS) oder im Geraetekern; keiner ist doppelt, keiner unbekannt."""
    handled = [*handlers, *kern]
    problems = []
    doppelt = sorted({kind for kind in handled if handled.count(kind) > 1})
    if doppelt:
        problems.append(f"Befehle doppelt behandelt: {doppelt}")
    fehlt, unbekannt = set(commands) - set(handled), set(handled) - set(commands)
    if fehlt:
        problems.append(f"Befehls-Handler: fehlt fuer {sorted(fehlt)}")
    if unbekannt:
        problems.append(f"Befehls-Handler: unbekannt {sorted(unbekannt)}")
    return problems


def test_check_52_jeder_befehl_hat_genau_einen_handler():
    assert _probleme_52(befehle.HANDLERS, GERAETEKERN_BEFEHLE, wire.COMMANDS) == []


def test_check_52_das_laufende_gateway_traegt_jeden_befehl_ein(world):
    """Die Liste GERAETEKERN_BEFEHLE stimmt: Geraetekern und Gateway zusammen tragen genau die Befehle des Vertrags
    ein."""
    world.start_runtime()
    assert world.device.befehle.kinds == set(wire.COMMANDS)


def test_check_52_meldet_einen_befehl_ohne_handler():
    commands = {**wire.COMMANDS, "neu": wire.COMMANDS["diagnostics"]}
    assert _probleme_52(befehle.HANDLERS, GERAETEKERN_BEFEHLE, commands) == ["Befehls-Handler: fehlt fuer ['neu']"]


def test_check_52_meldet_einen_doppelten_und_einen_unbekannten_handler():
    handlers = {**befehle.HANDLERS, "hinweis": None, "erfunden": None}
    assert _probleme_52(handlers, GERAETEKERN_BEFEHLE, wire.COMMANDS) == [
        "Befehle doppelt behandelt: ['hinweis']", "Befehls-Handler: unbekannt ['erfunden']"]


# --- Check 53 (Gateway-Teil): Treiberparameter ohne Geheimnis-Namen --------------------------------------------------

# Dieselbe Regel wie looks_secret in tools/contract_check.py (Check 53 fuer Server-Katalog, Entdeckung und
# ursprungswerte); eine Aenderung dort zieht hier nach.
_SECRET_PARTS = ("token", "secret", "passw", "credential", "apikey", "privatekey",
                 "kennwort", "schluessel", "schlüssel", "geheim", "zugang")  # deutsche Namen (Zugangsdaten)
_SECRET_KEY = re.compile(r"(?:^|[^a-z])keys?(?:[^a-z]|$)")  # "key" als Wortteil (api_key, private_key), nicht monkey


def looks_secret(name: str) -> bool:
    """Klingt ein Parametername nach einem Geheimnis (Gross-/Kleinschreibung egal)?"""
    lowered = str(name).lower()
    return any(part in lowered for part in _SECRET_PARTS) or bool(_SECRET_KEY.search(lowered))


def driver_parameter_names(drivers) -> dict[str, tuple[str, ...]]:
    """Parameternamen, die ein Gateway-Treiber fuer bindung.treiber.parameter deklariert: die identifizierenden
    Parameter und die Schluessel der DEFAULTS des Treibermoduls."""
    names = {}
    for driver_id, driver in sorted(drivers.items()):
        defaults = getattr(importlib.import_module(driver.__module__), "DEFAULTS", {})
        names[f"Gateway-Treiber {driver_id}"] = tuple(sorted({*driver.IDENTITY_PARAMETERS, *defaults}))
    return names


def _probleme_53(drivers) -> list[str]:
    """Spec 5b 7, 5b-S1-15: Geraetedokumente tragen keine Geheimnisse. Die Parameternamen der Gateway-Treiber
    (driver_parameter_names) klingen nicht nach Token, Secret, Passwort, Zugangsdaten oder Schluessel."""
    return [f"{source}: Parametername {name!r} klingt nach einem Geheimnis (Geraetedokumente bleiben frei davon)"
            for source, names in driver_parameter_names(drivers).items() for name in names if looks_secret(name)]


def test_check_53_treiberparameter_klingen_nicht_nach_geheimnissen():
    names = driver_parameter_names(registry.DRIVERS)
    assert set(names) == {"Gateway-Treiber simulation", "Gateway-Treiber vicare_cloud"}
    assert {"lever_set", "heizkreis", "poll_seconds"} <= set(names["Gateway-Treiber simulation"])
    assert {"installation_id", "gateway_serial", "device_id", "heizkreis"} <= set(names["Gateway-Treiber vicare_cloud"])
    assert _probleme_53(registry.DRIVERS) == []


def test_check_53_meldet_einen_geheimen_identifizierenden_parameter():
    fake = type("FakeDriver", (), {"IDENTITY_PARAMETERS": ("heizkreis", "api_key")})
    assert _probleme_53({**registry.DRIVERS, "fake": fake}) == [
        "Gateway-Treiber fake: Parametername 'api_key' klingt nach einem Geheimnis "
        "(Geraetedokumente bleiben frei davon)"]


def test_check_53_meldet_einen_geheimen_standardwert(monkeypatch):
    defaults = {"poll_seconds": 60, "refresh_token": ""}
    monkeypatch.setitem(sys.modules, "fake_treiber", SimpleNamespace(DEFAULTS=defaults))
    fake = type("FakeDriver", (), {"IDENTITY_PARAMETERS": ("heizkreis",), "__module__": "fake_treiber"})
    assert _probleme_53({"fake": fake}) == [
        "Gateway-Treiber fake: Parametername 'refresh_token' klingt nach einem Geheimnis (Geraetedokumente bleiben "
        "frei davon)"]


@pytest.mark.parametrize("name", ["client_secret", "access_token", "Password", "api_key", "private_key", "key",
                                  "Kennwort", "Schlüssel", "zugangsdaten"])
def test_check_53_regel_erkennt_geheimnis_namen(name):
    assert looks_secret(name)


@pytest.mark.parametrize("name", ["installation_id", "gateway_serial", "heizkreis", "lever_set", "poll_seconds",
                                  "monkey_count", "keyboard_layout", "zugriff_intervall"])
def test_check_53_regel_laesst_die_bekannten_namen_durch(name):
    assert not looks_secret(name)
