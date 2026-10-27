"""Uebernahme-Code des Gateways nur nach Knopfdruck (SG-02, GPC21, S-2): host/knopf.json {seq, ts} vom Knopf-Dienst,
neuer Code je Druck (hoechstens einer je 60 s, sonst verlaengert der Druck nur), Fenster 15 min, nur im Speicher.
Ein Druck ist eine Aenderung des Paars (seq, ts) gegenueber dem zuletzt gesehenen (Ruling R30)."""
import json
import logging

import pytest
from geraet_welt import Welt

from smartheat_core import dateien
from smartheat_device.host import Anzeige
from smartheat_gateway import geraet_host as gh
from smartheat_gateway.diagnose import CODE_HINWEIS_TEXTE, render_html, seite

TS = "2027-01-15T07:00:00+00:00"


@pytest.fixture
def welt(tmp_path, clock):
    return Welt(tmp_path, clock)


def _anzeige(zustand="nicht_uebernommen", **changes):
    values = dict(zustand=zustand, server_ok=True, device_state="nicht_uebernommen", eingerichtet=False,
                  hinweise=(), meldungen=(), konfiguration_abgelehnt=None)
    values.update(changes)
    return Anzeige(**values)


def _druck(welt, seq, ts=TS):
    dateien.schreiben_json(welt.host_dir / "knopf.json", {"seq": seq, "ts": ts}, modus=dateien.LESBAR)


def _takt(welt, anzeige=None):
    welt.host.anzeigen(anzeige or _anzeige())


def _led_datei(welt):
    return json.loads(welt.paths.agent_state.read_text())


def _neu_gestartet(welt, clock):
    """Ein frischer Prozess auf denselben Dateien (Neustart des Containers oder Stromzyklus)."""
    host = gh.GatewayDeviceHost(welt.paths, welt.mirror, host_dir=welt.host_dir, clock=clock,
                                wall=lambda: welt.wall["t"])
    host.verbinden(welt.device)
    return host


def test_ohne_druck_nie_ein_code(welt, clock):
    for _ in range(5):
        _takt(welt)
        clock.advance(400.0)
    assert welt.device.code_aufrufe == 0 and welt.host.code_sichtbar() is False
    assert _led_datei(welt)["code_fenster"] is False
    # Neustart mit vorhandener knopf.json: die erste gelesene Datei ist nur Grundlinie (GPC2)
    _druck(welt, 7)
    neu = _neu_gestartet(welt, clock)
    neu.anzeigen(_anzeige())
    clock.advance(10.0)
    neu.anzeigen(_anzeige())
    assert welt.device.code_aufrufe == 0 and neu.code_sichtbar() is False
    assert neu.code_fenster_bis is None and _led_datei(welt)["code_fenster"] is False


def test_ein_druck_im_fenster_ueberlebt_keinen_neustart(welt, clock):
    _takt(welt)
    _druck(welt, 1)
    _takt(welt)
    assert welt.host.code_sichtbar() is True
    neu = _neu_gestartet(welt, clock)
    neu.anzeigen(_anzeige())
    assert neu.code_sichtbar() is False and welt.device.code_aufrufe == 1


def test_druck_erzeugt_neuen_code_und_meldet_ihn(welt):
    _takt(welt)  # Grundlinie: keine Datei
    assert welt.device.code_aufrufe == 0
    _druck(welt, 1)
    _takt(welt)
    assert welt.device.code_aufrufe == 1  # der Geraetekern meldet den Hash dem Server
    assert welt.host.code_sichtbar() is True
    data = seite(welt.host, welt.device)
    assert data["code"] == welt.device.identity.claim_code and data["geraet"] == welt.device.identity.device_id
    assert "ABCDEFGHIJKL" in render_html(data, None)
    # sofort in der LED-Datei, nicht erst im Minutentakt
    assert _led_datei(welt)["code_fenster"] is True
    _takt(welt)
    assert welt.device.code_aufrufe == 1  # derselbe Druck zaehlt nur einmal


@pytest.mark.parametrize("zustand", ["startet", "gesperrt"])
def test_code_nur_solange_code_zeigen(welt, zustand):
    _takt(welt)
    _druck(welt, 1)
    _takt(welt, _anzeige(zustand))
    assert welt.host.code_sichtbar() is False and _led_datei(welt)["code_fenster"] is False
    assert seite(welt.host, welt.device)["code"] is None


