"""Zustand `ausser_betrieb` eines Kreises am Geraet: einmalige Rueckstellung auf die Ursprungswerte, danach schreibt das
Geraet fuer den Kreis nur noch den Kaelteschutz (nur mit Frostnaehe-Ausloeser), die Telemetrie traegt nur Raumwerte, die
Rueckkehr nach `in_betrieb` braucht keinen Neustart. Die Laeufe mit `env` pruefen die Aussensicht (HA-Writes, Sendungen);
die Kaelteschutz-Faelle bauen den Kreis von Hand wie test_runtime_kaelteschutz.py. Der Zustand kommt aus dem
Konfigurationsdokument (kreise[n].zustand) und wechselt zur Laufzeit."""
import dataclasses
import types
from datetime import datetime

import pytest
from runtime_helpers import (
    _abo_status,  # noqa: F401 - nur der Namensraum der Helfer
    _advance,
    _answer,
    _is_running,
    _mqtt,
    _quiet_backup,
    _set_room_target,
    _start,
)
from test_runtime_kaelteschutz import (
    EMERGENCY,
    MINUTE,
    RESTORE,
    STUNDE,
    _ist,
    _kreis,
    _lauf,
    _lauft,
    _neu,
    _pruefen,
    _store_in,
    _writes,
    uhr,  # noqa: F401 - Fixture
    wall,  # noqa: F401 - Fixture
)
from test_runtime_kreise import Anlage, _aufnahme_haengt, _laufzeit, _zwei_kreise_dokument
from test_runtime_testphase import (
    BETRIEB,
    _check,
    _hebel_schreibvorgaenge,
    _lauf_mit_serverwerten,
    _ursprung,
    _zustand,
)

from smartheat_runtime import app, entitlement, kreiszustand
from smartheat_runtime.backup_store import save_backup
from smartheat_runtime.runtime import (
    EV_ACK_TIMEOUT,
    EV_GRACE_CHECK,
    EV_MQTT_CONNECTED,
    EV_RETRY_DUE,
    EV_TELEMETRY,
    Runtime,
)
from smartheat_runtime.worker import Event

AUSSER = "ausser_betrieb"
TELEMETRIE_FELDER = {"kreis", "room_actual", "boost_active", "failsafe_active", "ts", "raeume"}


# --- Aussensicht (env) ---

def test_der_eintritt_stellt_genau_die_ursprungswerte_einmal_zurueck_und_danach_ruht_jedes_schreiben(env):
    bridge = _lauf_mit_serverwerten(env)
    ursprung = _ursprung(bridge)
    assert ursprung["curve"] != 0.95
    vorher = len(env.ha.writes)

    _zustand(env, AUSSER)
    _check(env, bridge)

    zurueck = _hebel_schreibvorgaenge(env, vorher)
    assert len(zurueck) == 3  # jeder abweichende Hebel genau einmal (die Heizgrenze steht schon auf dem Ursprung)
    assert dict(zurueck) == {"number.curve_current": ursprung["curve"], "number.shift_current": ursprung["room_setpoint"],
                             "number.min_flow": ursprung["min_flow"]}
    assert bridge.store.state.boost_active is False

    danach = len(env.ha.writes)
    for runde in range(12):  # neue Serverwerte, Wuensche, ein von aussen verstellter Hebel
        env.ha.states["number.curve_current"] = 1.3 + runde * 0.01
        _advance(env, bridge, 20 * MINUTE)
        _set_room_target(env, bridge, 20.0 + (runde % 2))
        _check(env, bridge)
    assert env.ha.writes[danach:] == []
    assert env.ha.states["number.curve_current"] == pytest.approx(1.3 + 11 * 0.01)  # nichts durchgesetzt


def test_ein_hoeherer_wunsch_startet_im_ausser_betrieb_keinen_boost(env):
    bridge = _lauf_mit_serverwerten(env)
    _zustand(env, AUSSER)
    _check(env, bridge)
    danach = len(env.ha.writes)
    env.ha.states["sensor.room_actual"] = 17.0
    _set_room_target(env, bridge, 25.0)  # Erhoehung bei kaltem Raum: in Betrieb ein Comfort-Boost
    _check(env, bridge)
    _advance(env, bridge, 10 * MINUTE)
    assert bridge.store.state.boost_active is False
    assert env.ha.writes[danach:] == []


