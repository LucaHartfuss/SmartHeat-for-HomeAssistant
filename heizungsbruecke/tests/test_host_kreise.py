"""Der HA-Host mit zwei Heizkreisen: je Kreis eigene Bindung, Soll-Speicher, Trigger und Hilfssensoren, gemeinsam
Geraet, HA-API und Supervisor."""
import copy
import logging
import threading
from datetime import datetime
from types import SimpleNamespace

import pytest
from fakes import schreibziel_eintrag
from geraet_helpers import FakeDevice, FakeSupervisor, konfiguration_aus_optionen
from runtime_helpers import OPTIONS

import heizungsbruecke.__main__ as main_module
from heizungsbruecke import config
from heizungsbruecke.derived_sensors import DerivedSensors
from smartheat_device.geraet_host_basis import abmeldung_abwarten
from smartheat_device.laufzeit import RuntimeChannel
from smartheat_device.prozess import Neustart
from smartheat_runtime import app, aufraeumen, dokument_config, entitlement
from smartheat_runtime.backup_store import load_backup, save_backup
from smartheat_runtime.runtime import Runtime


class _Geraet(FakeDevice):
    """FakeDevice mit einem Kanal je Kreis, der die gesendeten Nachrichten mit der Kreisnummer festhaelt."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.kanaele, self.gesendet, self.wuensche = {}, [], []

    def wunsch(self, wert, herkunft=None, durch=None, kreis=1):
        self.wuensche.append((kreis, wert))
        return wert

    def laufzeit_kanal(self, worker, kreis):
        kanal = RuntimeChannel(lambda name, payload: self.gesendet.append((kreis, name, payload)) or len(self.gesendet),
                               lambda: True, lambda: 0)
        self.kanaele[kreis] = kanal
        return kanal.binden(worker)


def _zwei_kreise_dokument():
    dokument = konfiguration_aus_optionen(OPTIONS)
    zwei = copy.deepcopy(dokument["kreise"][0])
    zwei["kreis"] = 2
    zwei["name"] = "Kreis 2"
    zwei["bindung"]["kreis"]["nummer"] = "1"
    zwei["bindung"]["rollen"] = {rolle: f"{ref}_2" for rolle, ref in zwei["bindung"]["rollen"].items()}
    zwei["raeume"][0]["fuehler"] = ["sensor.room_actual_2"]
    dokument["kreise"].append(zwei)
    return dokument


def _entities_zweiter_kreis(ha):
    for entity, wert in list(ha.states.items()):
        if "::" not in entity and entity.split(".")[1] in {"room_actual", "room_target", "curve_current", "shift_current",
                                                           "min_flow", "heat_limit", "outdoor_temp"}:
            ha.states[f"{entity}_2"] = wert
    for key in [k for k in ha.states if k.endswith("::device_class")]:
        ha.states[key.replace("::", "_2::")] = ha.states[key]
    ha.registry += [schreibziel_eintrag(f"number.{name}_2", "mypyllant")
                    for name in ("curve_current", "shift_current", "heat_limit", "min_flow")]


@pytest.fixture
def zwei(geraet_env, monkeypatch):
    """Baut die Anlage mit zwei Kreisen auf dem Dokument aus _zwei_kreise_dokument; `bauen(abo_1=..., abgemeldet=...)`."""
    ha = geraet_env.ha
    _entities_zweiter_kreis(ha)
    aufrufe = []

    def ensure_all(**kwargs):
        aufrufe.append(kwargs)
        suffix = "" if kwargs["kreis"] == 1 else "_2"
        return DerivedSensors({"room_actual": f"sensor.room_actual{suffix}"}, (), "fp1",
                              raeume={n: f"sensor.room_actual{suffix}" for n, _ in kwargs["raeume"]})

    monkeypatch.setattr("heizungsbruecke.derived_sensors.ensure_all", ensure_all)

    def bauen(*, abo_1="aktiv", abgemeldet=False, leer=False, soll=(21.0, 22.0)):
        dokument = _zwei_kreise_dokument()
        dokument["kreise"][0]["abo"] = abo_1
        if abo_1 == "inaktiv":  # die Frist ist abgelaufen: Kreis 1 startet als abo_beendet
            entitlement.mark_inactive(config.entitlement_path(1), datetime.now().astimezone())
            monkeypatch.setattr("smartheat_runtime.entitlement.grace_expired", lambda since, now: True)
        if abgemeldet:
            dokument_config.abmelden(config.ABGEMELDET_PATH, dokument["anlage"])
        start_markierung = None
        if leer:  # die Konfiguration ist leer, das Zuruecksetzen steht aus (wie prozess.aufraeumen_beim_start)
            aufraeumen.markieren(config.AUFRAEUMEN_PATH, dokument)
            start_markierung = aufraeumen.entscheiden(config.AUFRAEUMEN_PATH, {"anlage": None})[1]
        device = _Geraet({**dokument, "anlage": None} if leer else dokument, bedienung={"kreise": [
            {"kreis": 1, "modus": "manuell", "raum_soll": soll[0]},
            {"kreis": 2, "modus": "manuell", "raum_soll": soll[1]}]})
        supervisor = FakeSupervisor()
        geraet = main_module.HaDeviceHost(ha, supervisor)
        geraet.start_markierung = start_markierung
        status = main_module.verdrahten(geraet, device, ha, supervisor)
        laufzeit, anlage = main_module.start(geraet, device, ha, status, geraet_env.clock)
        neustart = False
        try:
            laufzeit.worker.run_pending()
        except Neustart:  # die Laufzeit verlangt den Prozessneustart (nach dem Zuruecksetzen aller Kreise)
            neustart = True
        return SimpleNamespace(env=geraet_env, ha=ha, device=device, geraet=geraet, laufzeit=laufzeit, anlage=anlage,
                               ensure=aufrufe, dokument=dokument, neustart=neustart)

    return bauen


def _verbinden(welt):
    for client in welt.env.trigger_clients:
        client.kwargs["on_connected"]()
    welt.laufzeit.worker.run_pending()


def _client(welt, kreis):
    """Der Trigger-Client des Kreises (erkannt an seiner Soll-Entity)."""
    entity = "sensor.room_target" if kreis == 1 else "sensor.room_target_2"
    (client,) = [c for c in welt.env.trigger_clients
                 if any(t.get("entity_id") == entity for t in c.kwargs["triggers"])]
    return client


def _wunsch_an_alle(welt, daten):
    """HA liefert das Ereignis smartheat_wunsch an jeden Client (jeder Kreis hat einen)."""
    for client in welt.env.trigger_clients:
        client.kwargs["on_trigger_event"]({"platform": "event", "event": {"data": daten}})
    welt.laufzeit.worker.run_pending()


def _antwort(welt, kreis, **hebel):
    snapshot = [p for k, n, p in welt.device.gesendet if k == kreis and n == "up/snapshot"][-1]
    welt.device.kanaele[kreis].setpoints({
        "schema": 5, "kreis": kreis, "seq": snapshot["seq"], "ts": "x", "status": "ok", "reason": None,
        "levers": {"curve": hebel["curve"], "room_setpoint": hebel["shift"], "heat_limit": 16.0, "min_flow": 20.5},
        "learned": {"curve": 1.0, "heat_limit": 16.0}})
    welt.laufzeit.worker.run_pending()


def test_jeder_kreis_hat_seine_laufzeit_seinen_speicher_und_seine_ordner(zwei):
    welt = zwei(soll=(21.0, 22.5))
    eins, zwei_ = welt.laufzeit.kreise()[1], welt.laufzeit.kreise()[2]
    assert isinstance(eins, Runtime) and isinstance(zwei_, Runtime)
    assert (eins.config.kreis, zwei_.config.kreis) == (1, 2)
    assert (welt.anlage.hosts[1].store.value, welt.anlage.hosts[2].store.value) == (21.0, 22.5)
    assert load_backup(config.room_target_path(1))["value"] == 21.0
    assert load_backup(config.room_target_path(2))["value"] == 22.5
    assert config.room_target_path(1).parent != config.room_target_path(2).parent
    assert welt.anlage.hosts[1].soll_entity == "sensor.room_target"
    assert welt.anlage.hosts[2].soll_entity == "sensor.room_target_2"
    assert welt.anlage.kreise() == [1, 2]


def test_jeder_kreis_schreibt_nur_an_seine_eigenen_ziele(zwei):
    welt = zwei()
    _verbinden(welt)
    _antwort(welt, 1, curve=0.95, shift=23.0)
    _antwort(welt, 2, curve=1.0, shift=24.0)
    ziele_eins = {"number.curve_current", "number.shift_current", "number.heat_limit", "number.min_flow"}
    ziele_zwei = {f"{ziel}_2" for ziel in ziele_eins}
    geschrieben = dict(welt.ha.writes)
    assert geschrieben["number.curve_current"] == 0.95 and geschrieben["number.shift_current"] == 23.0
    assert geschrieben["number.curve_current_2"] == 1.0 and geschrieben["number.shift_current_2"] == 24.0
    assert {entity for entity, _ in welt.ha.writes} <= ziele_eins | ziele_zwei
    assert {w for e, w in welt.ha.writes if e in ziele_eins and e.startswith("number.curve")} == {0.95}
    assert {w for e, w in welt.ha.writes if e in ziele_zwei and e.startswith("number.curve")} == {1.0}


def test_endet_das_abo_eines_kreises_regelt_der_andere_weiter(zwei):
    welt = zwei(abo_1="inaktiv")
    assert isinstance(welt.laufzeit.kreise()[1], app.IdleBridge)
    assert isinstance(welt.laufzeit.kreise()[2], Runtime)
    _verbinden(welt)
    _antwort(welt, 2, curve=1.0, shift=24.0)
    assert ("number.curve_current_2", 1.0) in welt.ha.writes
    assert all(not entity.startswith("number.") or entity.endswith("_2") for entity, _ in welt.ha.writes)


def test_ein_wunsch_an_kreis_2_aendert_kreis_1_nie(zwei):
    welt = zwei()
    _wunsch_an_alle(welt, {"raum_soll": 23.0, "kreis": 2})
    assert welt.device.wuensche == [(2, 23.0)]
    assert welt.anlage.hosts[2].store.value == 23.0 and welt.anlage.hosts[1].store.value == 21.0
    assert load_backup(config.room_target_path(1))["value"] == 21.0


def test_ein_wunsch_ohne_kreis_gilt_dem_ersten(zwei):
    welt = zwei()
    _wunsch_an_alle(welt, {"raum_soll": 20.5})
    assert welt.device.wuensche == [(1, 20.5)]
    assert welt.anlage.hosts[1].store.value == 20.5 and welt.anlage.hosts[2].store.value == 22.0


@pytest.mark.parametrize("kreis", [0, -1, "2", True, 1.5, None])
def test_ein_wunsch_mit_ungueltigem_kreis_wird_verworfen_und_gemeldet(zwei, caplog, kreis):
    welt = zwei()
    with caplog.at_level(logging.WARNING):
        _wunsch_an_alle(welt, {"raum_soll": 19.0, "kreis": kreis})
    assert welt.device.wuensche == []
    assert (welt.anlage.hosts[1].store.value, welt.anlage.hosts[2].store.value) == (21.0, 22.0)
    assert "ungueltigem Kreis" in caplog.text


def test_ein_wunsch_an_einen_unbekannten_kreis_erreicht_keinen_kreis(zwei):
    welt = zwei()
    _wunsch_an_alle(welt, {"raum_soll": 19.0, "kreis": 3})
    assert welt.device.wuensche == []
    assert (welt.anlage.hosts[1].store.value, welt.anlage.hosts[2].store.value) == (21.0, 22.0)


def test_die_bedienung_an_der_soll_entity_gilt_dem_kreis_der_entity(zwei):
    welt = zwei()
    welt.ha.states["sensor.room_target_2"] = 23.5
    _client(welt, 2).kwargs["on_trigger_event"]({"platform": "state", "entity_id": "sensor.room_target_2",
                                                 "attribute": None, "to_state": {"state": 23.5}})
    welt.laufzeit.worker.run_pending()
    assert welt.device.wuensche == [(2, 23.5)]
    assert welt.anlage.hosts[1].store.value == 21.0


def test_die_hilfssensoren_eines_kreises_nennen_nur_seine_fuehler(zwei):
    welt = zwei()
    je_kreis = {aufruf["kreis"]: aufruf for aufruf in welt.ensure}
    assert je_kreis[1]["room_sensors"] == ["sensor.room_actual"]
    assert je_kreis[2]["room_sensors"] == ["sensor.room_actual_2"]
    assert je_kreis[1]["raeume"] == ((1, ("sensor.room_actual",)),)
    assert je_kreis[2]["raeume"] == ((1, ("sensor.room_actual_2",)),)


def test_ein_kreis_liest_nie_die_dateien_eines_anderen(zwei, monkeypatch):
    """Die Pfade der Kreise liegen in getrennten Ordnern; Kreis 2 startet mit dem Wert seiner Datei, nicht mit dem von
    Kreis 1."""
    save_backup(config.room_target_path(2), {"value": 24.0, "source": "lokal"})
    welt = zwei(soll=(21.0, 24.0))
    assert welt.anlage.hosts[2].store.value == 24.0 and welt.anlage.hosts[1].store.value == 21.0
    ordner = {1: config.backup_path(1).parent, 2: config.backup_path(2).parent}
    for kreis, pfad in ordner.items():
        for beschreiber in (config.failsafe_path, config.entitlement_path, config.room_target_path):
            assert beschreiber(kreis).parent == pfad
    assert ordner[1] != ordner[2]
    assert config.anlage_budget_path().parent == config.RUNTIME_DIR  # die Zaehler der Anlage liegen daneben


def test_ein_wieder_aktives_abo_nimmt_den_kreis_ohne_prozessneustart_auf(zwei):
    welt = zwei(abo_1="inaktiv")
    assert isinstance(welt.laufzeit.kreise()[1], app.IdleBridge)
    zwei_vorher = welt.laufzeit.kreise()[2]
    alt = copy.deepcopy(welt.dokument)
    neu = copy.deepcopy(welt.dokument)
    neu["kreise"][0]["abo"] = "aktiv"
    welt.device.konf = neu
    welt.geraet.konfiguration_uebernommen(alt, neu)
    ende = threading.Event()
    for _ in range(500):
        if isinstance(welt.laufzeit.kreise()[1], Runtime):
            break
        ende.wait(0.01)
    assert isinstance(welt.laufzeit.kreise()[1], Runtime)
    assert not welt.geraet._neustart  # kein Prozessneustart
    assert welt.laufzeit.kreise()[2] is zwei_vorher  # der andere Kreis regelte durch
    assert welt.laufzeit.kreise()[1].worker is welt.geraet._workers[1]  # der Geraetekern folgt dem neuen Worker


def test_ein_geaenderter_kreissatz_verlangt_den_prozessneustart_sofort(zwei):
    welt = zwei()
    alt = copy.deepcopy(welt.dokument)
    neu = copy.deepcopy(welt.dokument)
    neu["kreise"] = neu["kreise"][:1]
    welt.device.konf = neu
    welt.geraet.konfiguration_uebernommen(alt, neu)
    assert welt.geraet._neustart


def test_die_anlage_meldet_den_raum_je_kreis(zwei):
    welt = zwei()
    raeume = {k: welt.anlage.hosts[k].raum for k in (1, 2)}
    assert raeume[1] is not raeume[2] and None not in raeume.values()


def _boost_zustand(kreise):
    """Beide Kreise stehen auf Boost-Werten und haben einen Wiederherstellungspunkt."""
    for kreis in kreise:
        save_backup(config.backup_path(kreis), {
            "boost_active": True, "last_room_target": 21.0,
            "restore_point": {"curve": 0.5, "room_setpoint": 20.0, "heat_limit": 16.0, "min_flow": 20.5}})


def test_beim_abmelden_mit_einem_scheiternden_kreis_gilt_die_anlage_nicht_als_zurueckgesetzt(zwei, geraet_env,
                                                                                            monkeypatch):
    """Kreis 2 kann seine Ursprungswerte nicht zurueckschreiben: Kreis 1 ist zurueckgesetzt, die Markierung bleibt
    offen, die Anlage meldet nicht zurueckgesetzt und der Neustart bleibt aus."""
    _boost_zustand((1, 2))
    ha = geraet_env.ha
    echt = ha.set_number_value

    def schreibt(entity_id, value):
        if entity_id.endswith("_2"):
            raise OSError("weg")
        return echt(entity_id, value)

    monkeypatch.setattr(ha, "set_number_value", schreibt)
    welt = zwei(leer=True)
    assert ("number.curve_current", 0.5) in ha.writes  # Kreis 1 ist zurueckgesetzt
    assert load_backup(config.backup_path(1))["boost_active"] is False
    assert load_backup(config.backup_path(2))["boost_active"] is True  # Kreis 2 steht noch auf Boost-Werten
    markierung = aufraeumen.lesen(config.AUFRAEUMEN_PATH)
    assert markierung is not None and not markierung.zurueckgesetzt
    assert not welt.neustart
    status = welt.geraet.runtime_status_event
    assert status["status"] == "abgemeldet" and status["grund"] is not None
    warten = abmeldung_abwarten(welt.dokument["anlage"], lambda: welt.geraet.runtime_status_event,
                                config.backup_path(1), 0.0)
    fertig = warten.check()
    assert fertig is not None and fertig.result["zurueckgesetzt"] is False


def test_beim_abmelden_mit_gelingenden_kreisen_ist_die_markierung_erledigt(zwei):
    _boost_zustand((1, 2))
    welt = zwei(leer=True)
    assert ("number.curve_current", 0.5) in welt.ha.writes and ("number.curve_current_2", 0.5) in welt.ha.writes
    markierung = aufraeumen.lesen(config.AUFRAEUMEN_PATH)
    assert markierung is not None and markierung.zurueckgesetzt
    assert welt.neustart
    assert welt.laufzeit.kreise()[1].status.event()["grund"] is None
