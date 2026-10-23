"""Testoffensive T4' Task 8 (Spec §5 "Kompromittierter Server"): Angriffsszenarien ueber die ganze Kette. Ein Server, der
sehr viele Befehle oder lauter kaputte Dokumente und Antworten sendet, waehrend die Laufzeit regelt, bringt weder den
Geraete-Thread noch den Regel-Worker zum Stehen und keinen Wert ausserhalb der lokalen Grenzen auf die Anlage (Regel 4).

Aufbau wie produktiv, nur ohne Netz und ohne Threads: der echte Geraetekern (`smartheat_device.device.Device` mit dem
Fake-Link aus device_helpers.py), der echte HA-Host (`HaDeviceHost`, `__main__.verdrahten` und `__main__.start`)
und die echte Laufzeit (`HaHost`, Regel-Worker, Pipeline) auf Fake-HA und Fake-Uhr. Ein Durchlauf ist eine Runde:
Nachrichten in die Inbox des Geraets, `Device.schritt()` (Geraete-Thread: Nachrichten und ein Takt), `run_pending()`
(Regel-Worker), dann die Uhr vor. Die Anlage startet wie nach einem Neustart des Add-ons mit gespeicherten Dokumenten
(sonst haelt `HaDeviceHost` den Neustart fuer die erste Einrichtung an). Die Grenzen stehen woertlich in
test_regel4_werte.py (TABELLE), nie aus dem Code. Findet ein Test hier einen Schreibwert ausserhalb der Grenze, ist das
ein Stoppfall (Regel 4), kein Fix im Produktcode.

Zahlen (gemessen, beide Tests zusammen etwa 8 s im schnellen Lauf, Grenze 20 s): 2000 Befehle in 40 Runden zu 50 und
100 kaputte Nachrichten in 100 Runden, wie der Auftrag sie nennt, ohne Kuerzung (die Zeit geht fast ganz in das atomare
Schreiben von befehle.json mit bis zu 500 Eintraegen je Befehl). Das atomare Schreiben laeuft ohne
fsync (Durchsatz; die Absturzsicherheit pruefen test_core_dateien.py und die Geraete-Tests)."""
import json
import logging
import os
import random
from collections import Counter

import pytest
from device_helpers import World
from geraet_helpers import FakeSupervisor, konfiguration_aus_optionen
from runtime_helpers import NICHT_NUMERISCH, OPTIONS, SAETZE, _set_room_target, _trigger
from test_regel4_werte import HK, TABELLE

import heizungsbruecke.__main__ as main_module
from smartheat_core import dateien
from smartheat_device import wire
from smartheat_device.documents import DocumentStore

HEBELSATZ = "vaillant_vrc720"
BEREICHE = TABELLE[(HEBELSATZ, HK)][0]  # lokale Grenzen je Hebel, Literal aus test_regel4_werte.py
ENTITIES = SAETZE[HEBELSATZ][2]  # Entity -> Hebel
HINWEIS_STUFE = "info"


@pytest.fixture(autouse=True)
def _ohne_fsync(monkeypatch):
    """Durchsatz: jeder Befehl schreibt befehle.json atomar; die Absturzsicherheit (fsync) pruefen andere Tests."""
    monkeypatch.setattr(os, "fsync", lambda fd: None)


# --- Aufbau ---


