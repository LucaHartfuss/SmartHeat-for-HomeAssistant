"""Der Gateway-Host mit zwei Heizkreisen: je Kreis ein Treiber, ein Zigbee-Fuehler und ein Thermostat, gemeinsam
Geraet, Zigbee-Spiegel, lokaler Bus, Cloud-Zaehler und Abmelde-Markierung."""
import copy
import json
import time
from datetime import datetime, timedelta

import pytest
from configs import THERMOSTAT, dokumente_schreiben, konfiguration
from conftest import VICARE_PARAMETER
from world import GatewayWorld

from smartheat_core import rastung
from smartheat_device import prozess
from smartheat_device.geraet_host_basis import abmeldung_abwarten
from smartheat_gateway.drivers.simulation import SimulationDriver
from smartheat_gateway.drivers.vicare_cloud.driver import QUOTA, ViCareCloudDriver
from smartheat_gateway.host import GatewayHost
from smartheat_gateway.paths import Paths
from smartheat_gateway.quota import QuotaExhausted
from smartheat_runtime import aufraeumen, entitlement
from smartheat_runtime.app import IdleBridge
from smartheat_runtime.backup_store import load_backup, save_backup
from smartheat_runtime.runtime import Runtime

SENSOR2 = "0x00124b0000000003"
THERMOSTAT2 = "0x00124b0000000004"


@pytest.fixture
def world(data_dir, monkeypatch, clock):
    world = GatewayWorld(data_dir, monkeypatch, clock)
    world.z2m.add_sensor(SENSOR2)
    world.z2m.add_thermostat(THERMOSTAT2)
    yield world
    world.close()


def zwei_kreise(abo_1: str = "aktiv", **changes) -> dict:
    """Das Konfigurationsdokument mit zwei Kreisen: der zweite hat eigenen Fuehler und eigenes Thermostat."""
    dokument = konfiguration(**changes)
    zwei = copy.deepcopy(dokument["kreise"][0])
    zwei["kreis"] = 2
    zwei["name"] = "Kreis 2"
    zwei["bindung"]["treiber"]["parameter"]["heizkreis"] = 1
    zwei["bindung"]["thermostat"] = THERMOSTAT2
    zwei["raeume"][0]["fuehler"] = [f"zigbee:{SENSOR2}:temperature"]
    dokument["kreise"].append(zwei)
    dokument["kreise"][0]["abo"] = abo_1
    return dokument


def zwei_bedienung(soll_1: float = 22.0, soll_2: float = 24.0) -> dict:
    return {"kreise": [{"kreis": 1, "modus": "manuell", "raum_soll": soll_1},
                       {"kreis": 2, "modus": "manuell", "raum_soll": soll_2}],
            "quelle": "portal", "ts": "2026-01-15T07:00:00+00:00"}


def _einrichten(world, dokument) -> None:
    """Das Dokument liegt wie nach einer Uebernahme vor, samt Rastung (der Dokumentspeicher schreibt sie dabei)."""
    dokumente_schreiben(world.paths, dokument)
    rastung.schreiben(world.paths.rastung, rastung.aus_konfiguration(dokument), "2026-01-01T00:00:00+00:00")


def _starten(world, dokument=None, soll=(22.0, 24.0)):
    _einrichten(world, dokument or zwei_kreise())
    world.start_runtime()
    world.z2m.report(SENSOR2, temperature=19.0, battery=80)
    world.connect()
    world.bedienung_senden(zwei_bedienung(*soll), 1)
    return world


def _gesetzt(world, ieee) -> list[float]:
    return [body["occupied_heating_setpoint"] for topic, body in world.bus.decoded()
            if topic == f"zigbee2mqtt/{ieee}/set" and "occupied_heating_setpoint" in body]


def test_zwei_kreise_laufen_je_mit_eigenem_host_treiber_und_thermostat(world):
    _starten(world)
    assert sorted(world.laufzeit.kreise()) == [1, 2] and sorted(world.anlage.hosts) == [1, 2]
    assert all(isinstance(ergebnis, Runtime) for ergebnis in world.laufzeit.kreise().values())
    erster, zweiter = world.anlage.hosts[1], world.anlage.hosts[2]
    assert erster.driver is not zweiter.driver and erster.driver is not None and zweiter.driver is not None
    assert _gesetzt(world, THERMOSTAT) and set(_gesetzt(world, THERMOSTAT)) == {22.0}
    assert _gesetzt(world, THERMOSTAT2) and set(_gesetzt(world, THERMOSTAT2)) == {24.0}
    assert world.status()["status"] == "regelt"


