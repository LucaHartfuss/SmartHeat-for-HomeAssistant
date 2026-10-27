"""Werksreset im Container (GP8, GPC9): der Host (Knopf-Dienst) schreibt host/werksreset.json {seq, ts, phase}; der
Geraete-Host setzt die Ursprungswerte zurueck wie sign_off und antwortet in data/werksreset_bereit.json. Er loest nie einen
Reset aus und loescht nichts. Eine Anfrage gilt als neu je Paar (seq, ts); eine schon beantwortete beim Start nicht."""
import json
import logging

import pytest
from configs import leer
from geraet_welt import Welt

from smartheat_core import dateien
from smartheat_device.host import Anzeige
from smartheat_gateway import geraet_host as gh
from smartheat_runtime import dokument_config

TS = "2027-01-15T07:59:00+00:00"  # vor dem Wanduhr-Wert der Welt (2027-01-15T08:00:00)
ANLAGE = "test-tenant"


@pytest.fixture
def welt(tmp_path, clock):
    return Welt(tmp_path, clock)


@pytest.fixture
def neustarts(welt, clock):
    """Ersetzt den Host durch einen mit eigenem Neustart-Weg (Konstruktor-Argument)."""
    aufrufe: list[int] = []
    welt.host = gh.GatewayDeviceHost(welt.paths, welt.mirror, host_dir=welt.host_dir, clock=clock,
                                     wall=lambda: welt.wall["t"], neustart=lambda: aufrufe.append(1))
    welt.host.verbinden(welt.device)
    return aufrufe


def _anzeige():
    return Anzeige(zustand="verbunden", server_ok=True, device_state="uebernommen", eingerichtet=True,
                   hinweise=(), meldungen=(), konfiguration_abgelehnt=None)


def _takt(host):
    host.anzeigen(_anzeige())


def _anfrage(welt, seq=1, ts=TS, phase="angefordert"):
    dateien.schreiben_json(welt.host_dir / "werksreset.json", {"seq": seq, "ts": ts, "phase": phase}, modus=dateien.LESBAR)


def _bereit_schreiben(welt, inhalt):
    welt.paths.data.mkdir(parents=True, exist_ok=True)
    welt.paths.werksreset_bereit.write_text(json.dumps(inhalt))


def _bereit(welt):
    pfad = welt.paths.werksreset_bereit
    return json.loads(pfad.read_text()) if pfad.exists() else None


def _abgemeldet(welt, grund=None, anlage=ANLAGE):
    welt.host.zustand.status = {"status": "abgemeldet", "tenant_id": anlage, "grund": grund}


def test_reset_anfrage_eingerichtet_meldet_ab(welt, neustarts):
    _takt(welt.host)  # Grundlinie: keine Datei
    _anfrage(welt)
    _takt(welt.host)
    assert dokument_config.abmelden(welt.paths.abgemeldet, ANLAGE) is False  # die Markierung steht schon
    assert neustarts == [1]  # wie sign_off: Markierung, dann Neustart der Laufzeit
    assert _bereit(welt) is None  # solange die Laufzeit nicht abgemeldet zeigt, keine Antwort
    _takt(welt.host)
    assert neustarts == [1] and _bereit(welt) is None
    _abgemeldet(welt)
    _takt(welt.host)
    assert _bereit(welt) == {"seq": 1, "ts": "2027-01-15T08:00:00+00:00", "ursprungswerte": True}
    assert (welt.paths.werksreset_bereit.stat().st_mode & 0o777) == 0o644  # der Host liest
    for _ in range(3):
        _takt(welt.host)
    assert neustarts == [1]  # nichts doppelt


def test_reset_anfrage_nicht_eingerichtet_antwortet_sofort(welt, neustarts):
    welt.device.konf = leer()
    _anfrage(welt, seq=5)
    _takt(welt.host)
    assert _bereit(welt) == {"seq": 5, "ts": "2027-01-15T08:00:00+00:00", "ursprungswerte": False}
    assert neustarts == [] and not welt.paths.abgemeldet.exists()  # nichts abgemeldet, kein Neustart


def test_ohne_geraetekern_gilt_das_geraet_als_nicht_eingerichtet(welt, neustarts):
    welt.host.device = None
    _anfrage(welt)
    _takt(welt.host)
    assert _bereit(welt)["ursprungswerte"] is False and neustarts == []


def test_abgemeldet_mit_grund_beantwortet_nicht_der_host_loescht_nach_seiner_frist_trotzdem(welt, neustarts):
    _anfrage(welt)
    _takt(welt.host)
    _abgemeldet(welt, grund="treiber")  # Ursprungswerte nicht zurueckgestellt
    _takt(welt.host)
    assert _bereit(welt) is None
    _abgemeldet(welt, grund=None)  # die Laufzeit wiederholt die Rueckstellung und schafft es
    _takt(welt.host)
    assert _bereit(welt)["ursprungswerte"] is True