class Anlage:
    """Geraetekern, HA-Host und Laufzeit verdrahtet wie in __main__, auf Fake-HA und gemeinsamer Fake-Uhr."""

    def __init__(self, env, tmp_path) -> None:
        self.env = env
        supervisor = FakeSupervisor()
        for entity in ("number.curve_current", "number.shift_current", "number.min_flow", "number.heat_limit"):
            env.ha.registry.append({"entity_id": entity, "platform": "mypyllant", "config_entry_id": "e1"})
        # lokale Positivliste (A6-02): die Sensoren der Bindung tragen ihre Klasse im Zustand, wie im echten HA
        for entity in ("sensor.room_actual", "sensor.room_target", "sensor.outdoor_temp", "sensor.flow_temperature",
                       "sensor.flow_setpoint"):
            env.ha.states[f"{entity}::device_class"] = "temperature"
        env.ha.states["sensor.thermostat_battery::device_class"] = "battery"
        self.konfiguration = konfiguration_aus_optionen(OPTIONS)
        daten = tmp_path / "data"  # config.DEVICE_DATA_DIR der Fixture geraet_env
        # Einrichtung: der erste Prozess nimmt Konfiguration und Bedienung an (der Host fordert dafuer einen Neustart an)
        einrichtung = World(daten, main_module.HaDeviceHost(env.ha, supervisor, clock=env.clock))
        einrichtung.verbinden()
        einrichtung.empfangen("down/config", {"schema": 1, "version": 1, **self.konfiguration})
        einrichtung.empfangen("down/operation", {"schema": 1, "version": 1, "modus": "manuell", "raum_soll": 21.0,
                                                 "quelle": "portal", "ts": "2027-01-15T07:00:00+00:00"})
        assert einrichtung.device.documents.eingerichtet()
        # Betrieb: neuer Prozess auf denselben Dateien
        self.geraet = main_module.HaDeviceHost(env.ha, supervisor, clock=env.clock)
        self.world = World(daten, self.geraet)
        status = main_module.verdrahten(self.geraet, self.world.device, env.ha, supervisor)
        self.bridge, _ = main_module.start(self.geraet, self.world.device, env.ha, status, clock=env.clock)
        self.bridge.worker.run_pending()
        self.world.verbinden()
        env.trigger_clients[-1].kwargs["on_connected"]()
        self.runde()

    @property
    def device(self):
        return self.world.device

    @property
    def link(self):
        return self.world.link

    def senden(self, name: str, body) -> None:
        """Nachricht des Servers in die Inbox des Geraets (verarbeitet erst die naechste Runde)."""
        self.link.empfangen(name, body)

    def roh_senden(self, name: str, raw: bytes) -> None:
        self.link.ereignis("nachricht", name, raw)

    def runde(self, sekunden: float = 0) -> None:
        """Geraete-Thread, Regel-Worker, dann die Uhr vor (Geraet und Laufzeit teilen die Zeit)."""
        self.device.schritt()
        self.bridge.worker.run_pending()
        self.env.clock.advance(sekunden)
        self.world.clock.now += sekunden

    def snapshots(self) -> list[dict]:
        return self.link.of("up/snapshot")

    def antwort(self, **hebel) -> None:
        """Gueltige Serverantwort auf den letzten Tick."""
        werte = {"curve": 0.95, "room_setpoint": 23.0, "heat_limit": 16.0, **hebel}
        self.senden("down/setpoints", {"schema": 4, "seq": self.snapshots()[-1]["seq"], "ts": "x", "status": "ok",
                                       "reason": None, "levers": werte, "learned": None})

    def stand(self) -> dict:
        state = self.bridge.store.state
        return {"komfort_boost": state.boost_active, "notfall_boost": state.emergency_boost_active,
                "notbetrieb": state.delivery.notbetrieb}