def _antwort(world, kreis: int, levers: dict) -> None:
    snapshot = [body for body in world.link.of("up/snapshot") if body.get("kreis") == kreis][-1]
    world.server("down/setpoints", {"schema": 5, "kreis": kreis, "seq": snapshot["seq"],
                                    "ts": "2026-01-15T00:00:00+01:00", "status": "ok", "reason": None,
                                    "levers": levers, "learned": {}})


LEVERS = {"curve": 1.4, "level": 3.0, "room_setpoint": 20.0}


def test_jeder_kreis_schreibt_nur_an_seine_eigenen_ziele(world):
    _starten(world)
    erster, zweiter = world.anlage.hosts[1].driver, world.anlage.hosts[2].driver
    vorher = _gesetzt(world, THERMOSTAT).copy()
    world.advance(3)
    schreibe_eins = erster.physical_writes
    _antwort(world, 2, LEVERS)
    assert zweiter.physical_writes > 0 and erster.physical_writes == schreibe_eins
    _antwort(world, 1, {**LEVERS, "curve": 1.1})
    assert erster.physical_writes > schreibe_eins
    assert _gesetzt(world, THERMOSTAT) == vorher  # der Hebel-Antwort schreibt nie ein anderes Thermostat
    assert {body["kreis"] for body in world.link.of("up/snapshot")} == {1, 2}


def _abo_beendet(world, monkeypatch, kreis: int) -> None:
    """Die Frist des Kreises ist abgelaufen: er startet als abo_beendet."""
    entitlement.mark_inactive(world.paths.entitlement(kreis), datetime.now().astimezone())
    monkeypatch.setattr("smartheat_runtime.entitlement.grace_expired", lambda since, now: True)


def test_endet_das_abo_eines_kreises_regelt_der_andere_weiter(world, monkeypatch):
    _abo_beendet(world, monkeypatch, 1)
    _starten(world, zwei_kreise(abo_1="inaktiv"))
    assert isinstance(world.kreis(1), IdleBridge) and isinstance(world.kreis(2), Runtime)
    assert not _gesetzt(world, THERMOSTAT) and set(_gesetzt(world, THERMOSTAT2)) == {24.0}
    world.advance(3)
    _antwort(world, 2, LEVERS)
    assert world.anlage.hosts[2].driver.physical_writes > 0


def _wunsch_am_thermostat(world, ieee: str, werte=(18.0, 16.0, 14.0)) -> None:
    from smartheat_runtime.soll_speicher import ECHO_WINDOW_SECONDS
    world.advance(ECHO_WINDOW_SECONDS)
    for wert in werte:
        world.z2m.report(ieee, occupied_heating_setpoint=wert, local_temperature=20.0)
        world.advance(3)
    world.advance(10)


def test_ein_wunsch_am_thermostat_von_kreis_2_aendert_kreis_1_nie(world):
    _starten(world)
    vorher = json.loads(world.paths.room_target(1).read_text())
    _wunsch_am_thermostat(world, THERMOSTAT2)
    wuensche = world.link.of("up/wish")
    assert len(wuensche) == 1 and wuensche[0]["kreis"] == 2 and wuensche[0]["werte"] == {"raum_soll": 15.0}
    assert json.loads(world.paths.room_target(1).read_text()) == vorher
    assert set(_gesetzt(world, THERMOSTAT)) == {22.0}
    assert _gesetzt(world, THERMOSTAT2)[-1] == 15.0


def _ergebnis(world, **match) -> dict:
    treffer = [body for link in world.links for body in link.of("up/result")
               if all(body.get(key) == value for key, value in match.items())]
    assert treffer, match
    return treffer[-1]


def test_ein_wieder_aktives_abo_nimmt_den_kreis_ohne_prozessneustart_auf(world, monkeypatch):
    _abo_beendet(world, monkeypatch, 1)
    _starten(world, zwei_kreise(abo_1="inaktiv"))
    assert isinstance(world.kreis(1), IdleBridge)
    zwei_vorher, starts, worker_vorher = world.kreis(2), world.restarts, world.geraet._workers[1]
    world.konfiguration_senden(zwei_kreise(abo_1="aktiv"), 2)
    assert _ergebnis(world, dokument="konfiguration", version=2)["ok"] is True
    for _ in range(500):
        world.run()
        if isinstance(world.kreis(1), Runtime):
            break
        time.sleep(0.01)
    assert isinstance(world.kreis(1), Runtime) and world.restarts == starts  # kein Prozessneustart
    assert world.kreis(2) is zwei_vorher  # der andere Kreis regelte durch
    assert world.geraet._workers[1] is not worker_vorher and world.kreis(1).worker is world.geraet._workers[1]
    world.bedienung_senden(zwei_bedienung(23.0, 24.0), 2)
    assert _gesetzt(world, THERMOSTAT)[-1] == 23.0  # der aufgenommene Kreis regelt wieder