def test_ohne_druck_keine_geraete_id_und_kein_code_auf_der_seite(welt):
    _takt(welt)
    data = seite(welt.host, welt.device)
    assert data["code"] is None and data["geraet"] is None and data["code_hinweis"] is None
    page = render_html(data, None)
    assert "ABCDEFGHIJKL" not in page and "shg-aaaaaaaaaaaaaaaa" not in page


@pytest.mark.parametrize(("abstand", "neu"), [(59.9, False), (60.0, True), (61.0, True)])
def test_zweiter_druck_binnen_60s_verlaengert_nur(welt, clock, abstand, neu):
    _takt(welt)
    _druck(welt, 1)
    _takt(welt)
    clock.advance(abstand)
    _druck(welt, 2)
    _takt(welt)
    assert welt.device.code_aufrufe == (2 if neu else 1)
    # in beiden Faellen laeuft das Fenster ab dem zweiten Druck neu: 899 s danach offen, 901 s danach zu
    clock.advance(899.0)
    _takt(welt)
    assert welt.host.code_sichtbar() is True
    clock.advance(2.0)
    _takt(welt)
    assert welt.host.code_sichtbar() is False


def test_ein_scheiterter_druck_zaehlt_nicht_fuer_die_drossel(welt, clock):
    welt.device.code_ergebnis = "keine_verbindung"
    _takt(welt)
    _druck(welt, 1)
    _takt(welt)
    clock.advance(1.0)
    welt.device.code_ergebnis = "neu"
    _druck(welt, 2)
    _takt(welt)
    assert welt.device.code_aufrufe == 2 and welt.host.code_sichtbar() is True
    assert welt.host.code_hinweis() is None  # der Erfolg loescht den Hinweis


def test_code_nach_15_minuten_unsichtbar(welt, clock):
    _takt(welt)
    _druck(welt, 1)
    _takt(welt)
    clock.advance(gh.CODE_FENSTER_SEKUNDEN - 0.1)
    _takt(welt)
    assert welt.host.code_sichtbar() is True and _led_datei(welt)["code_fenster"] is True
    assert seite(welt.host, welt.device)["code"] == "ABCDEFGHIJKL"
    clock.advance(0.1)
    _takt(welt)
    assert welt.host.code_sichtbar() is False and _led_datei(welt)["code_fenster"] is False
    data = seite(welt.host, welt.device)
    assert data["code"] is None and data["geraet"] is None


def test_ohne_server_kein_code_und_hinweis(welt):
    welt.device.code_ergebnis = "keine_verbindung"
    _takt(welt)
    _druck(welt, 1)
    _takt(welt)
    assert welt.host.code_sichtbar() is False and _led_datei(welt)["code_fenster"] is False
    data = seite(welt.host, welt.device)
    assert data["code"] is None and data["code_hinweis"] == "keine_verbindung"
    page = render_html(data, None)
    assert "Keine Verbindung zum Server – kein Code" in page and "ABCDEFGHIJKL" not in page


def test_der_hinweis_verfaellt_nach_15_minuten(welt, clock):
    welt.device.code_ergebnis = "keine_verbindung"
    _takt(welt)
    _druck(welt, 1)
    _takt(welt)
    clock.advance(gh.CODE_FENSTER_SEKUNDEN)
    assert seite(welt.host, welt.device)["code_hinweis"] is None


def test_ein_scheiternder_zweiter_druck_schliesst_das_fenster(welt, clock):
    _takt(welt)
    _druck(welt, 1)
    _takt(welt)
    clock.advance(gh.CODE_ABSTAND_SEKUNDEN)
    welt.device.code_ergebnis = "keine_verbindung"
    _druck(welt, 2)
    _takt(welt)
    assert welt.host.code_sichtbar() is False and _led_datei(welt)["code_fenster"] is False