def test_ein_laufender_comfort_boost_endet_mit_dem_eintritt(env):
    bridge = _lauf_mit_serverwerten(env)
    env.ha.states["sensor.room_actual"] = 17.0
    _set_room_target(env, bridge, 24.0)
    assert bridge.store.state.boost_active is True
    _zustand(env, AUSSER)
    _check(env, bridge)
    assert bridge.store.state.boost_active is False
    assert env.ha.states["number.curve_current"] == _ursprung(bridge)["curve"]


def test_die_telemetrie_traegt_nur_raumwerte_und_es_gibt_keine_snapshots(env):
    bridge = _lauf_mit_serverwerten(env)
    _zustand(env, AUSSER)
    _check(env, bridge)
    telemetrie, snapshots = len(_mqtt(env).telemetry), len(_mqtt(env).snapshots)

    _advance(env, bridge, 300)
    _set_room_target(env, bridge, 20.0)
    _advance(env, bridge, STUNDE)

    neu = _mqtt(env).telemetry[telemetrie:]
    assert neu
    for paket in neu:
        assert set(paket) == TELEMETRIE_FELDER
        assert paket["boost_active"] is False and paket["failsafe_active"] is False
        assert paket["room_actual"] == env.ha.states["sensor.room_actual"]
        assert all(set(raum) == {"raum", "ist"} for raum in paket["raeume"])
    assert len(_mqtt(env).snapshots) == snapshots  # weder Wunschwechsel noch Tagestick noch Takt


def test_ein_ausstehender_tick_wird_im_ausser_betrieb_nicht_neu_gesendet(env):
    bridge = _lauf_mit_serverwerten(env)
    _set_room_target(env, bridge, 20.0)  # offener Tick ohne Antwort
    _zustand(env, AUSSER)
    snapshots = len(_mqtt(env).snapshots)
    _check(env, bridge)
    _advance(env, bridge, 2 * STUNDE)
    assert len(_mqtt(env).snapshots) == snapshots


def test_eine_serverantwort_im_ausser_betrieb_wird_nur_quittiert_und_schreibt_nichts(env):
    bridge = _lauf_mit_serverwerten(env)
    _set_room_target(env, bridge, 20.0)  # offener Tick
    seq = _mqtt(env).snapshots[-1]["seq"]
    _zustand(env, AUSSER)
    _check(env, bridge)
    vorher = len(env.ha.writes)
    _answer(env, bridge, seq, curve=1.4, shift=25.0)
    assert env.ha.writes[vorher:] == []
    assert bridge.store.state.delivery.pending is None  # quittiert, nicht stehen geblieben


def test_die_rueckkehr_nimmt_die_serverwerte_ohne_neustart_wieder_auf(env):
    bridge = _lauf_mit_serverwerten(env)
    ursprung = _ursprung(bridge)
    _zustand(env, AUSSER)
    _check(env, bridge)
    _advance(env, bridge, 20 * MINUTE)
    _set_room_target(env, bridge, 20.0)
    assert env.ha.states["number.curve_current"] == ursprung["curve"]

    _zustand(env, BETRIEB)
    _check(env, bridge)
    _advance(env, bridge, 20 * MINUTE)
    _set_room_target(env, bridge, 19.5)
    _answer(env, bridge, _mqtt(env).snapshots[-1]["seq"], curve=0.9, shift=21.0)
    assert _is_running(bridge) and env.geraet.neustarts == []
    assert env.ha.states["number.curve_current"] == 0.9
    assert env.ha.states["number.shift_current"] == 21.0