def test_ein_dokument_ohne_einen_der_kreise_wird_abgelehnt(world):
    _starten(world)
    starts = world.restarts
    dokument = zwei_kreise()
    dokument["kreise"] = dokument["kreise"][:1]
    world.konfiguration_senden(dokument, 2)
    ergebnis = _ergebnis(world, dokument="konfiguration", version=2)
    assert ergebnis["ok"] is False and ergebnis["error"]["grund"] == "ungueltig"
    assert world.restarts == starts and sorted(world.laufzeit.kreise()) == [1, 2]


def test_ein_kreis_liest_nie_die_dateien_eines_anderen(world):
    pfade = {kreis: world.paths.kreis_dir(kreis) for kreis in (1, 2)}
    assert pfade[1] != pfade[2] and not pfade[1].is_relative_to(pfade[2]) and not pfade[2].is_relative_to(pfade[1])
    for beschreiber in (world.paths.backup, world.paths.failsafe, world.paths.entitlement, world.paths.room_target):
        for kreis in (1, 2):
            assert beschreiber(kreis).parent == pfade[kreis]
    save_backup(world.paths.room_target(2), {"value": 25.0, "source": "lokal"})
    _starten(world, soll=(22.0, 25.0))
    assert json.loads(world.paths.room_target(1).read_text())["value"] == 22.0
    assert json.loads(world.paths.room_target(2).read_text())["value"] == 25.0
    assert world.paths.anlage_budget.parent == world.paths.runtime_dir  # die Zaehler der Anlage liegen daneben


def _boost_zustand(world, kreise=(1, 2)) -> None:
    """Die Kreise stehen auf Boost-Werten und haben einen Wiederherstellungspunkt."""
    for kreis in kreise:
        save_backup(world.paths.backup(kreis), {
            "boost_active": True, "last_room_target": 21.0,
            "restore_point": {"curve": 0.5, "level": 2.0, "room_setpoint": 20.0}})


def _zuruecksetzen_starten(world):
    """Die Konfiguration ist leer, das Zuruecksetzen beider Kreise steht aus (Markierung aus dem Entfernen)."""
    _boost_zustand(world)
    dokumente_schreiben(world.paths, {**zwei_kreise(), "anlage": None})
    aufraeumen.markieren(world.paths.aufraeumen, zwei_kreise())
    return world.start_runtime()


def _reset_antwort(world):
    """Die Antwort des Geraete-Hosts auf eine offene Werksreset-Anfrage (`ursprungswerte`), None: keine Antwort."""
    world.geraet._reset_offen = (7, "2027-01-15T08:00:00+00:00", world.status()["tenant_id"])
    world.geraet._reset_weiter()
    if not world.paths.werksreset_bereit.exists():
        return None
    return json.loads(world.paths.werksreset_bereit.read_text())["ursprungswerte"]


def test_beim_abmelden_mit_gelingenden_kreisen_ist_die_markierung_erledigt(world):
    _zuruecksetzen_starten(world)
    assert [world.anlage.hosts[k].driver.physical_writes > 0 for k in (1, 2)] == [True, True]
    assert all(load_backup(world.paths.backup(k))["boost_active"] is False for k in (1, 2))
    assert world.status()["status"] == "abgemeldet" and world.status()["grund"] is None
    assert _reset_antwort(world) is True  # alle Kreise zurueckgesetzt: Ursprungswerte true
    world.run()  # die Laufzeit verlangt den Neustart erst, wenn alle Kreise fertig sind
    assert world.restarts == 2 and not world.paths.aufraeumen.exists()


