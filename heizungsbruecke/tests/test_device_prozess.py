"""Lebenszyklus des Geraeteprozesses, beide Hosts (Spec Stufe 3 S6 4.2/4.3). Die Host-Einstiege pruefen
test_main_geraet.py (HA) und gateway/tests/test_runtime_main.py."""
import errno
import logging
import threading
from types import SimpleNamespace

import pytest
from conftest import FakeClock

from smartheat_device import prozess
from smartheat_device.geraet_host_basis import EV_RELOAD
from smartheat_runtime import app
from smartheat_runtime import aufraeumen as markierung
from smartheat_runtime.runtime import EV_RECHECK
from smartheat_runtime.status import STATUS_ABGEMELDET, STATUS_KONFIGURATIONSFEHLER
from smartheat_runtime.worker import Event, RegulationWorker

ALT = {"anlage": "kunde1", "hebelsatz": "vaillant_vrc720", "verteilsystem": "Heizkoerper", "ursprungswerte": {}}


class _Thread:
    def __init__(self, order):
        self.order = order

    def join(self, timeout=None):
        self.order.append("join")

    def is_alive(self):
        return False


class _Device:
    def __init__(self, order, fehler=None):
        self.order, self.fehler = order, fehler

    def start(self):
        self.order.append("start")
        return _Thread(self.order)

    def stop(self):
        self.order.append("stop")
        if self.fehler:
            raise self.fehler


def _worker_mit(order, ausloesen):
    def run():
        order.append("run")
        ausloesen()
    return SimpleNamespace(worker=SimpleNamespace(run=run))


# --- beenden ---

def test_beenden_trennt_stoppt_den_hintergrund_wartet_und_trennt_noch_einmal():
    order = []
    prozess.beenden(_Device(order), _Thread(order), SimpleNamespace(stop=lambda: order.append("hintergrund")))
    assert order == ["stop", "hintergrund", "join", "stop"]


def test_beenden_wirft_nie_und_loggt_nur_die_fehlerart(caplog):
    order = []

    class _Kaputt(_Thread):
        def join(self, timeout=None):
            order.append("join")
            raise RuntimeError("join-geheim")

    with caplog.at_level(logging.WARNING):
        prozess.beenden(_Device(order, OSError("paho-geheim")), _Kaputt(order), None)
    assert order == ["stop", "join", "stop"]
    assert "OSError" in caplog.text and "RuntimeError" in caplog.text and "geheim" not in caplog.text


def test_beenden_wartet_standardmaessig_10_s_auf_den_geraete_thread():
    """JOIN_SECONDS liegt unter der stop_grace_period von 20 s im Gateway-Compose (gateway/tests/test_compose.py hat
    dafuer nur die Untergrenze JOIN_SECONDS + 5); beenden reicht den Wert unveraendert an thread.join weiter."""
    assert prozess.JOIN_SECONDS == 10.0
    fristen = []

    class _Aufzeichnend(_Thread):
        def join(self, timeout=None):
            fristen.append(timeout)

    prozess.beenden(_Device([]), _Aufzeichnend([]), None)
    prozess.beenden(_Device([]), _Aufzeichnend([]), None, join_seconds=3.0)
    assert fristen == [10.0, 3.0]


def test_beenden_warnt_nur_bei_einem_nach_dem_join_noch_lebenden_thread(caplog):
    class _Haengt(_Thread):
        def is_alive(self):
            return True

    with caplog.at_level(logging.WARNING):
        prozess.beenden(_Device([]), _Thread([]), None)
    assert "nicht beendet" not in caplog.text
    with caplog.at_level(logging.WARNING):
        prozess.beenden(_Device([]), _Haengt([]), None, join_seconds=2.0)
    assert "Geraete-Thread nach 2 s nicht beendet" in caplog.text


def test_beenden_ohne_geraete_thread_stoppt_nur():
    gestoppt = threading.Event()
    prozess.beenden(SimpleNamespace(stop=gestoppt.set), None, None)
    assert gestoppt.is_set()


def test_beenden_laesst_das_laufende_ereignis_des_geraete_threads_fertig_werden():
    gestoppt, fertig = threading.Event(), []

    def laufendes_ereignis():
        gestoppt.wait(5)
        fertig.append("software_soll")

    thread = threading.Thread(target=laufendes_ereignis, daemon=True)
    thread.start()
    prozess.beenden(SimpleNamespace(stop=gestoppt.set), thread, None)
    assert fertig == ["software_soll"] and not thread.is_alive()


