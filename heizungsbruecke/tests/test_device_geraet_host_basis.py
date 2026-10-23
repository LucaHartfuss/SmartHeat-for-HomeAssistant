"""Gemeinsamer Teil der Geraete-Hosts; das Verhalten beider Hosts prueft
test_host_konformitaet.py, hier nur, was ohne Host gilt."""
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fakes import FakeClock

from smartheat_core import dateien
from smartheat_device.commands import Done
from smartheat_device.geraet_host_basis import (
    EV_RELOAD,
    SIGN_OFF_WAIT_SECONDS,
    GeraetHostBasis,
    abmeldung_abwarten,
    wiederherstellungspunkt,
)
from smartheat_runtime import aufraeumen, dokument_config
from smartheat_runtime.worker import RegulationWorker


class _Host(GeraetHostBasis):
    host, version, wunsch_herkunft = "test", "0.0.0", "test"

    def __init__(self, pfad: Path) -> None:
        super().__init__()
        self._pfad = pfad

    def aufraeumen_pfad(self) -> Path:
        return self._pfad

    def abgemeldet_pfad(self) -> Path:
        return self._pfad.with_name("abgemeldet.json")


def test_ein_neustart_aus_dem_geraetethread_waehrend_des_bindens_geht_nie_verloren(tmp_path):
    """Hauptthread bindet, Geraete-Thread fordert gleichzeitig an: genau ein EV_RELOAD erreicht den Worker."""
    for _ in range(200):
        host = _Host(tmp_path / "a.json")
        worker = RegulationWorker(FakeClock())
        kinds = []
        worker.register(EV_RELOAD, lambda event, ziel=kinds: ziel.append(event.kind))
        anfordern = threading.Thread(target=host.neustart_anfordern)
        anfordern.start()
        host.worker_binden(worker)
        anfordern.join()
        worker.run_pending()
        assert kinds == [EV_RELOAD]  # die Sperre serialisiert: entweder nachgestellt oder direkt, nie beides, nie keins


def test_der_neustart_ereignisname_ist_hostneutral():
    assert EV_RELOAD == "geraet_reload"


def test_ohne_aufraeumen_pfad_ist_die_basis_nicht_nutzbar(tmp_path):
    basis = GeraetHostBasis()
    with pytest.raises(NotImplementedError):
        basis.aufraeumen_pfad()


EINGERICHTET = {"anlage": "t1", "hebelsatz": "vaillant_vrc720", "raumfuehler": ["sensor.a"], "abo": "aktiv"}
LEER = {"anlage": None, "raumfuehler": []}  # wie der Server sie nach dem Erstkontakt schickt: raumfuehler [] != fehlend


def _angefordert(host: _Host) -> bool:
    worker = RegulationWorker(FakeClock())
    kinds = []
    worker.register(EV_RELOAD, lambda event: kinds.append(event.kind))
    host.worker_binden(worker)
    worker.run_pending()
    return kinds == [EV_RELOAD]


def test_leere_konfiguration_nach_keiner_startet_nicht_neu(tmp_path):
    """Im Ruhezustand "nicht eingerichtet" liest die Laufzeit kein Laufzeitfeld; ein Neustart risse den
    Befehl mit, der gleich nach dem Erstkontakt laeuft. Auch leer nach leer mit anderem Laufzeitfeld nicht."""
    assert dokument_config.laufzeit_relevant(None, LEER)  # ohne (c) ginge der Neustart hinaus
    host = _Host(tmp_path / "a.json")
    host.konfiguration_uebernommen(None, LEER)
    host.konfiguration_uebernommen(LEER, {**LEER, "abo": "inaktiv", "tagestick": "12:00"})
    assert not _angefordert(host)


@pytest.mark.parametrize("alt, neu", [(None, EINGERICHTET), (LEER, EINGERICHTET), (EINGERICHTET, LEER),
                                     (EINGERICHTET, {**EINGERICHTET, "abo": "inaktiv"})])
def test_eine_eingerichtete_konfiguration_startet_neu(tmp_path, alt, neu):
    """Gegenprobe zu (c): ist die alte oder die neue Konfiguration eingerichtet, bleibt der Neustart."""
    host = _Host(tmp_path / "a.json")
    host.konfiguration_uebernommen(alt, neu)
    assert _angefordert(host)


def test_leer_nach_leer_hebt_die_abmeldung_nicht_auf_und_startet_nicht_neu(tmp_path):
    """Die Abmelde-Markierung faellt nur durch eine eingerichtete Konfiguration derselben Anlage (dann Neustart, auch
    ohne Laufzeitaenderung); (c) aendert daran nichts."""
    host = _Host(tmp_path / "a.json")
    host.abgemeldet_pfad().write_text('{"anlage": "t1"}')
    host.konfiguration_uebernommen(None, LEER)
    assert not _angefordert(host) and host.abgemeldet_pfad().exists()
    host.konfiguration_uebernommen(EINGERICHTET, dict(EINGERICHTET))  # dieselbe Anlage, nichts Laufzeitrelevantes
    assert _angefordert(host) and not host.abgemeldet_pfad().exists()


# --- Start der Laufzeit: Konfiguration und Ursprungswerte ---

ALT = {**EINGERICHTET, "ursprungswerte": {"heat_limit": 16.0}}
OFFEN = aufraeumen.Markierung("t1", ALT, False)