@pytest.mark.parametrize("status", [None, {"status": "regelt", "tenant_id": ANLAGE}, {"status": "abgemeldet", "tenant_id": "andere"}],
                         ids=["kein-status", "anderer-status", "andere-anlage"])
def test_nur_abgemeldet_fuer_diese_anlage_beantwortet(welt, neustarts, status):
    _anfrage(welt)
    welt.host.zustand.status = status
    _takt(welt.host)
    _takt(welt.host)
    assert _bereit(welt) is None


def test_bereits_abgemeldet_durch_die_markierung_setzt_keinen_zweiten_neustart(welt, neustarts):
    dokument_config.abmelden(welt.paths.abgemeldet, ANLAGE)
    _abgemeldet(welt)
    _anfrage(welt)
    _takt(welt.host)
    assert neustarts == [] and _bereit(welt)["ursprungswerte"] is True


def test_eine_beim_start_vorhandene_schon_beantwortete_anfrage_gilt_nicht_als_neu(welt, clock):
    _anfrage(welt, seq=2)
    dateien.schreiben_json(welt.paths.werksreset_bereit,
                           {"seq": 2, "ts": "2027-01-15T08:00:00+00:00", "ursprungswerte": True}, modus=dateien.LESBAR)
    aufrufe: list[int] = []
    neu = gh.GatewayDeviceHost(welt.paths, welt.mirror, host_dir=welt.host_dir, clock=clock, wall=lambda: welt.wall["t"],
                               neustart=lambda: aufrufe.append(1))
    neu.verbinden(welt.device)
    for _ in range(3):
        _takt(neu)
    assert aufrufe == [] and not welt.paths.abgemeldet.exists()
    assert _bereit(welt)["ts"] == "2027-01-15T08:00:00+00:00"  # nicht neu geschrieben


def test_eine_beim_start_vorhandene_unbeantwortete_anfrage_wird_bearbeitet(welt, clock):
    """Der Container startete neu, bevor er antwortete (Absturz, Stromzyklus ohne Erststart nicht denkbar): die Anfrage
    lebt noch (sonst haette der Host sie verworfen), die Markierung ist idempotent."""
    _anfrage(welt, seq=2)
    aufrufe: list[int] = []
    neu = gh.GatewayDeviceHost(welt.paths, welt.mirror, host_dir=welt.host_dir, clock=clock, wall=lambda: welt.wall["t"],
                               neustart=lambda: aufrufe.append(1))
    neu.verbinden(welt.device)
    _takt(neu)
    assert aufrufe == [1] and dokument_config.abmelden(welt.paths.abgemeldet, ANLAGE) is False


def test_eine_alte_antwort_gilt_nicht_fuer_eine_neue_anfrage(welt, neustarts):
    dateien.schreiben_json(welt.paths.werksreset_bereit,
                           {"seq": 1, "ts": "2026-01-01T00:00:00+00:00", "ursprungswerte": True}, modus=dateien.LESBAR)
    _anfrage(welt, seq=1, ts=TS)  # gleiche seq, die Antwort ist aelter als die Anfrage
    _takt(welt.host)
    assert neustarts == [1]


def test_eine_antwort_mit_spaeterem_zeitpunkt_und_gleicher_seq_gilt_als_beantwortet(welt, neustarts):
    _bereit_schreiben(welt, {"seq": 1, "ts": "2027-01-15T08:00:00+00:00", "ursprungswerte": False})
    _anfrage(welt, seq=1, ts=TS)
    _takt(welt.host)
    assert neustarts == [] and not welt.paths.abgemeldet.exists()


@pytest.mark.parametrize("bereit", [
    {"seq": 3, "ts": "2027-01-15T08:00:00+00:00"}, {"seq": True, "ts": "2027-01-15T08:00:00+00:00"},
    {"seq": 1, "ts": 5}, {"seq": 1, "ts": "2027-01-15T08:00:00"}, {"seq": 1}, [],
], ids=["andere-seq", "bool-seq", "zeit-kein-text", "ohne-zone", "ohne-zeit", "liste"])
def test_nur_eine_antwort_auf_genau_diese_anfrage_zaehlt_als_beantwortet(welt, neustarts, bereit):
    _bereit_schreiben(welt, bereit)
    _anfrage(welt, seq=1, ts=TS)
    _takt(welt.host)
    assert neustarts == [1]