def test_scheitert_die_rueckstellung_bleibt_sie_offen_und_der_naechste_check_wiederholt_sie(env):
    bridge = _lauf_mit_serverwerten(env)
    ursprung = _ursprung(bridge)
    env.ha.write_error = RuntimeError("HA nicht erreichbar")
    _zustand(env, AUSSER)
    _check(env, bridge)
    assert env.ha.states["number.curve_current"] == 0.95  # nichts zurueckgestellt

    env.ha.write_error = None
    _advance(env, bridge, 15 * MINUTE)  # ueber die Wartezeit der Rueckweg-Staffel hinaus
    _check(env, bridge)
    assert env.ha.states["number.curve_current"] == ursprung["curve"]
    danach = len(env.ha.writes)
    _advance(env, bridge, 30 * MINUTE)
    _check(env, bridge)
    assert env.ha.writes[danach:] == []  # erledigt: kein weiterer Versuch
    assert not bridge.store.state.abo_finished


def test_altbestand_ohne_zustandsfeld_regelt(env):
    bridge = _lauf_mit_serverwerten(env)
    env.geraet._konfiguration["kreise"][0].pop("zustand", None)
    _check(env, bridge)
    _advance(env, bridge, 20 * MINUTE)
    _set_room_target(env, bridge, 19.5)
    _answer(env, bridge, _mqtt(env).snapshots[-1]["seq"], curve=0.9, shift=21.0)
    assert env.ha.states["number.curve_current"] == 0.9


def test_ein_kreis_ausser_betrieb_ohne_abo_sendet_raumwerte_und_bleibt_ohne_notbetrieb(env):
    _quiet_backup(env)
    env.abo["status"] = entitlement.INACTIVE
    env.ha.states["sensor.room_actual"] = 19.0  # in Betrieb ohne Abo: Notfall-Boost
    bridge = _start(env, zustand=AUSSER)

    assert bridge.mqtt_client is not None  # die Schutz-Telemetrie braucht die Verbindung
    assert bridge.store.state.abo_inactive_since is None and bridge.store.state.abo_finished is False
    assert bridge.store.state.delivery.notbetrieb is False
    ursprung = dict(bridge.store.state.restore_point)  # die Anlage steht auf ihren Ursprungswerten
    eintritt = _hebel_schreibvorgaenge(env, 0)  # hoechstens die einmalige Rueckstellung auf genau diese Werte
    assert all(wert == env.ha.states[entity] for entity, wert in eintritt)
    assert not bridge.store.state.emergency_boost_active and not bridge.store.state.boost_active
    _advance(env, bridge, 300)
    assert _mqtt(env).telemetry and set(_mqtt(env).telemetry[-1]) == TELEMETRIE_FELDER
    assert _mqtt(env).snapshots == []
    assert _hebel_schreibvorgaenge(env, 0) == eintritt
    assert dict(bridge.store.state.restore_point) == ursprung


# --- Kaelteschutz im Kreis von Hand (Zustand ueber die Config) ---

def _kreis_ausser(rt, zustand=AUSSER):
    """Macht den Kreis zu einem mit wechselbarem Zustand (`rt.zustand[0]`)."""
    rt.zustand = [zustand]
    rt.config = dataclasses.replace(rt.config, zustand_source=lambda: rt.zustand[0])
    rt.ausser_betrieb_rueckgestellt = False
    return rt


def _ausser(make_store, wall, uhr, ist, **optionen):  # noqa: F811
    return _kreis_ausser(_neu(make_store, wall, uhr, ist, **optionen))


def test_der_eintritt_stellt_zurueck_und_ruht_dann_im_kreis_von_hand(make_store, wall, uhr):  # noqa: F811
    rt = _ausser(make_store, wall, uhr, {1: 21.0})
    _pruefen(rt)
    assert _writes(rt) == RESTORE
    _lauf(rt, 2 * STUNDE)
    assert _writes(rt) == RESTORE
    assert rt.ausser_betrieb_rueckgestellt is True
    assert rt.store.state.abo_finished is False


def test_der_soll_ausloeser_startet_im_ausser_betrieb_nicht(make_store, wall, uhr):  # noqa: F811
    rt = _ausser(make_store, wall, uhr, {1: 21.0})
    _pruefen(rt)
    rt.ha_api.set_number_value.reset_mock()
    _ist(rt, r1=18.0)  # 3 K unter dem Wunsch, drei Stunden und mehr
    _lauf(rt, 5 * STUNDE)
    assert rt.store.state.kaelteschutz_art is None and not _lauft(rt)
    assert _writes(rt) == []