def _mit_konfiguration(tmp_path, gespeichert, start_markierung=None) -> _Host:
    """Host, dessen Geraetekern `gespeichert` zeigt; ein spaeteres host.device.gespeichert wirkt wie eine neue
    Konfiguration."""
    host = _Host(tmp_path / "a.json")
    host.verbinden(SimpleNamespace(gespeichert=gespeichert, konfiguration=lambda: host.device.gespeichert))
    host.start_markierung = start_markierung
    return host


def test_laufzeit_konfiguration_ohne_markierung_ist_die_gespeicherte(tmp_path):
    assert _mit_konfiguration(tmp_path, EINGERICHTET).laufzeit_konfiguration() == (EINGERICHTET, False)
    assert _mit_konfiguration(tmp_path, None).laufzeit_konfiguration() == ({}, False)
    assert _mit_konfiguration(tmp_path, LEER).laufzeit_konfiguration() == (LEER, False)


def test_laufzeit_konfiguration_mit_markierung_startet_abgemeldet_mit_der_alten(tmp_path):
    konfiguration, leer_abmelden = _mit_konfiguration(tmp_path, LEER, OFFEN).laufzeit_konfiguration()
    assert (konfiguration, leer_abmelden) == (ALT, True)
    assert konfiguration is not ALT  # eine Kopie: die Markierung bleibt unveraendert


def test_laufzeit_konfiguration_eine_schon_eingerichtete_gespeicherte_geht_vor(tmp_path):
    """Kam vor dem Start der Laufzeit schon eine neue Einrichtung an, laeuft die Laufzeit mit ihr (wie bisher)."""
    neu = {**EINGERICHTET, "anlage": "t2"}
    assert _mit_konfiguration(tmp_path, neu, OFFEN).laufzeit_konfiguration() == (neu, False)


def test_ursprungswerte_server_markierung_hat_vorrang(tmp_path):
    """Steht das Zuruecksetzen aus, gelten die Werte der Markierung -- auch wenn danach eine neue Einrichtung (andere
    Anlage) eintrifft; ohne ausstehendes Zuruecksetzen gilt die aktuelle eingerichtete, sonst die der Laufzeit."""
    host = _mit_konfiguration(tmp_path, LEER, OFFEN)
    konfiguration, leer_abmelden = host.laufzeit_konfiguration()
    host.device.gespeichert = {**EINGERICHTET, "anlage": "t2", "ursprungswerte": {"heat_limit": 12.0}}
    assert host.ursprungswerte_server(konfiguration, leer_abmelden) == {"heat_limit": 16.0}
    # ohne Markierung: die frisch eingetroffene Konfiguration
    assert host.ursprungswerte_server(konfiguration, False) == {"heat_limit": 12.0}
    # ohne Markierung und ohne eingerichtete aktuelle Konfiguration: die der Laufzeit
    host.device.gespeichert = LEER
    assert host.ursprungswerte_server(ALT, False) == {"heat_limit": 16.0}


def test_wiederherstellungspunkt_aus_der_sicherung(tmp_path):
    pfad = tmp_path / "backup.json"
    assert wiederherstellungspunkt(pfad) == {}  # keine Sicherung
    dateien.schreiben_json(pfad, {"restore_point": {"heat_limit": 16.0}}, modus=dateien.PRIVAT)
    punkt = wiederherstellungspunkt(pfad)
    assert punkt == {"heat_limit": 16.0}
    punkt["heat_limit"] = 1.0
    assert wiederherstellungspunkt(pfad) == {"heat_limit": 16.0}  # eine Kopie
    dateien.schreiben_json(pfad, {"restore_point": [1]}, modus=dateien.PRIVAT)
    assert wiederherstellungspunkt(pfad) == {}
    dateien.schreiben_json(pfad, [1], modus=dateien.PRIVAT)
    assert wiederherstellungspunkt(pfad) == {}


def test_abmeldung_abwarten_fertig_erst_bei_abgemeldet_dieser_anlage(tmp_path):
    """sign_off wartet auf den Status "abgemeldet" dieser Anlage; werte ist der Wiederherstellungspunkt, zurueckgesetzt
    gilt ohne `grund`. Die Frist ist die uebergebene; sie wird nicht verlaengert."""
    backup = tmp_path / "backup.json"
    dateien.schreiben_json(backup, {"restore_point": {"heat_limit": 16.0}}, modus=dateien.PRIVAT)
    status: dict | None = None
    wartend = abmeldung_abwarten("t1", lambda: status, backup, 123.0)
    assert wartend.deadline == 123.0
    for noch_nicht in (None, {}, {"status": "regelt", "tenant_id": "t1"},
                       {"status": "abgemeldet", "tenant_id": "andere"}, {"status": "abgemeldet"}):
        status = noch_nicht
        assert wartend.check() is None
    status = {"status": "abgemeldet", "tenant_id": "t1"}
    assert wartend.check() == Done({"zurueckgesetzt": True, "werte": {"heat_limit": 16.0}})
    status = {"status": "abgemeldet", "tenant_id": "t1", "grund": "anlage_nicht_erreichbar"}
    assert wartend.check() == Done({"zurueckgesetzt": False, "werte": {"heat_limit": 16.0}})


def test_die_frist_von_sign_off_bleibt_24_stunden():
    assert SIGN_OFF_WAIT_SECONDS == 24 * 3600