def test_eine_neue_anfrage_nach_einer_beantworteten_wird_bearbeitet(welt, neustarts):
    _anfrage(welt, seq=1)
    _takt(welt.host)
    _abgemeldet(welt)
    _takt(welt.host)
    assert _bereit(welt)["seq"] == 1
    welt.paths.abgemeldet.unlink()
    welt.paths.werksreset_bereit.unlink()  # der Host hat die Daten geloescht
    _anfrage(welt, seq=1, ts="2027-01-15T08:05:00+00:00")  # gleiche seq (Protokoll verloren), anderer Zeitpunkt: neu
    _takt(welt.host)
    assert neustarts == [1, 1]


def test_phase_loeschen_und_unbrauchbare_dateien_loesen_nichts_aus(welt, neustarts):
    _anfrage(welt, phase="loeschen")
    _takt(welt.host)
    for inhalt in ("kaputt{", "[]", '{"seq": 1}', '{"seq": true, "ts": "x", "phase": "angefordert"}',
                   '{"seq": 1, "ts": 7, "phase": "angefordert"}'):
        (welt.host_dir / "werksreset.json").write_text(inhalt)
        _takt(welt.host)
    assert neustarts == [] and _bereit(welt) is None and not welt.paths.abgemeldet.exists()


def test_verschwindet_die_anfrage_waehrend_des_wartens_endet_das_warten(welt, neustarts):
    _anfrage(welt)
    _takt(welt.host)
    (welt.host_dir / "werksreset.json").write_text(json.dumps({"seq": 1, "ts": TS, "phase": "loeschen"}))
    _takt(welt.host)
    _abgemeldet(welt)
    _takt(welt.host)
    assert _bereit(welt) is None  # der Host loescht schon, eine Antwort waere ohne Leser


def test_ein_fehler_beim_beantworten_wirft_nie_und_wird_einmal_gemeldet(welt, neustarts, caplog):
    welt.device.konf = leer()
    welt.paths.data.mkdir(parents=True, exist_ok=True)
    (welt.paths.data / "werksreset_bereit.json").mkdir()  # ein Ordner an der Stelle der Datei: Schreiben scheitert
    _anfrage(welt)
    with caplog.at_level(logging.ERROR):
        for _ in range(4):
            _takt(welt.host)
    meldungen = [r for r in caplog.records if "Werksreset-Anfrage nicht bearbeitet" in r.message]
    assert len(meldungen) == 1


def test_ohne_eigenen_weg_stoesst_der_reset_den_neustart_der_laufzeit_an(welt):
    welt.host.worker_binden(welt.worker)
    _anfrage(welt)
    _takt(welt.host)
    assert "geraet_reload" in welt.kinds()  # EV_RELOAD an den Worker (neustart_anfordern)


# --- Verworfener Reset hebt die eigene Abmeldung auf (Ruling R32) ---

def _vermerk(welt):
    return json.loads(welt.paths.werksreset_abmeldung.read_text()) if welt.paths.werksreset_abmeldung.exists() else None


def _verworfen(welt):
    (welt.host_dir / "werksreset.json").unlink()


def _neuer_host(welt, clock, aufrufe):
    neu = gh.GatewayDeviceHost(welt.paths, welt.mirror, host_dir=welt.host_dir, clock=clock, wall=lambda: welt.wall["t"],
                               neustart=lambda: aufrufe.append(1))
    neu.verbinden(welt.device)
    return neu


def test_eine_verworfene_anfrage_hebt_die_abmeldung_wieder_auf(welt, neustarts, caplog):
    _anfrage(welt, seq=4)
    _takt(welt.host)
    assert welt.paths.abgemeldet.exists() and neustarts == [1]
    assert _vermerk(welt) == {"seq": 4, "ts": TS, "anlage": ANLAGE}
    assert (welt.paths.werksreset_abmeldung.stat().st_mode & 0o777) == 0o600
    _verworfen(welt)
    with caplog.at_level(logging.WARNING):
        _takt(welt.host)
    assert not welt.paths.abgemeldet.exists() and _vermerk(welt) is None
    assert neustarts == [1, 1]  # die Laufzeit startet mit der alten Konfiguration neu
    assert "Abmeldung der Anfrage aufgehoben" in caplog.text and "seq=4" in caplog.text
    for _ in range(3):
        _takt(welt.host)
    assert neustarts == [1, 1]  # nur einmal


def test_die_aufhebung_gilt_auch_nach_einem_neustart_des_containers(welt, clock):
    erste: list[int] = []
    alt = _neuer_host(welt, clock, erste)
    _anfrage(welt)
    _takt(alt)
    _verworfen(welt)  # der Container stuerzt ab, bevor er es sieht
    zweite: list[int] = []
    neu = _neuer_host(welt, clock, zweite)
    _takt(neu)
    assert not welt.paths.abgemeldet.exists() and _vermerk(welt) is None and zweite == [1]