def _fehler_im_log(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


def _alle_schreibwerte_in_den_grenzen(ha) -> int:
    """Jeder Schreibwert an die Anlage liegt in der lokalen Grenze seines Hebels; Rueckgabe: Zahl der Schreibwerte."""
    for entity, wert in ha.writes:
        if entity in NICHT_NUMERISCH:
            continue
        assert entity in ENTITIES, entity  # ein unbekanntes Schreibziel faellt auf
        low, high = BEREICHE[ENTITIES[entity]]
        assert low <= wert <= high, (entity, wert)
    return len(ha.writes)


# --- ck:376 Befehlsflut ---

FLUT_ANZAHL = 2000  # 1700 verschiedene gueltige IDs, 200 Duplikate, 100 ohne gueltige ID
RUNDEN = 40
JE_RUNDE = FLUT_ANZAHL // RUNDEN


def _befehl(command_id, kind, payload, **extra):
    return {"schema": 1, "command_id": command_id, "kind": kind, "payload": payload,
            "expires_at": "2027-01-15T09:00:00+00:00", **extra}


def _flut() -> tuple[list[dict], list[str]]:
    """(Nachrichten in Sendereihenfolge, gueltige IDs in der Reihenfolge ihres ersten Auftretens). Feste Zufallsfolge."""
    zufall = random.Random(20261018)
    einzeln: list[dict] = []
    for nr in range(700):  # gueltig: Hinweis mit wenigen Schluesseln (die Datei waechst nicht mit der Flut)
        einzeln.append(_befehl(f"f{len(einzeln):04d}", "hinweis",
                               {"key": f"flut{nr % 8}", "stufe": HINWEIS_STUFE, "text": f"Text {nr}"}))
    for _ in range(300):
        einzeln.append(_befehl(f"f{len(einzeln):04d}", "diagnostics", {}))
    for _ in range(200):
        einzeln.append(_befehl(f"f{len(einzeln):04d}", "reboot_everything", {}))  # unbekannte Art
    for nr in range(200):
        einzeln.append(_befehl(f"f{len(einzeln):04d}", "diagnostics", {}, fremd=nr))  # fremdes Feld
    for nr in range(150):  # Nutzlast: Liste, fremdes Feld, zu langer Text
        payload = [[], {"key": "k", "stufe": HINWEIS_STUFE, "text": "t", "fremd": 1},
                   {"key": "k", "stufe": HINWEIS_STUFE, "text": "x" * 1001}][nr % 3]
        einzeln.append(_befehl(f"f{len(einzeln):04d}", "hinweis", payload))
    for _ in range(150):  # eine Art, die HA nicht kann
        einzeln.append(_befehl(f"f{len(einzeln):04d}", "zigbee_permit_join", {"seconds": 30}))
    assert len(einzeln) == 1700
    ohne_id = []
    for nr in range(100):  # gueltig bis auf die ID: liefe er, bliebe der Hinweis "ohne-id" stehen
        ohne_id.append(_befehl([None, 123, "", "x" * 65, "a\ud800b", ["f0000"], {"id": 1}, True, 1.5, "\udfff"][nr % 10],
                               "hinweis", {"key": "ohne-id", "stufe": HINWEIS_STUFE, "text": "Angriff"}))
        if nr % 25 == 0:
            del ohne_id[-1]["command_id"]  # und ganz ohne
    reihe = [(nachricht, True) for nachricht in einzeln] + [(nachricht, False) for nachricht in ohne_id]
    zufall.shuffle(reihe)
    gueltige = [nachricht["command_id"] for nachricht, gueltig in reihe if gueltig]  # Reihenfolge des ersten Auftretens
    for _ in range(200):  # Duplikate: dieselbe ID, andere Nutzlast ("dup-attack"), kurz nach dem Original
        quelle = zufall.choice([i for i, (_, gueltig) in enumerate(reihe) if gueltig])
        ziel = min(len(reihe), quelle + 1 + zufall.randint(0, 80))
        reihe.insert(ziel, (_befehl(reihe[quelle][0]["command_id"], "hinweis",
                                    {"key": "dup-attack", "stufe": HINWEIS_STUFE, "text": "zweiter Lauf"}), False))
    assert len(reihe) == FLUT_ANZAHL
    return [nachricht for nachricht, _ in reihe], gueltige


def _gespeicherte_ergebnisse(anlage) -> list:
    return dateien.lesen_json(anlage.world.dir / "befehle.json")


def test_eine_befehlsflut_haelt_die_regelung_nicht_an_und_den_speicher_bei_500(
        geraet_env, tmp_path, caplog):
    """ck:376: 2000 Befehle ueber down/command (1700 verschiedene gueltige IDs, 200 Duplikate mit anderer Nutzlast, 100 ohne
    gueltige ID), waehrend die Laufzeit Ticks, Comfort-Boost und Notbetrieb mit Notfall-Boost durchlaeuft (Fake-Uhr):
    die Tick-Zaehler steigen, befehle.json haelt hoechstens die letzten 500 Ergebnisse, ein Duplikat laeuft nicht noch
    einmal, ein Befehl ohne gueltige ID nie, jeder Schreibwert liegt in der lokalen Grenze. sign_off ist nicht Teil der
    Flut: der Befehl beendet die Regelung dem Vertrag nach (test_runtime.py)."""
    env = geraet_env
    reihe, gueltige = _flut()
    assert len(gueltige) == 1700 and len(set(gueltige)) == 1700
    anlage = Anlage(env, tmp_path)
    assert _gespeicherte_ergebnisse(anlage) is None  # gleich null Ergebnisse vor der Flut
    assert anlage.stand() == {"komfort_boost": False, "notfall_boost": False, "notbetrieb": False}  # Ausgangslage
    ticks_vorher, schreibwerte_vorher = len(anlage.snapshots()), len(env.ha.writes)
    zaehler = {"komfort_boost": 0, "notfall_boost": 0, "notbetrieb": 0}
    ticks_je_runde = []
    mitte = None

    with caplog.at_level(logging.INFO):
        for runde in range(RUNDEN):
            for nachricht in reihe[runde * JE_RUNDE:(runde + 1) * JE_RUNDE]:
                anlage.senden("down/command", nachricht)
            if runde == 1:
                anlage.antwort()  # der Server antwortet auf den ersten Tick
            if runde == 3:
                _set_room_target(env, anlage.bridge, 22.0)  # Erhoehung des Wunsches: Comfort-Boost und neuer Tick
            if runde == 7:
                _trigger(env, anlage.bridge, "sensor.room_actual")  # Raum 20,0 bei Soll 22,0: Notfall-Boost im Notbetrieb
            if runde == 14:
                anlage.antwort()  # der Server ist wieder da: Notbetrieb und Notfall-Boost enden
            if runde == 16:
                env.ha.states["sensor.room_actual"] = 21.6
                _trigger(env, anlage.bridge, "sensor.room_actual")  # Soll erreicht: Boost endet
            if runde == 20:
                _set_room_target(env, anlage.bridge, 22.5)  # und noch einmal Comfort-Boost mit Tick
            if runde == 26:
                anlage.antwort()
            if runde == 30:
                env.ha.states["sensor.room_actual"] = 22.4
                _trigger(env, anlage.bridge, "sensor.room_actual")
            anlage.runde(30)
            for name, aktiv in anlage.stand().items():
                zaehler[name] += int(aktiv)
            ticks_je_runde.append(len(anlage.snapshots()))
            if runde == 5:  # 300 Nachrichten gesendet: noch unter dem Limit, alle gueltigen IDs gespeichert
                mitte = _gespeicherte_ergebnisse(anlage)

    # Regelung lief weiter: Boost und Notbetrieb wurden wirklich durchlaufen (sonst waere das Szenario leer)
    assert all(anzahl > 0 for anzahl in zaehler.values()), zaehler
    assert ticks_je_runde[-1] > ticks_vorher and len(set(ticks_je_runde)) >= 4, ticks_je_runde  # Tick-Zaehler steigt
    assert ("number.curve_current", 1.5) in env.ha.writes[schreibwerte_vorher:]  # der Boost schrieb den Hoechstwert
    assert _alle_schreibwerte_in_den_grenzen(env.ha) > schreibwerte_vorher
    assert anlage.stand() == {"komfort_boost": False, "notfall_boost": False, "notbetrieb": False}  # alles beendet

    # Speicher beschraenkt: hoechstens 500 Ergebnisse, genau die letzten 500 gueltigen IDs, aelteste zuerst
    erste = list(dict.fromkeys(m["command_id"] for m in reihe[:6 * JE_RUNDE] if isinstance(m.get("command_id"), str)
                                and m["command_id"] in set(gueltige)))  # gueltige IDs der ersten 300 Befehle
    assert mitte is not None and 100 < len(erste) < 500 and [eintrag[0] for eintrag in mitte] == erste
    gespeichert = _gespeicherte_ergebnisse(anlage)
    assert len(gespeichert) == 500
    assert [eintrag[0] for eintrag in gespeichert] == gueltige[-500:]
    assert all(eintrag[1] is not None for eintrag in gespeichert)  # fertige Ergebnisse, keine Marker "gestartet"

    # jede gueltige ID wurde beantwortet, ein Duplikat mit dem ersten Ergebnis (der Server behaelt das erste)
    alle_ergebnisse = anlage.link.of("up/result")  # alle, auch solche ohne "command_id": keines ohne gueltige String-ID
    assert all(isinstance(m.get("command_id"), str) and m["command_id"] in set(gueltige) for m in alle_ergebnisse)
    ergebnisse = alle_ergebnisse
    je_id: dict[str, list[dict]] = {}
    for ergebnis in ergebnisse:
        je_id.setdefault(ergebnis["command_id"], []).append(ergebnis)
    assert set(je_id) == set(gueltige) and len(ergebnisse) == 1900
    gesendet = Counter(m["command_id"] for m in reihe if isinstance(m.get("command_id"), str) and m["command_id"] in je_id)  # Original und Duplikate je ID
    assert sum(gesendet.values()) == 1900 and sum(anzahl - 1 for anzahl in gesendet.values()) == 200
    assert {kennung: len(liste) for kennung, liste in je_id.items()} == dict(gesendet)  # jedes Duplikat beantwortet
    assert all(len(set(map(json.dumps, liste))) == 1 for liste in je_id.values())
    assert {(m["ok"], (m["error"] or {}).get("grund")) for m in ergebnisse} == {
        (True, None), (False, "ungueltige_nutzlast"), (False, "nicht_unterstuetzt")}

    # nichts davon lief mit der falschen Nutzlast: weder das Duplikat noch der Befehl ohne gueltige ID
    schluessel = {eintrag["key"] for eintrag in anlage.device.hinweise.alle()}
    assert schluessel == {f"flut{nr}" for nr in range(8)}

    # Geraete-Thread und Regel-Worker liefen durch: kein Fehler mit Ausnahme, auch sonst keiner im Log
    fehler = _fehler_im_log(caplog)
    assert [r.getMessage() for r in fehler if r.exc_info] == []
    assert [r.getMessage() for r in fehler] == []


# --- ck:377 kaputte Dokumente und Serverantworten ---

KAPUTT_ANZAHL = 100
#: gueltig aufgebaute Antworten mit Werten weit ausserhalb der lokalen Grenzen: zu hoch, zu niedrig
WILDE_ANTWORTEN = ({"curve": 9.9, "room_setpoint": 99.0, "heat_limit": 99.0},
                   {"curve": -5.0, "room_setpoint": -40.0, "heat_limit": -40.0})
#: Namen der Varianten, die das Geraet verwirft, bevor es sie liest (zu gross, kein JSON, keine Nachricht): keine Antwort
UNGELESEN = {"Uebergroesse", "kein JSON", "Liste"}


def _grosser_text() -> str:
    return "x" * (wire.MAX_MESSAGE_BYTES + 10)


def _kaputte_konfigurationen(konfiguration: dict) -> list:
    """Je Eintrag (Name, Koerper oder Bytes): kaputte down/config; Version setzt der Aufrufer ein."""
    def mit(**aenderung):
        return lambda version: {"schema": 1, "version": version, **konfiguration, **aenderung}

    lockerer_schutz = json.loads(json.dumps(konfiguration["bindungsparameter"]))
    lockerer_schutz["schreibschutz"] = {**lockerer_schutz["schreibschutz"], "durchsetzen_tag": 7}
    return [
        ("fremdes Feld", mit(gift=1)),
        ("NaN im Raumfuehler", mit(raumfuehler=[float("nan")])),
        ("Uebergroesse", mit(anlage=_grosser_text())),
        ("Surrogat", mit(anlage="a\ud800b")),
        ("lockerer Schreibschutz", mit(bindungsparameter=lockerer_schutz)),
        ("anderer Hebelsatz", mit(hebelsatz="viessmann_vicare")),
        ("anderes Verteilsystem", mit(verteilsystem="Fussbodenheizung")),
        ("Tagestick keine Uhrzeit", mit(tagestick=5)),
        ("Riesenzahl", mit(tagestick=10**400)),
        ("Feld fehlt", lambda version: {k: v for k, v in {"schema": 1, "version": version, **konfiguration}.items()
                                         if k != "bindung"}),
        ("kein JSON", lambda version: b"{kaputt"),
        ("Liste", lambda version: b"[1, 2]"),
    ]


def _kaputte_bedienungen() -> list:
    def mit(**aenderung):
        return lambda version: {"schema": 1, "version": version, "modus": "manuell", "raum_soll": 21.0,
                                "quelle": "portal", "ts": "2027-01-15T07:00:00+00:00", **aenderung}

    return [
        ("NaN", mit(raum_soll=float("nan"))),
        ("Unendlich", mit(raum_soll=float("inf"))),
        ("ueber dem Bereich", mit(raum_soll=25.5)),
        ("unter dem Bereich", mit(raum_soll=14.5)),
        ("falscher Schritt", mit(raum_soll=21.3)),
        ("Text", mit(raum_soll="21")),
        ("fremdes Feld", mit(gift=1)),
        ("Surrogat", mit(quelle="a\udfffb")),
        ("Uebergroesse", mit(quelle=_grosser_text())),
        ("Riesenzahl", mit(raum_soll=10**400)),
        ("unbekannter Modus", mit(modus="gift")),
    ]


def _kaputte_antworten(seq: str) -> list:
    """Kaputte down/setpoints zum offenen Tick seq."""
    def mit(**aenderung):
        return {"schema": 4, "seq": seq, "ts": "x", "status": "ok", "reason": None,
                "levers": {"curve": 0.95, "room_setpoint": 23.0, "heat_limit": 16.0}, "learned": None, **aenderung}

    return [
        ("fremdes Feld", mit(gift=1)),
        ("NaN", mit(levers={"curve": float("nan"), "room_setpoint": 23.0, "heat_limit": 16.0})),
        ("Unendlich", mit(levers={"curve": 0.95, "room_setpoint": float("-inf"), "heat_limit": 16.0})),
        ("Hebel fehlt", mit(levers={"curve": 0.95})),
        ("fremder Hebel", mit(levers={"curve": 0.95, "room_setpoint": 23.0, "heat_limit": 16.0, "gift": 1.0})),
        ("falsches Schema", mit(schema=3)),
        ("Surrogat im Grund", mit(reason="a\ud800b")),
        ("Uebergroesse", mit(reason=_grosser_text())),
        ("Riesenzahl", mit(levers={"curve": 10**400, "room_setpoint": 23.0, "heat_limit": 16.0})),
        ("unbekannter Status", mit(status="gift")),
        ("gelernte Werte Text", mit(learned={"curve": "x", "heat_limit": float("nan")})),
    ]


def test_100_kaputte_dokumente_und_antworten_im_wechsel_mit_ticks_aendern_die_konfiguration_nicht(
        geraet_env, tmp_path, caplog):
    """ck:377: 100 kaputte Konfigurationen, Bedienungen und Serverantworten (fremde Felder, NaN, Unendlich, Uebergroesse,
    Surrogat, Riesenzahl, kein JSON) im Wechsel mit Ticks der laufenden Laufzeit: die gespeicherte Konfiguration und
    Bedienung bleiben die gueltigen (auch nach dem Neuladen von der Platte), die Laufzeit startet nicht neu, jeder
    Schreibwert liegt in der lokalen Grenze, kein ERROR mit Ausnahme im Log. Gegenprobe: zwischendurch gueltige
    Antworten (auch mit Werten ausserhalb der Grenze, geklemmt) werden geschrieben, danach nimmt das Geraet eine
    gueltige Bedienung an."""
    env = geraet_env
    anlage = Anlage(env, tmp_path)
    konfiguration, store = anlage.konfiguration, anlage.device.documents
    assert (store.version("konfiguration"), store.version("bedienung")) == (1, 1)
    konfigurationen, bedienungen = _kaputte_konfigurationen(konfiguration), _kaputte_bedienungen()
    zaehler = {"konfiguration": 0, "bedienung": 0, "antwort": 0, "gueltige_antwort": 0}
    gelesen = 0  # Dokumente, die das Geraet liest und mit up/result ablehnt
    laeufe = {"komfort_boost": 0, "notfall_boost": 0, "notbetrieb": 0}
    ticks_vorher, schreibwerte_vorher = len(anlage.snapshots()), len(env.ha.writes)

    with caplog.at_level(logging.INFO):
        for runde in range(KAPUTT_ANZAHL):
            _set_room_target(env, anlage.bridge, 21.0 + 0.5 * (runde % 4))  # neuer Wunsch: ein Tick je Runde
            art = ("konfiguration", "bedienung", "antwort")[runde % 3]
            nr = runde // 3
            if art == "konfiguration":
                name, bauen = konfigurationen[nr % len(konfigurationen)]
                koerper = bauen(2 + runde)
                if isinstance(koerper, bytes):
                    anlage.roh_senden("down/config", koerper)
                else:
                    anlage.senden("down/config", koerper)
            elif art == "bedienung":
                name, bauen = bedienungen[nr % len(bedienungen)]
                anlage.senden("down/operation", bauen(2 + runde))
            else:
                name, koerper = _kaputte_antworten(anlage.snapshots()[-1]["seq"])[nr % 11]
                anlage.senden("down/setpoints", koerper)
            zaehler[art] += 1
            gelesen += int(art != "antwort" and name not in UNGELESEN)
            if runde % 10 == 9:  # und zwischendurch eine gueltige Antwort mit Werten ausserhalb der Grenzen, im Wechsel
                env.ha.states["sensor.room_actual"] = 23.0  # ueber dem Wunsch: ein laufender Boost endet, die Antwort gilt
                _trigger(env, anlage.bridge, "sensor.room_actual")
                anlage.antwort(**WILDE_ANTWORTEN[(runde // 10) % 2])
                zaehler["gueltige_antwort"] += 1
            anlage.runde(30)  # Ack-Timeouts und Retries laufen mit
            if runde % 10 == 9:
                env.ha.states["sensor.room_actual"] = 20.0
                _trigger(env, anlage.bridge, "sensor.room_actual")
            for stand, aktiv in anlage.stand().items():
                laeufe[stand] += int(aktiv)
            if runde == 49:  # in der Mitte: nichts uebernommen
                assert (store.version("konfiguration"), store.version("bedienung")) == (1, 1)

    assert zaehler == {"konfiguration": 34, "bedienung": 33, "antwort": 33, "gueltige_antwort": 10}
    # jedes gelesene kaputte Dokument wurde mit up/result abgelehnt (die ungelesenen schweigen), keines uebernommen
    abgelehnt = [m for m in anlage.link.of("up/result") if "dokument" in m and not m["ok"]]
    assert len(abgelehnt) == gelesen == 57
    assert {(m["dokument"], m["error"]["grund"]) for m in abgelehnt} == {
        ("konfiguration", "ungueltig"), ("konfiguration", "hebelsatz_eingerastet"),
        ("bedienung", "ungueltig"), ("bedienung", "ausserhalb_bereich")}
    # die gespeicherte Konfiguration und Bedienung sind unveraendert die gueltigen, auch von der Platte gelesen
    assert store.inhalt("konfiguration") == konfiguration
    assert store.inhalt("bedienung")["raum_soll"] == 21.0
    assert (store.version("konfiguration"), store.version("bedienung")) == (1, 1)
    neu = DocumentStore(anlage.world.dir, host="ha", software_version="1.0.0")
    assert (neu.inhalt("konfiguration"), neu.version("konfiguration")) == (konfiguration, 1)
    assert (neu.inhalt("bedienung"), neu.version("bedienung")) == (store.inhalt("bedienung"), 1)
    # die Laufzeit blieb dieselbe (kein Neustart: run_pending haette Neustart geworfen) und tickte weiter
    assert len(anlage.snapshots()) - ticks_vorher >= 50
    # Schreibwerte nur in den Grenzen; die gueltigen Antworten wurden geschrieben und geklemmt
    assert _alle_schreibwerte_in_den_grenzen(env.ha) > schreibwerte_vorher
    # Der Notfall-Boost startet hoechstens alle 30 min (A6-34): die Stichprobe am Rundenende trifft ihn nicht mehr, weil
    # sein einziger Start in eine Runde ohne Notbetrieb am Rundenende faellt. Er lief mit, wenn sein Start gezaehlt ist,
    # die Notfall-Zeile geschrieben wurde (Log) und die Maximalwerte in den Schreibvorgaengen stehen.
    emergency_start = anlage.bridge.store.state.write_budget.get("emergency_start", {}).get("count", 0)
    assert emergency_start > 0 and any(r.getMessage().startswith("Notfall-Boost aktiv") for r in caplog.records)
    maxima = {(entity, BEREICHE[hebel][1]) for entity, hebel in ENTITIES.items() if hebel != "min_flow"}
    assert maxima <= set(env.ha.writes)
    laeufe["notfall_boost"] += emergency_start
    assert all(anzahl > 0 for anzahl in laeufe.values()), laeufe  # Boost und Notbetrieb liefen mit (kein leeres Szenario)
    # zu niedrige Antwortwerte kamen als unterer Grenzwert an (den schreibt sonst nichts); die obere Seite deckt
    # _alle_schreibwerte_in_den_grenzen ab (der Comfort-Boost schreibt dort ohnehin das Maximum)
    for entity, hebel in (("number.curve_current", "curve"), ("number.shift_current", "room_setpoint"),
                          ("number.heat_limit", "heat_limit")):
        assert (entity, BEREICHE[hebel][0]) in env.ha.writes, hebel
    assert anlage.bridge.store.state.learned == {}  # die gueltigen Antworten lernen nichts, die kaputten bleiben ohne Wirkung
    # Gegenprobe: das Geraet nimmt nach der Flut ein gueltiges Dokument an
    anlage.senden("down/operation", {"schema": 1, "version": 500, "modus": "manuell", "raum_soll": 22.0,
                                     "quelle": "portal", "ts": "2027-01-15T07:00:00+00:00"})
    anlage.runde(30)
    assert (store.version("bedienung"), store.inhalt("bedienung")["raum_soll"]) == (500, 22.0)
    fehler = _fehler_im_log(caplog)
    assert [r.getMessage() for r in fehler if r.exc_info] == []
    assert [r.getMessage() for r in fehler] == []
