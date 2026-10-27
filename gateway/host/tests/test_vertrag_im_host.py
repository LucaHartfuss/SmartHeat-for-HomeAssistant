"""Vertragspruefung der Host-Dienste des Gateways gegen die Kopie im Geraetekern (smartheat_device.wire), die zeichengleich zum Server bleibt
(test_device_wire.py::test_the_copy_is_literal und Contract-Check 49). Der Test importiert den Updater, und der
importiert smartheat_core, smartheat_gateway und smartheat_host; er braucht also den Quellbaum des Gateways, nicht nur
die Standardbibliothek."""
from types import SimpleNamespace

from smartheat_device import wire

from smartheat_host import updater


def _probleme_47(updater, software_errors) -> list[str]:
    """Die Gruende des Gateway-Updaters (REASONS und der Rueckweg ohne Ablehnung
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


def test_led_liest_das_code_fenster_so_wie_die_laufzeit_es_schreibt(tmp_path):
    """Schreiber (smartheat_gateway.geraet_host.ZustandsDatei, Container) und Leser (led.state_from_file, Host) der
    Datei agent_state.json: `code_fenster` als echtes bool neben `zustand`; ein offenes Fenster zeigt code_sichtbar,
    ein geschlossenes den Zustand."""
    import json

    from smartheat_gateway.geraet_host import ZustandsDatei
    from smartheat_host import led

    pfad = tmp_path / "agent_state.json"
    datei = ZustandsDatei(pfad, lambda: "2027-01-15T07:00:00+00:00", lambda: 0.0)
    datei.update("nicht_uebernommen", True)
    assert json.loads(pfad.read_text())["code_fenster"] is True
    assert led.state_from_file(pfad) == "code_sichtbar"
    datei.update("nicht_uebernommen", False)
    assert json.loads(pfad.read_text())["code_fenster"] is False
    assert led.state_from_file(pfad) == "nicht_uebernommen"
    datei.update("nicht_uebernommen")  # ohne Angabe: kein Fenster
    assert led.state_from_file(pfad) == "nicht_uebernommen"