def test_der_frostnaehe_kaelteschutz_startet_im_ausser_betrieb_und_endet_bei_allen_raeumen_ab_7_grad(make_store, wall, uhr):  # noqa: F811
    rt = _ausser(make_store, wall, uhr, {1: 21.0, 2: 21.0})
    _pruefen(rt)
    rt.ha_api.set_number_value.reset_mock()
    _ist(rt, r1=4.0, r2=4.5)
    _pruefen(rt)
    _lauf(rt, 10 * MINUTE, schritt=MINUTE)
    assert not _lauft(rt)  # noch keine 15 Minuten
    _lauf(rt, 6 * MINUTE, schritt=MINUTE)
    assert _lauft(rt) and rt.store.state.kaelteschutz_art == "frost"
    assert _writes(rt) == EMERGENCY
    _ist(rt, r1=8.0)  # ein Raum noch unter 7 Grad: der Lauf haelt
    _lauf(rt, 10 * MINUTE)
    assert _lauft(rt)
    _ist(rt, r2=7.0)
    _lauf(rt, 10 * MINUTE)
    assert not _lauft(rt) and rt.store.state.kaelteschutz_art is None
    # Das Ende des Laufs stellt die Hebel zurueck, der naechste Check stellt noch einmal zurueck (Zone und Hilfswerte;
    # die Anlage-Attrappe merkt sich ihren Stand nicht, eine echte Anlage ueberspringt das zweite Schreiben).
    assert _writes(rt) == EMERGENCY + RESTORE + RESTORE
    rt.ha_api.set_number_value.reset_mock()
    _lauf(rt, 2 * STUNDE)
    assert _writes(rt) == []


def test_der_frostlauf_endet_nach_vier_stunden(make_store, wall, uhr):  # noqa: F811
    rt = _ausser(make_store, wall, uhr, {1: 21.0})
    _pruefen(rt)
    _ist(rt, r1=4.0)
    _lauf(rt, 16 * MINUTE, schritt=MINUTE)
    assert _lauft(rt)
    _lauf(rt, 4 * STUNDE)
    assert not _lauft(rt)


def _ohne_kreissensor(rt):
    rt.manifest = dataclasses.replace(rt.manifest, refs={k: v for k, v in rt.manifest.refs.items() if k != "room_actual"})


def test_ohne_referenzraeume_schuetzt_der_kreissensor(make_store, wall, uhr):  # noqa: F811
    rt = _ausser(make_store, wall, uhr, {}, referenz=())
    _pruefen(rt)
    rt.werte["sensor.mittel"] = 4.0
    _lauf(rt, 16 * MINUTE, schritt=MINUTE)
    assert _lauft(rt) and rt.store.state.kaelteschutz_art == "frost"


def test_ohne_referenzraeume_und_ohne_kreissensor_passiert_nichts(make_store, wall, uhr):  # noqa: F811
    rt = _ausser(make_store, wall, uhr, {1: 4.0}, referenz=())
    _ohne_kreissensor(rt)
    _pruefen(rt)  # der Eintritt stellt zurueck
    rt.ha_api.set_number_value.reset_mock()
    _lauf(rt, 2 * STUNDE, schritt=MINUTE)
    assert _writes(rt) == [] and rt.store.state.kaelteschutz_art is None


def test_mit_referenzraeumen_genuegt_der_raum_ohne_kreissensor(make_store, wall, uhr):  # noqa: F811
    rt = _ausser(make_store, wall, uhr, {1: 4.0})
    _ohne_kreissensor(rt)
    _lauf(rt, 16 * MINUTE, schritt=MINUTE)
    assert _lauft(rt) and rt.store.state.kaelteschutz_art == "frost"