def test_beenden_stoppt_auch_einen_link_den_der_letzte_takt_nach_dem_ersten_stop_oeffnete():
    """Restpunkt 5b-D2-4: Device.run fuehrt nach stop() noch einen Takt aus."""
    gestoppt = threading.Event()
    device = SimpleNamespace(link=SimpleNamespace(stopped=False))

    def stop():
        gestoppt.set()
        device.link.stopped = True

    device.stop = stop

    def letzter_takt():
        gestoppt.wait(5)
        device.link = SimpleNamespace(stopped=False)

    thread = threading.Thread(target=letzter_takt, daemon=True)
    thread.start()
    prozess.beenden(device, thread, None)
    assert not thread.is_alive() and device.link.stopped


# --- laufen ---

def test_ein_neustart_trennt_zuerst_dann_startet_der_host_neu():
    order = []

    def ausloesen():
        raise prozess.Neustart()

    prozess.laufen(_Device(order), None, lambda: _worker_mit(order, ausloesen), lambda: order.append("neustart"),
                   sleep=lambda s: order.append(("pause", s)))
    assert order == ["start", "run", "stop", "join", "stop", "neustart"]


def test_sigterm_endet_ohne_neustart():
    order = []

    def ausloesen():
        raise SystemExit(0)

    with pytest.raises(SystemExit):
        prozess.laufen(_Device(order), None, lambda: _worker_mit(order, ausloesen), lambda: order.append("neustart"))
    assert order == ["start", "run", "stop", "join", "stop"]


def test_ein_unerwarteter_fehler_trennt_pausiert_und_startet_neu(caplog):
    order = []

    def starten():
        raise KeyError("geheim")

    with caplog.at_level(logging.WARNING):
        prozess.laufen(_Device(order), None, starten, lambda: order.append("neustart"),
                       sleep=lambda s: order.append(("pause", s)))
    assert order == ["start", "stop", "join", "stop", ("pause", prozess.FEHLER_PAUSE_SECONDS), "neustart"]
    assert prozess.FEHLER_PAUSE_SECONDS == 60.0
    assert "KeyError" in caplog.text and "geheim" not in caplog.text


def test_sigterm_in_der_pause_endet_sofort_ohne_neustart():
    order = []

    def pause(seconds):
        order.append("pause")
        raise SystemExit(0)

    def starten():
        raise RuntimeError("kaputt")

    with pytest.raises(SystemExit):
        prozess.laufen(_Device(order), None, starten, lambda: order.append("neustart"), sleep=pause)
    assert order == ["start", "stop", "join", "stop", "pause"]


def test_ein_normal_endender_worker_trennt_ohne_neustart():
    order = []
    prozess.laufen(_Device(order), None, lambda: _worker_mit(order, lambda: None), lambda: order.append("neustart"))
    assert order == ["start", "run", "stop", "join", "stop"]


def test_vor_der_pause_ruft_laufen_den_rueckruf_des_hosts_nach_dem_trennen():
    """Abschlussreview S6 I-2: das Gateway meldet sich vor der Pause ungesund (runtime_main.vor_der_pause)."""
    order = []

    def ausloesen():
        raise RuntimeError("kaputt")

    prozess.laufen(_Device(order), None, lambda: _worker_mit(order, ausloesen), lambda: order.append("neustart"),
                   vor_pause=lambda: order.append("vor_pause"), sleep=lambda s: order.append(("pause", s)))
    assert order == ["start", "run", "stop", "join", "stop", "vor_pause", ("pause", 60.0), "neustart"]


@pytest.mark.parametrize("ausloesen", [prozess.Neustart(), None], ids=["neustart", "normales_ende"])
def test_ohne_fehler_kein_rueckruf_vor_der_pause(ausloesen):
    order = []

    def run():
        if ausloesen is not None:
            raise ausloesen

    prozess.laufen(_Device(order), None, lambda: _worker_mit(order, run), lambda: order.append("neustart"),
                   vor_pause=lambda: order.append("vor_pause"), sleep=lambda s: order.append(("pause", s)))
    assert "vor_pause" not in order and not any(isinstance(schritt, tuple) for schritt in order)


def test_ein_scheiternder_rueckruf_vor_der_pause_laesst_pause_und_neustart(caplog):
    order = []

    def vor_pause():
        order.append("vor_pause")
        raise OSError("geheim")

    def starten():
        raise KeyError("x")

    with caplog.at_level(logging.WARNING):
        prozess.laufen(_Device(order), None, starten, lambda: order.append("neustart"), vor_pause=vor_pause,
                       sleep=lambda s: order.append(("pause", s)))
    assert order[-3:] == ["vor_pause", ("pause", 60.0), "neustart"]
    assert "OSError" in caplog.text and "geheim" not in caplog.text