def test_beim_abmelden_mit_einem_scheiternden_kreis_gilt_die_anlage_nicht_als_zurueckgesetzt(world, monkeypatch):
    """Kreis 2 kann seine Ursprungswerte nicht zurueckschreiben: Kreis 1 ist zurueckgesetzt, die Markierung bleibt
    offen, die Anlage meldet nicht zurueckgesetzt und der Neustart bleibt aus."""
    echt = SimulationDriver.write

    def schreiben(self, hebel, wert):
        if self._p["heizkreis"] == 1:
            raise RuntimeError("weg")
        return echt(self, hebel, wert)

    monkeypatch.setattr(SimulationDriver, "write", schreiben)
    _zuruecksetzen_starten(world)
    assert load_backup(world.paths.backup(1))["boost_active"] is False  # Kreis 1 ist zurueckgesetzt
    assert load_backup(world.paths.backup(2))["boost_active"] is True  # Kreis 2 steht noch auf Boost-Werten
    markierung = aufraeumen.lesen(world.paths.aufraeumen)
    assert markierung is not None and not markierung.zurueckgesetzt
    world.run()
    assert world.restarts == 1  # kein Neustart, solange ein Kreis aussteht
    status = world.status()
    assert status["status"] == "abgemeldet" and status["grund"] is not None
    warten = abmeldung_abwarten("test-tenant", lambda: world.geraet.zustand.status, world.paths.backup(1), 0.0)
    fertig = warten.check()
    assert fertig is not None and fertig.result["zurueckgesetzt"] is False
    assert _reset_antwort(world) is None  # ein Kreis steht aus: keine Antwort mit Ursprungswerten true


def test_die_cloud_quote_zaehlt_alle_kreise_auf_einer_datei(tmp_path, clock):
    """Zwei Cloud-Treiber (Kreis 0 und 1) derselben Anlage teilen das Kontingent: Grenze minus eins auf beiden Seiten
    noch frei, die Grenze selbst fuer beide erschoepft."""
    paths = Paths(tmp_path)
    erster = ViCareCloudDriver({**VICARE_PARAMETER, "heizkreis": 0}, paths, clock=clock)
    zweiter = ViCareCloudDriver({**VICARE_PARAMETER, "heizkreis": 1}, paths, clock=clock)
    assert erster._guard._path == zweiter._guard._path == paths.quota_dir / "vicare_cloud.json"
    for nummer in range(QUOTA.hard_limit - 1):
        (erster if nummer % 2 else zweiter)._guard.take()
    assert not erster._guard.exhausted() and not zweiter._guard.exhausted()
    erster._guard.take()  # der letzte freie Aufruf eines Kreises ...
    assert erster._guard.exhausted() and zweiter._guard.exhausted()  # ... erschoepft beide
    with pytest.raises(QuotaExhausted):
        zweiter._guard.take()


def test_die_raumdaten_gehoeren_je_zum_eigenen_kreis(world):
    _starten(world)
    world.geraet.raum_merken(1, {"name": "Wohnen"})
    world.geraet.raum_merken(2, {"name": "Schlafen"})
    assert world.geraet.raum(1) == {"name": "Wohnen"} and world.geraet.raum(2) == {"name": "Schlafen"}


def _abfrager_mitzaehlen(monkeypatch) -> set:
    """Die Treiber starten ihren Abfrage-Thread (gezaehlt, ohne echten Thread); das Ergebnis haelt die laufenden."""
    laufend: set = set()
    echt = GatewayHost._activate

    def aktivieren(self, driver):
        self._driver_threads = True
        driver.start = lambda: laufend.add(id(driver))
        driver.stop = lambda: laufend.discard(id(driver))
        echt(self, driver)

    monkeypatch.setattr(GatewayHost, "_activate", aktivieren)
    return laufend


def test_nach_jeder_wiederaufnahme_fragt_hoechstens_ein_treiber_je_kreis_ab(world, monkeypatch):
    laufend = _abfrager_mitzaehlen(monkeypatch)
    _starten(world)
    assert len(laufend) == 2  # ein Abfrager je Kreis
    for _ in range(3):
        assert world.laufzeit.wiederaufnehmen(2, erzwingen=True) is True
        assert len(laufend) == 2
    assert id(world.anlage.hosts[2].driver) in laufend and id(world.anlage.hosts[1].driver) in laufend


def test_die_aufnahme_aus_dem_ruhezustand_ersetzt_den_abfrager_des_kreises(world, monkeypatch):
    laufend = _abfrager_mitzaehlen(monkeypatch)
    _abo_beendet(world, monkeypatch, 1)
    _starten(world, zwei_kreise(abo_1="inaktiv"))
    assert isinstance(world.kreis(1), IdleBridge)
    alter_treiber = world.anlage.hosts[1].driver

    world.konfiguration_senden(zwei_kreise(abo_1="aktiv"), 2)
    for _ in range(500):
        world.run()
        if isinstance(world.kreis(1), Runtime):
            break
        time.sleep(0.01)

    assert isinstance(world.kreis(1), Runtime) and len(laufend) == 2
    assert id(alter_treiber) not in laufend and id(world.anlage.hosts[1].driver) in laufend


