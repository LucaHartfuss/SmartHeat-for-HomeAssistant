"""Das Soll-Ende im Ruhezustand (soll_ende.py): der Merker faellt erst nach Erfolg, ein Fehler wird wiederholt."""
import logging

import pytest

from smartheat_runtime.soll_ende import SollEnde, merker_setzen, ruhezustand_abarbeiten
from smartheat_runtime.worker import RegulationWorker

WIEDERHOLUNG = 60.0


class Quelle:
    def __init__(self, angezeigt, schreibbar=True, ausgeschaltet=False):
        self.angezeigt_wert, self.schreibbar, self.geschrieben = angezeigt, schreibbar, []
        self.ausgeschaltet = ausgeschaltet

    def aus(self):
        return self.ausgeschaltet

    def angezeigt(self):
        if isinstance(self.angezeigt_wert, Exception):
            raise self.angezeigt_wert
        return self.angezeigt_wert

    def schreiben(self, wert):
        if not self.schreibbar:
            return False
        self.geschrieben.append(wert)
        return True


@pytest.fixture
def aufbau(make_store, clock):
    store = make_store({"soll_ende_offen": True})
    return store, RegulationWorker(clock=clock)


def test_ohne_offenen_merker_passiert_nichts(make_store, clock):
    store, worker = make_store(), RegulationWorker(clock=clock)
    aufrufe = []
    ruhezustand_abarbeiten(worker, store, lambda: aufrufe.append(1), WIEDERHOLUNG)
    assert aufrufe == []


def test_der_wunsch_geht_an_die_quelle_und_der_merker_faellt_danach(aufbau):
    store, worker = aufbau
    quelle = Quelle(19.0)
    ruhezustand_abarbeiten(worker, store, lambda: SollEnde(21.0, quelle), WIEDERHOLUNG)
    assert quelle.geschrieben == [21.0] and store.state.soll_ende_offen is False


def test_ohne_wunsch_oder_quelle_faellt_der_merker_ohne_schreiben(aufbau):
    store, worker = aufbau
    ruhezustand_abarbeiten(worker, store, lambda: None, WIEDERHOLUNG)
    assert store.state.soll_ende_offen is False


@pytest.mark.parametrize("quelle", [Quelle(RuntimeError("weg")), Quelle(19.0, schreibbar=False)],
                         ids=["fehler", "schreiben_scheitert"])
def test_ein_fehlschlag_laesst_den_merker_stehen_und_wiederholt_im_takt(aufbau, clock, quelle):
    store, worker = aufbau
    ruhezustand_abarbeiten(worker, store, lambda: SollEnde(21.0, quelle), WIEDERHOLUNG)
    assert store.state.soll_ende_offen is True and quelle.geschrieben == []

    quelle.angezeigt_wert, quelle.schreibbar = 19.0, True  # die Anzeige ist unveraendert: nichts hat der Kunde geaendert
    worker.run_pending()
    assert store.state.soll_ende_offen is True  # noch nicht faellig

    clock.advance(WIEDERHOLUNG + 1)
    worker.run_pending()
    assert quelle.geschrieben == [21.0] and store.state.soll_ende_offen is False


def test_bei_unbekannter_anzeige_wartet_es_und_die_erste_bekannte_gilt_als_wert_des_kunden(aufbau, clock):
    store, worker = aufbau
    quelle = Quelle(None)
    ruhezustand_abarbeiten(worker, store, lambda: SollEnde(21.0, quelle), WIEDERHOLUNG)
    clock.advance(WIEDERHOLUNG + 1)
    worker.run_pending()
    assert store.state.soll_ende_offen is True and quelle.geschrieben == []

    quelle.angezeigt_wert = 18.0
    clock.advance(WIEDERHOLUNG + 1)
    worker.run_pending()
    assert store.state.soll_ende_offen is False and quelle.geschrieben == []


def test_aendert_der_kunde_nach_einem_gescheiterten_schreiben_die_anzeige_wird_nicht_mehr_geschrieben(aufbau, clock):
    store, worker = aufbau
    quelle = Quelle(19.0, schreibbar=False)
    ruhezustand_abarbeiten(worker, store, lambda: SollEnde(21.0, quelle), WIEDERHOLUNG)
    assert store.state.soll_ende_offen is True

    quelle.angezeigt_wert, quelle.schreibbar = 17.0, True  # der Kunde hat inzwischen etwas anderes eingestellt
    clock.advance(WIEDERHOLUNG + 1)
    worker.run_pending()

    assert quelle.geschrieben == [] and store.state.soll_ende_offen is False


def test_eine_ausgeschaltete_quelle_gilt_als_erledigt_ohne_zu_schreiben(aufbau, clock):
    store, worker = aufbau
    quelle = Quelle(19.0, ausgeschaltet=True)
    ruhezustand_abarbeiten(worker, store, lambda: SollEnde(21.0, quelle), WIEDERHOLUNG)
    assert quelle.geschrieben == [] and store.state.soll_ende_offen is False


def test_ein_fehler_beim_ermitteln_des_ziels_wird_wiederholt(aufbau, clock):
    store, worker = aufbau
    ziele = [RuntimeError("Konfiguration nicht lesbar"), SollEnde(21.0, Quelle(19.0))]

    def ziel():
        naechstes = ziele.pop(0)
        if isinstance(naechstes, Exception):
            raise naechstes
        return naechstes

    ruhezustand_abarbeiten(worker, store, ziel, WIEDERHOLUNG)
    assert store.state.soll_ende_offen is True
    clock.advance(WIEDERHOLUNG + 1)
    worker.run_pending()
    assert store.state.soll_ende_offen is False


def test_ist_der_merker_zwischendurch_weg_schreibt_die_wiederholung_nicht(aufbau, clock):
    store, worker = aufbau
    quelle = Quelle(None)
    ruhezustand_abarbeiten(worker, store, lambda: SollEnde(21.0, quelle), WIEDERHOLUNG)
    store.update(soll_ende_offen=False)
    quelle.angezeigt_wert = 18.0
    clock.advance(WIEDERHOLUNG + 1)
    worker.run_pending()
    assert quelle.geschrieben == []


def test_merker_setzen_haelt_den_wert_im_speicher_auch_wenn_die_datei_nicht_schreibbar_ist(make_store, monkeypatch, caplog):
    store = make_store()
    monkeypatch.setattr("smartheat_runtime.backup_store.save_backup", lambda *a, **k: (_ for _ in ()).throw(OSError("voll")))
    with caplog.at_level(logging.ERROR):
        merker_setzen(store, True)
    assert store.state.soll_ende_offen is True
    assert "Soll-Ende offen" in caplog.text