def test_ohne_eigene_pause_nimmt_laufen_time_sleep_zur_laufzeit(monkeypatch):
    order = []
    monkeypatch.setattr(prozess.time, "sleep", lambda s: order.append(("pause", s)))

    def starten():
        raise ValueError("x")

    prozess.laufen(_Device(order), None, starten, lambda: order.append("neustart"))
    assert ("pause", 60.0) in order and order[-1] == "neustart"


# --- reload_anhaengen und der Rueckruf neustart_der_laufzeit ---

def _bridge(reason):
    worker = RegulationWorker(FakeClock())
    return app.IdleBridge(worker=worker, status=None, reason=reason, neustart=prozess.neustart_der_laufzeit)


def test_reload_und_der_neustart_der_laufzeit_fuehren_in_den_neustart():
    """EV_RELOAD (Geraete-Host) und der Rueckruf, den die Hosts app.start uebergeben (Abo wieder aktiv, Neupruefung,
    Neustart nach dem Zuruecksetzen), enden beide mit Neustart; nichts Globales wird dafuer ueberschrieben."""
    bridge = _bridge(app.IDLE_NOT_CONFIGURED)
    prozess.reload_anhaengen(bridge)
    bridge.worker.post(Event(EV_RELOAD))
    with pytest.raises(prozess.Neustart):
        bridge.worker.run_pending()
    with pytest.raises(prozess.Neustart):
        prozess.neustart_der_laufzeit()


def test_jeder_neustart_wird_mit_laufzeit_startet_neu_angekuendigt(caplog):
    """tools/e2e/drive_gateway2.py zaehlt diese Zeile; ohne sie gilt jeder Neustart des Gateways dort als Absturz."""
    bridge = _bridge(app.IDLE_NOT_CONFIGURED)
    prozess.reload_anhaengen(bridge)
    bridge.worker.post(Event(EV_RELOAD))
    with caplog.at_level(logging.INFO), pytest.raises(prozess.Neustart):
        bridge.worker.run_pending()
    assert "Laufzeit startet neu" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.INFO), pytest.raises(prozess.Neustart):
        prozess.neustart_der_laufzeit()
    assert "Laufzeit startet neu" in caplog.text


def test_im_konfigurationsfehler_ist_die_neupruefung_ein_neustart():
    """Der Ruhezustand Konfigurationsfehler (app._fail_start ueber app._idle) prueft per Rueckruf neustart neu; mit dem
    Rueckruf der Hosts ist das der Neustart mit sauberem Trennen."""
    clock = FakeClock()
    bridge = app._idle(clock, None, STATUS_KONFIGURATIONSFEHLER, neustart=prozess.neustart_der_laufzeit,
                       recheck_after=app.CONFIG_RECHECK_SECONDS)
    clock.advance(app.CONFIG_RECHECK_SECONDS)
    with pytest.raises(prozess.Neustart):
        bridge.worker.run_pending()


def test_anlass_konfigurationsfehler_steht_vor_der_neustart_zeile_im_log(caplog):
    """Der Anlass steht vor dem Rueckruf, dessen Zeile auf "Laufzeit startet neu" endet (tools/e2e/drive_gateway2.py
    zaehlt sie). Ins Log nur der Anlass, keine Inhalte."""
    clock = FakeClock()
    bridge = app._idle(clock, None, STATUS_KONFIGURATIONSFEHLER, neustart=prozess.neustart_der_laufzeit,
                       recheck_after=app.CONFIG_RECHECK_SECONDS)
    clock.advance(app.CONFIG_RECHECK_SECONDS)
    with caplog.at_level(logging.INFO), pytest.raises(prozess.Neustart):
        bridge.worker.run_pending()
    assert [r.getMessage() for r in caplog.records][-2:] == [
        "Neupruefung nach Konfigurationsfehler", "Neustart der Laufzeit, Laufzeit startet neu"]


def test_anlass_zuruecksetzen_steht_vor_der_neustart_zeile_im_log(tmp_path, caplog):
    """Der dritte Anlass, Abo wieder aktiv, steht in test_runtime.py (abo.check_grace_end)."""
    pfad = tmp_path / "aufraeumen.json"
    markierung.markieren(pfad, ALT)
    bridge = _bridge(STATUS_ABGEMELDET)
    app._nach_zuruecksetzen(bridge, pfad, "kunde1")
    with caplog.at_level(logging.INFO), pytest.raises(prozess.Neustart):
        bridge.worker.run_pending()
    assert [r.getMessage() for r in caplog.records] == [
        "Zuruecksetzen nach leerer Konfiguration erledigt", "Neustart der Laufzeit, Laufzeit startet neu"]