def _abo_seit(world, kreis: int, tage: int = 31) -> None:
    """Das Abo des Kreises ist seit `tage` Tagen inaktiv (Wanduhr der Welt, die echte Fristpruefung)."""
    entitlement.mark_inactive(world.paths.entitlement(kreis), world.wall.now() - timedelta(days=tage))


def _prozess_neu_starten(world) -> None:
    """Ein Neustart des Prozesses wie Compose: erst sauber trennen, dann alles neu auf denselben Dateien."""
    prozess.beenden(world.device, None, None)
    world.start_runtime()
    world.connect()
    world.bedienung_senden(zwei_bedienung(), 2)


def test_nach_dem_ende_bleibt_der_kreis_auch_bei_unsicherer_uhr_im_ruhezustand(world):
    _abo_seit(world, 1)
    _starten(world, zwei_kreise(abo_1="inaktiv"))
    assert isinstance(world.kreis(1), IdleBridge) and world.kreis(1).reason == "abo_beendet"
    assert entitlement.ist_beendet(world.paths.entitlement(1))
    world.zeitdienst(False)  # der Neustart kommt vor dem Zeitabgleich (Pi ohne Echtzeituhr)
    vorher = _gesetzt(world, THERMOSTAT)
    _prozess_neu_starten(world)
    world.advance(120)
    assert isinstance(world.kreis(1), IdleBridge) and world.kreis(1).reason == "abo_beendet"
    assert _gesetzt(world, THERMOSTAT) == vorher  # kein Thermostat-Schreiben des beendeten Kreises
    assert world.anlage.hosts[1].driver is None or world.anlage.hosts[1].driver.physical_writes == 0
    assert isinstance(world.kreis(2), Runtime)  # der andere Kreis regelt weiter


def test_das_erste_ende_bei_unsicherer_uhr_bleibt_offen_und_ist_nicht_beendet(world):
    _abo_seit(world, 1)
    world.zeitdienst(False)
    _starten(world, zwei_kreise(abo_1="inaktiv"))
    assert isinstance(world.kreis(1), Runtime)  # die Frist wird nie auf einer unsicheren Uhr beendet
    assert not entitlement.ist_beendet(world.paths.entitlement(1))
    world.zeitdienst(True)
    world.advance(120)
    world.laufzeit.worker.run_pending()


def _wunsch_merken(world, kreis: int, wert: float) -> None:
    """Der gemerkte Wunsch des Kreises liegt im Soll-Speicher (wie nach einem Betrieb vor dem Ende)."""
    pfad = world.paths.room_target(kreis)
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_text(json.dumps({"value": wert, "source": "portal", "ts": "x"}))


def test_das_soll_ende_wird_nach_dem_neustart_bei_unsicherer_uhr_nachgeholt(world):
    _wunsch_merken(world, 1, 22.0)
    _abo_seit(world, 1)
    _starten(world, zwei_kreise(abo_1="inaktiv"))  # das Thermostat des Kreises ist noch nicht bekannt
    assert isinstance(world.kreis(1), IdleBridge) and world.kreis(1).reason == "abo_beendet"
    assert load_backup(world.paths.backup(1))["soll_ende_offen"] is True and _gesetzt(world, THERMOSTAT) == []
    world.zeitdienst(False)
    _prozess_neu_starten(world)
    world.z2m.report(THERMOSTAT, occupied_heating_setpoint=19.0, system_mode="heat", local_temperature=20.0)

    world.advance(3600)

    assert isinstance(world.kreis(1), IdleBridge) and world.kreis(1).reason == "abo_beendet"
    assert _gesetzt(world, THERMOSTAT) == [22.0]  # die Soll-Quelle zeigt wieder den gemerkten Wunsch
    assert load_backup(world.paths.backup(1)).get("soll_ende_offen") is not True


@pytest.mark.parametrize("zustand", [{"occupied_heating_setpoint": 22.0}, {"occupied_heating_setpoint": 23.5},
                                     {"occupied_heating_setpoint": 6.0},
                                     {"occupied_heating_setpoint": 19.0, "system_mode": "off"}],
                         ids=["gleich", "mehr", "frostschutz", "aus"])
def test_das_soll_ende_senkt_nie_und_schreibt_nicht_auf_ein_ausgeschaltetes_thermostat(world, zustand):
    _wunsch_merken(world, 1, 22.0)
    _abo_seit(world, 1)
    _starten(world, zwei_kreise(abo_1="inaktiv"))
    world.z2m.report(THERMOSTAT, local_temperature=20.0, **{"system_mode": "heat", **zustand})

    world.advance(3600)

    assert _gesetzt(world, THERMOSTAT) == []
