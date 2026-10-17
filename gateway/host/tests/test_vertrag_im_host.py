"""Stufe 3 S8a-2 (A5-16, Audit 5 p5-ci): Vertragspruefung der Host-Dienste des Gateways, frueher Check 47 des
Contract-Checks im Dev-Root (tools/contract_check.py). Der Check las updater.py als Text gegen SOFTWARE_ERRORS des
Servers; hier gegen die Kopie im Geraetekern (smartheat_device.wire), die zeichengleich zum Server bleibt
(test_device_wire.py::test_the_copy_is_literal und Contract-Check 49). Der Test importiert den Updater, und der
importiert smartheat_core, smartheat_gateway und smartheat_host; er braucht also den Quellbaum des Gateways, nicht nur
die Standardbibliothek."""
from types import SimpleNamespace

from smartheat_device import wire

from smartheat_host import updater


def _probleme_47(updater, software_errors) -> list[str]:
    """G3-Restpunkt, Spec 5b 5.3: Die Gruende des Gateway-Updaters (REASONS und der Rueckweg ohne Ablehnung
    RETRY_REASON) sind Gruende des Vertrags (SOFTWARE_ERRORS); die Laufzeit meldet sie als up/result (dokument
    software)."""
    allowed = set(software_errors)
    problems = []
    retry = getattr(updater, "RETRY_REASON", None)
    if not isinstance(retry, str):
        problems.append("RETRY_REASON fehlt im Gateway-Updater")
    elif retry not in allowed:
        problems.append(f"RETRY_REASON {retry!r} steht nicht in SOFTWARE_ERRORS")
    reasons = getattr(updater, "REASONS", None)
    if not isinstance(reasons, tuple):
        problems.append("REASONS fehlt im Gateway-Updater")
    else:
        problems += [f"Updater-Grund {reason!r} steht nicht in SOFTWARE_ERRORS" for reason in reasons
                     if reason not in allowed]
    return problems


def test_check_47_gruende_des_updaters_sind_gruende_des_vertrags():
    assert updater.REASONS and updater.RETRY_REASON in updater.REASONS
    assert _probleme_47(updater, wire.SOFTWARE_ERRORS) == []


def test_check_47_meldet_gruende_ausserhalb_des_vertrags():
    problems = _probleme_47(updater, ("manifest_ungueltig",))
    assert "RETRY_REASON 'server_unerreichbar' steht nicht in SOFTWARE_ERRORS" in problems
    assert "Updater-Grund 'ungesund' steht nicht in SOFTWARE_ERRORS" in problems
    assert "Updater-Grund 'manifest_ungueltig' steht nicht in SOFTWARE_ERRORS" not in problems


def test_check_47_meldet_fehlende_konstanten():
    assert _probleme_47(SimpleNamespace(), wire.SOFTWARE_ERRORS) == [
        "RETRY_REASON fehlt im Gateway-Updater", "REASONS fehlt im Gateway-Updater"]