def test_uebernommenes_geraet_zeigt_uebernommen(welt):
    welt.device.code_ergebnis = "uebernommen"
    _takt(welt)
    _druck(welt, 1)
    _takt(welt, _anzeige("wartet_auf_einrichtung", device_state="uebernommen"))
    assert welt.host.code_sichtbar() is False and _led_datei(welt)["code_fenster"] is False
    data = seite(welt.host, welt.device)
    assert data["code"] is None and data["code_hinweis"] == "uebernommen"
    page = render_html(data, None)
    assert CODE_HINWEIS_TEXTE["uebernommen"] == "übernommen"
    assert "<p>übernommen</p>" in page and "ABCDEFGHIJKL" not in page


def test_ein_geraet_das_uebernommen_wird_schliesst_das_sichtbare_fenster(welt):
    _takt(welt)
    _druck(welt, 1)
    _takt(welt)
    assert welt.host.code_sichtbar() is True
    _takt(welt, _anzeige("wartet_auf_einrichtung", device_state="uebernommen"))
    assert welt.host.code_sichtbar() is False and _led_datei(welt)["code_fenster"] is False
    assert seite(welt.host, welt.device)["code"] is None


# --- Ruling R30: ein Druck ist eine Aenderung des Paars (seq, ts) ---

def test_zurueckgesetzte_seq_ist_ein_druck(welt, clock):
    _druck(welt, 5, "2027-01-15T07:00:00+00:00")
    _takt(welt)  # Grundlinie (5, ts)
    _druck(welt, 1, "2027-01-15T08:00:00+00:00")  # knopf.json zerstoert, Host zaehlt wieder bei 1
    _takt(welt)
    assert welt.device.code_aufrufe == 1


def test_gleiches_paar_ist_kein_druck_anderer_zeitstempel_schon(welt, clock):
    _druck(welt, 5)
    _takt(welt)
    _druck(welt, 5)
    _takt(welt)
    assert welt.device.code_aufrufe == 0
    _druck(welt, 5, "2027-01-15T07:00:01+00:00")
    _takt(welt)
    assert welt.device.code_aufrufe == 1


def test_fehlende_datei_beim_start_ist_grundlinie_und_ihr_erscheinen_ein_druck(welt):
    _takt(welt)
    assert welt.device.code_aufrufe == 0
    _druck(welt, 1)
    _takt(welt)
    assert welt.device.code_aufrufe == 1


def test_verschwundene_datei_ist_kein_druck_ihr_wiederkehren_schon(welt, clock):
    _druck(welt, 3)
    _takt(welt)
    (welt.host_dir / "knopf.json").unlink()
    _takt(welt)
    assert welt.device.code_aufrufe == 0
    clock.advance(1.0)
    _druck(welt, 3)  # dasselbe Paar nach dem Verschwinden: der Host hat neu geschrieben
    _takt(welt)
    assert welt.device.code_aufrufe == 1


@pytest.mark.parametrize("inhalt", ["{kaputt", "[]", '{"seq": "1", "ts": "x"}', '{"seq": true, "ts": "x"}',
                                    '{"seq": 1}', '{"seq": 1, "ts": 5}', ""])
def test_unlesbare_datei_ist_kein_druck_und_wird_nur_einmal_gemeldet(welt, inhalt, caplog):
    _druck(welt, 4)
    _takt(welt)
    (welt.host_dir / "knopf.json").write_text(inhalt)
    with caplog.at_level(logging.WARNING, logger=gh.__name__):
        for _ in range(3):
            _takt(welt)
    assert welt.device.code_aufrufe == 0
    assert len([r for r in caplog.records if "knopf.json" in r.getMessage()]) == 1
    _druck(welt, 4)  # unveraendert wieder lesbar: weiterhin kein Druck
    _takt(welt)
    assert welt.device.code_aufrufe == 0
    _druck(welt, 1, "2027-01-15T09:00:00+00:00")  # der Host setzt nach einer zerstoerten Datei seq auf 1
    _takt(welt)
    assert welt.device.code_aufrufe == 1


def test_unlesbare_datei_beim_start_ist_grundlinie_keine_datei(welt):
    (welt.host_dir).mkdir(parents=True)
    (welt.host_dir / "knopf.json").write_text("{kaputt")
    _takt(welt)
    assert welt.device.code_aufrufe == 0
    _druck(welt, 1)
    _takt(welt)
    assert welt.device.code_aufrufe == 1