def test_im_ruhezustand_abgemeldet_bleibt_die_neupruefung_ein_erneutes_zuruecksetzen():
    bridge = _bridge(STATUS_ABGEMELDET)
    retries = []
    bridge.worker.register(EV_RECHECK, retries.append)
    prozess.reload_anhaengen(bridge)
    bridge.worker.post(Event(EV_RECHECK))
    bridge.worker.run_pending()
    assert len(retries) == 1


# --- aufraeumen_beim_start ---

def _aufraeumen(pfad, gespeichert, fertig=True):
    gelesen, geloescht = [], []
    prozess.aufraeumen_beim_start(pfad, lambda: gelesen.append(1) or gespeichert,
                                  lambda: geloescht.append(1) or fertig, wer="das Geraet")
    return gelesen, geloescht


def test_ohne_markierung_wird_nichts_gelesen_und_nichts_geloescht(tmp_path):
    assert _aufraeumen(tmp_path / "aufraeumen.json", None) == ([], [])


def test_neu_eingerichtet_verwirft_die_markierung_und_behaelt_den_zustand(tmp_path):
    pfad = tmp_path / "aufraeumen.json"
    markierung.markieren(pfad, ALT)
    assert _aufraeumen(pfad, {**ALT, "anlage": "kunde2"})[1] == [] and not pfad.exists()


def test_ausstehendes_zuruecksetzen_loescht_nichts(tmp_path):
    pfad = tmp_path / "aufraeumen.json"
    markierung.markieren(pfad, ALT)
    assert _aufraeumen(pfad, {"anlage": None})[1] == [] and pfad.exists()


def test_nach_dem_zuruecksetzen_wird_geloescht_und_die_markierung_entfernt(tmp_path):
    pfad = tmp_path / "aufraeumen.json"
    markierung.markieren(pfad, ALT)
    markierung.zurueckgesetzt(pfad, "kunde1")
    assert _aufraeumen(pfad, {"anlage": None})[1] == [1] and not pfad.exists()


def test_unfertiges_loeschen_behaelt_die_markierung_fuer_den_naechsten_start(tmp_path):
    pfad = tmp_path / "aufraeumen.json"
    markierung.markieren(pfad, ALT)
    markierung.zurueckgesetzt(pfad, "kunde1")
    assert _aufraeumen(pfad, {"anlage": None}, fertig=False)[1] == [1] and pfad.exists()


def test_eine_unlesbare_markierung_bleibt_und_es_wird_nichts_geloescht(tmp_path, monkeypatch):
    pfad = tmp_path / "aufraeumen.json"
    markierung.markieren(pfad, ALT)
    monkeypatch.setattr(markierung, "entscheiden", lambda p, g: markierung.BEHALTEN)
    assert _aufraeumen(pfad, {"anlage": None})[1] == [] and pfad.exists()


def test_eine_markierung_mit_unbekannter_existenz_kippt_den_start_nicht(tmp_path, monkeypatch):
    """Path.exists wirft bei EIO im stat: der Start geht weiter, nichts wird gelesen oder geloescht."""
    pfad = tmp_path / "aufraeumen.json"
    markierung.markieren(pfad, ALT)

    def kaputt(self, **kwargs):
        raise OSError(errno.EIO, "geheim")

    monkeypatch.setattr(type(pfad), "exists", kaputt)
    assert _aufraeumen(pfad, {"anlage": None}) == ([], [])


def test_eine_zwischen_entscheidung_und_loeschen_verschwundene_markierung_kippt_den_start_nicht(tmp_path, monkeypatch):
    """Rennen im Zweig VERWERFEN: die Markierung ist nach entscheiden() schon weg; das Loeschen wirft nicht."""
    pfad = tmp_path / "aufraeumen.json"
    markierung.markieren(pfad, ALT)

    def verschwindet(p, gespeichert):
        p.unlink()
        return markierung.VERWERFEN

    monkeypatch.setattr(markierung, "entscheiden", verschwindet)
    assert _aufraeumen(pfad, {**ALT, "anlage": "kunde2"})[1] == [] and not pfad.exists()


def test_entfernt_der_loeschen_rueckruf_die_markierung_selbst_ist_das_kein_fehler(tmp_path):
    """Rennen im Zweig LOESCHEN: der Rueckruf des Hosts hat die Markierung schon entfernt und meldet fertig."""
    pfad = tmp_path / "aufraeumen.json"
    markierung.markieren(pfad, ALT)
    markierung.zurueckgesetzt(pfad, "kunde1")
    geloescht = []

    def loeschen():
        pfad.unlink()
        geloescht.append(1)
        return True

    prozess.aufraeumen_beim_start(pfad, lambda: {"anlage": None}, loeschen, wer="das Geraet")
    assert geloescht == [1] and not pfad.exists()