def test_ein_fuehlerfehler_loest_im_ausser_betrieb_nichts_aus(make_store, wall, uhr):  # noqa: F811
    rt = _ausser(make_store, wall, uhr, {1: 21.0})
    _pruefen(rt)
    rt.ha_api.set_number_value.reset_mock()
    _ist(rt, r1=Exception("unavailable"))
    _lauf(rt, 3 * STUNDE)
    assert _writes(rt) == [] and rt.store.state.kaelteschutz_art is None


def test_im_notbetrieb_gibt_es_im_ausser_betrieb_keinen_notfall_boost(make_store, wall, uhr):  # noqa: F811
    rt = _ausser(make_store, wall, uhr, {1: 18.0}, notbetrieb=True)  # Notbetrieb, 3 K unter dem Wunsch
    _lauf(rt, 5 * STUNDE)
    assert rt.store.state.emergency_boost_active is False
    assert _writes(rt) == RESTORE


def test_ein_notfall_boost_des_notbetriebs_endet_mit_dem_eintritt(make_store, wall, uhr):  # noqa: F811
    rt = _neu(make_store, wall, uhr, {1: 18.0}, notbetrieb=True)
    _kreis_ausser(rt, BETRIEB)
    _pruefen(rt)
    assert rt.store.state.emergency_boost_active is True  # in Betrieb ohne Server: Notfall-Boost
    rt.zustand[0] = AUSSER
    _lauf(rt, 10 * MINUTE)
    assert rt.store.state.emergency_boost_active is False
    assert _writes(rt)[-2:] == RESTORE


def test_nach_dem_abo_ende_des_geraets_laeuft_auch_im_ausser_betrieb_kein_kaelteschutz(make_store, wall, uhr):  # noqa: F811
    rt = _ausser(make_store, wall, uhr, {1: 4.0})
    rt.store.update(abo_finished=True)
    _lauf(rt, 2 * STUNDE, schritt=MINUTE)
    assert _writes(rt) == [] and rt.store.state.kaelteschutz_art is None
    assert rt.ausser_betrieb_rueckgestellt is False


def test_ein_laufender_frostlauf_schiebt_die_rueckstellung_bis_zu_seinem_ende_auf(make_store, wall, uhr):  # noqa: F811
    rt = _ausser(make_store, wall, uhr, {1: 4.0}, soll=None)
    rt.zustand[0] = BETRIEB
    _lauf(rt, 16 * MINUTE, schritt=MINUTE)
    assert rt.store.state.kaelteschutz_art == "frost" and _writes(rt) == EMERGENCY
    rt.zustand[0] = AUSSER
    _lauf(rt, 30 * MINUTE)
    assert rt.store.state.kaelteschutz_art == "frost"  # der Lauf schuetzt weiter, nichts wird zurueckgestellt
    assert _writes(rt) == EMERGENCY and rt.ausser_betrieb_rueckgestellt is False
    _ist(rt, r1=9.0)
    _lauf(rt, 10 * MINUTE)
    assert rt.store.state.kaelteschutz_art is None and rt.ausser_betrieb_rueckgestellt is True
    # Das Ende des Laufs stellt zurueck, danach der Eintritt auf dieselben Werte (die Anlage-Attrappe merkt sich ihren
    # Stand nicht; eine echte Anlage ueberspringt das zweite Schreiben).
    assert _writes(rt) == EMERGENCY + RESTORE + RESTORE
    _lauf(rt, STUNDE)
    assert _writes(rt) == EMERGENCY + RESTORE + RESTORE


def test_die_rueckkehr_beendet_einen_laufenden_frostlauf_nach_den_ankunftsregeln(make_store, wall, uhr):  # noqa: F811
    rt = _ausser(make_store, wall, uhr, {1: 21.0})
    _pruefen(rt)
    _ist(rt, r1=4.0)
    _lauf(rt, 16 * MINUTE, schritt=MINUTE)
    assert _lauft(rt)
    rt.zustand[0] = BETRIEB
    _ist(rt, r1=21.0)
    _lauf(rt, 10 * MINUTE)
    assert not _lauft(rt) and rt.ausser_betrieb_rueckgestellt is False  # der Merker faellt mit der Rueckkehr