def test_startet_der_container_vor_dem_verwerfen_wartet_er_und_hebt_danach_auf(welt, clock):
    _anfrage(welt)
    _takt(_neuer_host(welt, clock, []))  # Abmeldung gesetzt
    aufrufe: list[int] = []
    neu = _neuer_host(welt, clock, aufrufe)  # Neustart des Containers, die Anfrage liegt noch
    for _ in range(3):
        _takt(neu)
    assert welt.paths.abgemeldet.exists() and _vermerk(welt) is not None and aufrufe == []  # nichts aufgehoben
    _verworfen(welt)
    _takt(neu)
    assert not welt.paths.abgemeldet.exists() and aufrufe == [1]


def test_eine_abmeldung_des_servers_bleibt_auch_wenn_der_reset_verworfen_wird(welt, neustarts):
    dokument_config.abmelden(welt.paths.abgemeldet, ANLAGE)  # sign_off des Servers, vor dem Reset
    _anfrage(welt)
    _takt(welt.host)
    assert _vermerk(welt) is None  # nicht von diesem Reset
    _verworfen(welt)
    _takt(welt.host)
    assert welt.paths.abgemeldet.exists() and neustarts == []


def test_eine_abmeldung_des_servers_nach_der_anfrage_bleibt_ebenfalls(welt, neustarts):
    from smartheat_gateway import config as gateway_config

    _anfrage(welt)
    _takt(welt.host)
    gateway_config.abmeldung_vom_server(welt.paths)  # sign_off kommt, bevor der Reset verworfen wird
    _verworfen(welt)
    _takt(welt.host)
    assert welt.paths.abgemeldet.exists() and neustarts == [1]


def test_ein_zweiter_reset_nach_einem_verworfenen_behaelt_den_vermerk(welt, neustarts):
    _anfrage(welt, seq=1)
    _takt(welt.host)
    welt.host._reset_gesehen = None  # der Dienst haette die Datei zwischendurch nicht gesehen
    _anfrage(welt, seq=2, ts="2027-01-15T07:59:30+00:00")  # Abmeldung steht noch, neue Anfrage ersetzt die alte
    _takt(welt.host)
    assert _vermerk(welt)["seq"] == 2 and neustarts == [1]
    _verworfen(welt)
    _takt(welt.host)
    assert not welt.paths.abgemeldet.exists()


@pytest.mark.parametrize("zustand", ["loeschen", "kaputt", "ohne-phase", "unlesbar"])
def test_nur_eine_sicher_fehlende_anfrage_hebt_auf(welt, neustarts, zustand):
    _anfrage(welt)
    _takt(welt.host)
    datei = welt.host_dir / "werksreset.json"
    datei.unlink()
    if zustand == "loeschen":
        _anfrage(welt, phase="loeschen")  # der Host loescht gerade: kein Verwerfen
    elif zustand == "kaputt":
        datei.write_text("kaputt{")
    elif zustand == "ohne-phase":
        datei.write_text(json.dumps({"seq": 1, "ts": TS}))
    else:
        datei.mkdir()  # lesen scheitert mit einem OSError ausser "fehlt"
    for _ in range(3):
        _takt(welt.host)
    assert welt.paths.abgemeldet.exists() and _vermerk(welt) is not None and neustarts == [1]


def test_laesst_sich_die_markierung_nicht_entfernen_bleibt_der_vermerk_und_der_naechste_takt_versucht_es_erneut(
        welt, neustarts, monkeypatch):
    _anfrage(welt)
    _takt(welt.host)
    _verworfen(welt)
    monkeypatch.setattr(dokument_config, "abmeldung_aufheben", lambda pfad, anlage: False)
    _takt(welt.host)
    assert welt.paths.abgemeldet.exists() and _vermerk(welt) is not None and neustarts == [1]
    monkeypatch.undo()
    _takt(welt.host)
    assert not welt.paths.abgemeldet.exists() and neustarts == [1, 1]


def test_ein_unbrauchbarer_vermerk_hebt_nichts_auf_und_wird_entfernt(welt, neustarts):
    welt.paths.data.mkdir(parents=True, exist_ok=True)
    dokument_config.abmelden(welt.paths.abgemeldet, ANLAGE)
    welt.paths.werksreset_abmeldung.write_text("[]")
    _takt(welt.host)
    assert welt.paths.abgemeldet.exists() and _vermerk(welt) is None and neustarts == []