def test_zwei_kreise_laufen_unabhaengig(tmp_path, wall, uhr):  # noqa: F811
    def kreis(name, zustand):
        store = _store_in(tmp_path / name, {"restore_point": {"curve": 0.3, "room_setpoint": 1.0}, "last_room_target": 21.0})
        rt = _kreis(store, wall, uhr, {1: 18.0}, soll=21.0)
        return _kreis_ausser(rt, zustand)

    a, b = kreis("a", AUSSER), kreis("b", BETRIEB)
    for _ in range(5 * STUNDE // (5 * MINUTE)):
        wall.advance(5 * MINUTE)
        uhr.advance(5 * MINUTE)
        _pruefen(a)
        _pruefen(b)
    assert _writes(a) == RESTORE and a.store.state.kaelteschutz_art is None  # A: nur die Rueckstellung
    assert b.store.state.kaelteschutz_art == "soll"  # B: der Soll-Ausloeser gilt weiter
    assert _writes(b) == EMERGENCY


# --- kreiszustand.pruefen (reine Entscheidung, ohne Anlage) ---

def _stub(zustand, *, abo_finished=False, rueckgestellt=False, restore_ok=True):
    aufrufe = []
    override = types.SimpleNamespace(restore_and_clear=lambda immer: aufrufe.append(immer) or restore_ok)
    rt = types.SimpleNamespace(
        config=types.SimpleNamespace(zustand_source=lambda: zustand),
        store=types.SimpleNamespace(state=types.SimpleNamespace(abo_finished=abo_finished, kaelteschutz_art=None)),
        override=override, ausser_betrieb_rueckgestellt=rueckgestellt, zone_prepared=True)
    return rt, aufrufe


def test_nach_dem_abo_ende_gibt_es_nichts_zurueckzustellen():
    rt, aufrufe = _stub(AUSSER, abo_finished=True)
    assert kreiszustand.pruefen(rt) is False
    assert aufrufe == []


def test_ausserhalb_des_ausser_betriebs_faellt_der_merker():
    rt, aufrufe = _stub(BETRIEB, rueckgestellt=True)
    assert kreiszustand.pruefen(rt) is False
    assert rt.ausser_betrieb_rueckgestellt is False
    assert aufrufe == []


def test_ein_anderer_zustand_ist_nicht_ausser_betrieb():
    assert kreiszustand.ausser_betrieb(_stub("testphase_ohne")[0]) is False
    assert kreiszustand.ausser_betrieb(_stub(AUSSER)[0]) is True


def test_der_kreis_ruht_erst_nach_erfolgreicher_rueckstellung_und_die_rueckstellung_leert():
    rt, aufrufe = _stub(AUSSER)
    assert kreiszustand.pruefen(rt) is True
    assert rt.ausser_betrieb_rueckgestellt is True
    assert kreiszustand.pruefen(rt) is True
    assert aufrufe == [True]  # nur beim Eintritt, immer auch den Comfort-Boost zurueck
    offen, _ = _stub(AUSSER, restore_ok=False)
    assert kreiszustand.pruefen(offen) is False
    assert offen.ausser_betrieb_rueckgestellt is False


def test_nach_der_rueckstellung_gilt_die_zone_als_nicht_vorbereitet():
    rt, _ = _stub(AUSSER)
    assert kreiszustand.pruefen(rt) is True
    assert rt.zone_prepared is False
    offen, _ = _stub(AUSSER, restore_ok=False)
    kreiszustand.pruefen(offen)
    assert offen.zone_prepared is True


# --- Laufzeit: der Kreis nimmt den Wechsel ohne Prozessneustart auf ---

@pytest.mark.parametrize("alt,neu", [({}, {"zustand": AUSSER}), ({"zustand": AUSSER}, {"zustand": BETRIEB})])
def test_ein_wechsel_nach_oder_aus_ausser_betrieb_startet_nur_diesen_kreis_neu(env, alt, neu):
    laufzeit = _laufzeit(Anlage(env), env)
    eins, zwei = laufzeit.kreise()[1], laufzeit.kreise()[2]

    assert laufzeit.konfiguration_geaendert(_zwei_kreise_dokument(**alt), _zwei_kreise_dokument(**neu)) is False

    assert laufzeit.kreise()[1] is eins  # Kreis 1 unberuehrt
    assert laufzeit.kreise()[2] is not zwei and isinstance(laufzeit.kreise()[2], Runtime)


def test_ein_wechsel_nach_ausser_betrieb_waehrend_der_aufnahme_wird_danach_nachgezogen(env, monkeypatch):
    laufzeit = _laufzeit(Anlage(env), env)
    aufnahme, frei, starts = _aufnahme_haengt(laufzeit, 2, monkeypatch)

    assert laufzeit.konfiguration_geaendert(_zwei_kreise_dokument(), _zwei_kreise_dokument(zustand=AUSSER)) is False
    assert starts == [2]
    frei.set()
    aufnahme.join(10)

    assert not aufnahme.is_alive() and starts == [2, 2]


def test_ein_wechsel_in_die_testphase_startet_keinen_kreis_neu(env):
    laufzeit = _laufzeit(Anlage(env), env)
    eins, zwei = laufzeit.kreise()[1], laufzeit.kreise()[2]
    laufzeit.konfiguration_geaendert(_zwei_kreise_dokument(), _zwei_kreise_dokument(zustand="testphase_ohne"))
    assert laufzeit.kreise()[1] is eins and laufzeit.kreise()[2] is zwei



# --- Ereignisse eines Kreises ausser Betrieb (jeder Handler allein, gegen den Kreis in Betrieb) ---

@pytest.mark.parametrize("zustand, erwartet", [(AUSSER, 0), (BETRIEB, 1)])
@pytest.mark.parametrize("handler, art", [
    (app._on_ack_timeout, EV_ACK_TIMEOUT), (app._on_retry_due, EV_RETRY_DUE), (app._on_mqtt_connected, EV_MQTT_CONNECTED),
])
def test_die_zustellung_ruht_im_ausser_betrieb(env, monkeypatch, handler, art, zustand, erwartet):
    bridge = _lauf_mit_serverwerten(env)
    _zustand(env, zustand)
    aufrufe = []
    monkeypatch.setattr(app.ticks, "deliver", lambda rt, ereignis: aufrufe.append(ereignis))
    handler(bridge, Event(art, {"seq": "s", "gen": 1}))
    assert len(aufrufe) == erwartet


@pytest.mark.parametrize("zustand, erwartet", [(AUSSER, 0), (BETRIEB, 1)])
def test_ausser_betrieb_gibt_es_keine_abo_frist(env, monkeypatch, zustand, erwartet):
    bridge = _lauf_mit_serverwerten(env)
    _zustand(env, zustand)
    aufrufe = []
    monkeypatch.setattr(app.abo, "check_grace_end", aufrufe.append)
    app._on_grace_check(bridge, Event(EV_GRACE_CHECK))
    assert len(aufrufe) == erwartet


@pytest.mark.parametrize("zustand, erwartet", [(AUSSER, 0), (BETRIEB, 1)])
def test_ausser_betrieb_wird_die_zone_nicht_vorbereitet(env, monkeypatch, zustand, erwartet):
    bridge = _lauf_mit_serverwerten(env)
    _zustand(env, zustand)
    aufrufe = []
    monkeypatch.setattr(app, "_prepare_zone_gesperrt", aufrufe.append)
    app._prepare_zone(bridge)
    assert len(aufrufe) == erwartet


@pytest.mark.parametrize("zustand, abo_inaktiv, felder", [
    (AUSSER, False, TELEMETRIE_FELDER), (AUSSER, True, TELEMETRIE_FELDER), (BETRIEB, False, None), (BETRIEB, True, "keine"),
])
def test_die_telemetrie_unterscheidet_ausser_betrieb_und_abo_ruhe(env, zustand, abo_inaktiv, felder):
    bridge = _lauf_mit_serverwerten(env)
    _zustand(env, zustand)
    if abo_inaktiv:
        bridge.store.update(abo_inactive_since=datetime.now().astimezone())
    vorher = len(_mqtt(env).telemetry)
    app._on_telemetry(bridge, Event(EV_TELEMETRY))
    neu = _mqtt(env).telemetry[vorher:]
    if felder == "keine":
        assert neu == []
    elif felder is None:
        assert len(neu) == 1 and set(neu[0]) != TELEMETRIE_FELDER  # voller Satz mit Hebel- und Regelungsdaten
    else:
        assert len(neu) == 1 and set(neu[0]) == TELEMETRIE_FELDER


def test_der_verbindungswaechter_legt_ausser_betrieb_keinen_pruef_tick_an(env, monkeypatch):
    bridge = _lauf_mit_serverwerten(env)
    _zustand(env, AUSSER)
    bridge.mqtt_client.is_connected = lambda: False
    bridge.mqtt_down_since = bridge.clock() - 24 * STUNDE
    proben = []
    monkeypatch.setattr(app.ticks, "start_probe_tick", lambda rt, grund: proben.append(grund))
    app._on_connection_check(bridge, Event("connection_check"))
    assert proben == [] and bridge.mqtt_down_since is None


def test_der_start_im_ausser_betrieb_klaert_keinen_alten_notbetrieb_und_sendet_nichts(env, monkeypatch):
    _quiet_backup(env)
    save_backup(env.paths["FAILSAFE_PATH"], {"failsafe_active": True, "datenfehler": None, "pending": None})
    klaerungen = []
    monkeypatch.setattr(app, "_end_stale_notbetrieb", lambda rt, *, beenden: klaerungen.append(beenden))
    _start(env, zustand=AUSSER)
    assert klaerungen == [] and _mqtt(env).snapshots == []


@pytest.mark.parametrize("zustand, erwartet", [(AUSSER, []), (BETRIEB, ["alt-1"])])
def test_der_start_sendet_einen_offenen_tick_nur_in_betrieb_erneut(env, zustand, erwartet):
    _quiet_backup(env)
    save_backup(env.paths["FAILSAFE_PATH"], {
        "failsafe_active": True, "datenfehler": None, "pending": {"seq": "alt-1", "trigger": "daily"},
    })
    _start(env, zustand=zustand)
    assert [snapshot["seq"] for snapshot in _mqtt(env).snapshots] == erwartet


# --- V1b-Kennzeichen in der Telemetrie des Kreises ---

def _kennzeichen(env, bridge):
    vorher = len(_mqtt(env).telemetry)
    app._on_telemetry(bridge, Event(EV_TELEMETRY))
    neu = _mqtt(env).telemetry[vorher:]
    assert len(neu) == 1
    return neu[0]["testphase_ohne"], neu[0]["kaelteschutz"]


def test_die_telemetrie_nennt_testphase_und_kaelteschutz_des_kreises(env):
    bridge = _lauf_mit_serverwerten(env)
    assert _kennzeichen(env, bridge) == (False, "aus")
    _zustand(env, "testphase_ohne")
    assert _kennzeichen(env, bridge) == (True, "aus")
    _zustand(env, BETRIEB)
    for art in ("soll", "frost"):
        bridge.store.update(kaelteschutz_art=art, kaelteschutz_seit=datetime.now().astimezone().isoformat())
        assert _kennzeichen(env, bridge) == (False, art)
    bridge.store.update(kaelteschutz_art=None, kaelteschutz_seit=None)
    assert _kennzeichen(env, bridge) == (False, "aus")


def test_ausser_betrieb_traegt_auch_mit_laufendem_frostlauf_keine_kennzeichen(env):
    bridge = _lauf_mit_serverwerten(env)
    _zustand(env, AUSSER)
    bridge.store.update(kaelteschutz_art="frost", kaelteschutz_seit=datetime.now().astimezone().isoformat())
    vorher = len(_mqtt(env).telemetry)
    app._on_telemetry(bridge, Event(EV_TELEMETRY))
    neu = _mqtt(env).telemetry[vorher:]
    assert len(neu) == 1 and set(neu[0]) == TELEMETRIE_FELDER
