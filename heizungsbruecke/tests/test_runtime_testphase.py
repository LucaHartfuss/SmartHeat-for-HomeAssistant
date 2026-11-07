"""Zustand `testphase_ohne` eines Kreises am Geraet: einmalige Rueckstellung auf die Ursprungswerte ohne Leeren, danach
keine Schreibvorgaenge an die Hebel, Rueckkehr ohne Neustart, Kaelteschutz bleibt. Die Laeufe mit `env` pruefen die
Aussensicht (HA-Writes, Dateiinhalte, Sendungen); die Kaelteschutz-Faelle bauen den Kreis von Hand wie
test_runtime_kaelteschutz.py. Der Zustand kommt aus dem Konfigurationsdokument (kreise[n].zustand) und wechselt zur
Laufzeit, ohne dass das Geraet neu startet."""
import dataclasses
import types
from datetime import datetime

import pytest
from runtime_helpers import (
    _advance,
    _answer,
    _backup,
    _is_running,
    _mqtt,
    _set_room_target,
    _start,
    _trigger,
)
from test_runtime_kaelteschutz import (
    DAY,
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

from smartheat_runtime import testphase
from smartheat_runtime.backup_store import save_backup

OHNE = "testphase_ohne"
BETRIEB = "in_betrieb"
HEBEL = ("number.curve_current", "number.shift_current", "number.heat_limit", "number.min_flow")


def _zustand(env, wert):
    env.geraet._konfiguration["kreise"][0]["zustand"] = wert


def _lauf_mit_serverwerten(env):
    """Gestartet, der erste Tick ist beantwortet: die Serverwerte (0.95 / 23.0 / 16.0 / 20.5) stehen auf der Anlage."""
    # ohne Wiederherstellungspunkt: die Ursprungswerte aller vier Hebel werden beim Start von der Anlage gemerkt
    save_backup(env.paths["BACKUP_PATH"], {
        "last_room_target": 21.0, "last_published_target_rt": 21.0,
        "last_daily_trigger_date": datetime.now().date().isoformat()})
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)
    _answer(env, bridge, _mqtt(env).snapshots[-1]["seq"])
    assert env.ha.states["number.curve_current"] == 0.95
    return bridge


def _check(env, bridge):
    """Ein lokaler Check (Zustandsaenderung des Raumfuehlers)."""
    _trigger(env, bridge, "sensor.room_actual")


def _hebel_schreibvorgaenge(env, seit):
    return [(entity, wert) for entity, wert in env.ha.writes[seit:] if entity in HEBEL]


def _ursprung(bridge):
    return dict(bridge.store.state.originals)


def test_der_eintritt_stellt_genau_die_ursprungswerte_zurueck_und_danach_ruht_jedes_schreiben(env):
    bridge = _lauf_mit_serverwerten(env)
    ursprung = _ursprung(bridge)
    assert ursprung["curve"] != 0.95
    vorher = len(env.ha.writes)

    _zustand(env, OHNE)
    _check(env, bridge)

    zurueck = _hebel_schreibvorgaenge(env, vorher)
    # Die Heizgrenze steht schon auf ihrem Ursprungswert: sie wird nicht erneut geschrieben.
    assert ursprung["heat_limit"] == env.ha.states["number.heat_limit"]
    assert dict(zurueck) == {"number.curve_current": ursprung["curve"], "number.shift_current": ursprung["room_setpoint"],
                             "number.min_flow": ursprung["min_flow"]}
    assert len(zurueck) == 3  # jeder abweichende Hebel genau einmal

    danach = len(env.ha.writes)
    for runde in range(12):  # viele Takte: neue Serverwerte, Wuensche, ein von aussen verstellter Hebel
        env.ha.states["number.curve_current"] = 1.3 + runde * 0.01
        _advance(env, bridge, 20 * MINUTE)
        _set_room_target(env, bridge, 20.0 + (runde % 2))
        _answer(env, bridge, _mqtt(env).snapshots[-1]["seq"], curve=0.5 + runde * 0.01, shift=19.0)
        _check(env, bridge)
    assert env.ha.writes[danach:] == []
    assert env.ha.states["number.curve_current"] == pytest.approx(1.3 + 11 * 0.01)  # nichts durchgesetzt


def test_nach_der_rueckstellung_bleiben_ursprungswerte_hilfswerte_und_lernstand_unveraendert(env):
    bridge = _lauf_mit_serverwerten(env)
    gemerkt = _backup(env)
    vorher = (gemerkt["originals"], gemerkt.get("aux_originals", {}), gemerkt.get("learned"))
    assert vorher[2]
    _zustand(env, OHNE)
    _check(env, bridge)
    _advance(env, bridge, 20 * MINUTE)
    _set_room_target(env, bridge, 20.0)
    _mqtt(env).answer_raw(_mqtt(env).snapshots[-1]["seq"], learned={"curve": 0.31, "heat_limit": 12.0})
    bridge.worker.run_pending()
    nachher = _backup(env)
    assert (nachher["originals"], nachher.get("aux_originals", {}), nachher.get("learned")) == vorher
    assert not nachher.get("abo_finished")


def test_in_der_testphase_startet_kein_comfort_boost_auch_nicht_bei_erhoehtem_wunsch(env):
    bridge = _lauf_mit_serverwerten(env)
    _zustand(env, OHNE)
    _check(env, bridge)
    danach = len(env.ha.writes)
    _set_room_target(env, bridge, 25.0)  # der Wunsch steigt um 4 K: sonst Comfort-Boost
    _check(env, bridge)
    _advance(env, bridge, 10 * MINUTE)
    assert bridge.store.state.boost_active is False
    assert env.ha.writes[danach:] == []


def test_telemetrie_und_snapshots_laufen_in_der_testphase_weiter(env):
    bridge = _lauf_mit_serverwerten(env)
    _zustand(env, OHNE)
    _check(env, bridge)
    telemetrie = len(_mqtt(env).telemetry)
    _advance(env, bridge, 300)
    assert len(_mqtt(env).telemetry) == telemetrie + 1
    snapshots = len(_mqtt(env).snapshots)
    _set_room_target(env, bridge, 20.0)  # ein Wunschwechsel bleibt ein Tick mit Snapshot
    assert len(_mqtt(env).snapshots) == snapshots + 1
    assert bridge.store.state.last_room_target == 20.0


def test_die_rueckkehr_nimmt_die_serverwerte_ohne_neustart_wieder_auf(env):
    bridge = _lauf_mit_serverwerten(env)
    ursprung = _ursprung(bridge)
    _zustand(env, OHNE)
    _check(env, bridge)
    _advance(env, bridge, 20 * MINUTE)
    _set_room_target(env, bridge, 20.0)
    _answer(env, bridge, _mqtt(env).snapshots[-1]["seq"], curve=0.5, shift=19.0)  # nur quittiert, nicht geschrieben
    assert env.ha.states["number.curve_current"] == ursprung["curve"]

    _zustand(env, BETRIEB)
    _check(env, bridge)
    _advance(env, bridge, 20 * MINUTE)
    _set_room_target(env, bridge, 19.5)
    _answer(env, bridge, _mqtt(env).snapshots[-1]["seq"], curve=0.9, shift=21.0)
    assert _is_running(bridge) and env.geraet.neustarts == []
    assert env.ha.states["number.curve_current"] == 0.9
    assert env.ha.states["number.shift_current"] == 21.0
    assert _ursprung(bridge) == ursprung


def test_eine_zweite_testphase_stellt_zurueck_ohne_die_ursprungswerte_neu_zu_merken(env):
    bridge = _lauf_mit_serverwerten(env)
    ursprung = _ursprung(bridge)
    _zustand(env, OHNE)
    _check(env, bridge)
    _zustand(env, BETRIEB)
    _check(env, bridge)
    _advance(env, bridge, 20 * MINUTE)
    _set_room_target(env, bridge, 19.5)
    _answer(env, bridge, _mqtt(env).snapshots[-1]["seq"], curve=0.9, shift=21.0)
    assert env.ha.states["number.curve_current"] == 0.9

    vorher = len(env.ha.writes)
    _zustand(env, OHNE)
    _check(env, bridge)
    assert env.ha.states["number.curve_current"] == ursprung["curve"]
    assert env.ha.states["number.shift_current"] == ursprung["room_setpoint"]
    assert _ursprung(bridge) == ursprung
    nach_rueckstellung = len(env.ha.writes)
    assert nach_rueckstellung > vorher
    _advance(env, bridge, 30 * MINUTE)
    _check(env, bridge)
    assert len(env.ha.writes) == nach_rueckstellung


def test_scheitert_die_rueckstellung_bleibt_sie_offen_und_der_naechste_check_wiederholt_sie(env):
    bridge = _lauf_mit_serverwerten(env)
    ursprung = _ursprung(bridge)
    env.ha.write_error = RuntimeError("HA nicht erreichbar")
    _zustand(env, OHNE)
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


def test_altbestand_ohne_zustandsfeld_ist_in_betrieb_und_regelt(env):
    bridge = _lauf_mit_serverwerten(env)
    env.geraet._konfiguration["kreise"][0].pop("zustand", None)
    _check(env, bridge)
    _advance(env, bridge, 20 * MINUTE)
    _set_room_target(env, bridge, 19.5)
    _answer(env, bridge, _mqtt(env).snapshots[-1]["seq"], curve=0.9, shift=21.0)
    assert env.ha.states["number.curve_current"] == 0.9


# --- Kaelteschutz in der Testphase (Kreis von Hand gebaut, Zustand ueber die Config) ---

def _mit_zustand(rt, zustand=OHNE):
    """Macht den Kreis zu einem mit wechselbarem Zustand (`rt.zustand[0]`)."""
    rt.zustand = [zustand]
    rt.config = dataclasses.replace(rt.config, zustand_source=lambda: rt.zustand[0])
    return rt


def _kreis_ohne(make_store, wall, uhr, ist, **optionen):  # noqa: F811
    return _mit_zustand(_neu(make_store, wall, uhr, ist, **optionen))


def _neustart_ohne(make_store, wall, uhr, ist, zustand=OHNE):  # noqa: F811
    """Derselbe Kreis nach einem Prozessneustart: neuer Speicher aus denselben Dateien, neue Laufzeit."""
    return _mit_zustand(_kreis(make_store(), wall, uhr, ist, soll=21.0), zustand)


def _rueckgestellt(rt):
    return rt.store.state.testphase_rueckgestellt


def test_der_eintritt_stellt_zurueck_und_ruht_dann_im_kreis_von_hand(make_store, wall, uhr):  # noqa: F811
    rt = _kreis_ohne(make_store, wall, uhr, {1: 21.0})
    _pruefen(rt)
    assert _writes(rt) == RESTORE
    _lauf(rt, 2 * STUNDE)
    assert _writes(rt) == RESTORE
    assert _rueckgestellt(rt) is True


def test_ein_neustart_in_der_testphase_stellt_nicht_erneut_zurueck(make_store, wall, uhr):  # noqa: F811
    rt = _kreis_ohne(make_store, wall, uhr, {1: 21.0})
    _pruefen(rt)
    assert _writes(rt) == RESTORE
    neu = _neustart_ohne(make_store, wall, uhr, {1: 21.0})
    assert _rueckgestellt(neu) is True
    _lauf(neu, 2 * STUNDE)
    assert _writes(neu) == []  # die Handaenderung eines Bewohners bleibt stehen


def test_ein_zustandswechsel_zwischendurch_stellt_nach_dem_neustart_noch_einmal_zurueck(make_store, wall, uhr):  # noqa: F811
    rt = _kreis_ohne(make_store, wall, uhr, {1: 21.0})
    _pruefen(rt)
    rt.zustand[0] = BETRIEB
    _pruefen(rt)
    assert _rueckgestellt(rt) is False
    neu = _neustart_ohne(make_store, wall, uhr, {1: 21.0}, zustand=OHNE)  # neuer Eintritt nach dem Neustart
    _lauf(neu, 10 * MINUTE)
    assert _writes(neu) == RESTORE
    assert _rueckgestellt(neu) is True
    spaeter = _neustart_ohne(make_store, wall, uhr, {1: 21.0})
    _lauf(spaeter, 10 * MINUTE)
    assert _writes(spaeter) == []


def test_ein_bestandsstand_ohne_merker_stellt_einmal_zurueck(make_store, wall, uhr):  # noqa: F811
    rt = _kreis_ohne(make_store, wall, uhr, {1: 21.0}, backup={"last_daily_trigger_date": DAY})
    assert _rueckgestellt(rt) is False
    _pruefen(rt)
    assert _writes(rt) == RESTORE
    _lauf(rt, STUNDE)
    assert _writes(rt) == RESTORE


def test_der_merker_fehlt_in_backup_json_solange_er_nicht_gesetzt_ist(make_store, wall, uhr, tmp_path):  # noqa: F811
    rt = _kreis_ohne(make_store, wall, uhr, {1: 21.0})
    rt.zustand[0] = BETRIEB
    _lauf(rt, 10 * MINUTE)
    assert "testphase_rueckgestellt" not in (tmp_path / "backup.json").read_text()
    rt.zustand[0] = OHNE
    _pruefen(rt)
    assert '"testphase_rueckgestellt": true' in (tmp_path / "backup.json").read_text()


def test_zwei_kreise_fuehren_ihren_merker_getrennt(tmp_path, wall, uhr):  # noqa: F811
    def kreis(name, zustand):
        store = _store_in(tmp_path / name, {"restore_point": {"curve": 0.3, "room_setpoint": 1.0}, "last_room_target": 21.0})
        return _mit_zustand(_kreis(store, wall, uhr, {1: 21.0}, soll=21.0), zustand)

    a, b = kreis("a", OHNE), kreis("b", OHNE)
    _pruefen(a)
    _pruefen(b)
    assert _writes(a) == RESTORE and _writes(b) == RESTORE
    b.zustand[0] = BETRIEB
    _pruefen(b)
    assert _rueckgestellt(a) is True and _rueckgestellt(b) is False
    a2 = _mit_zustand(_kreis(_store_in(tmp_path / "a"), wall, uhr, {1: 21.0}, soll=21.0))
    b2 = _mit_zustand(_kreis(_store_in(tmp_path / "b"), wall, uhr, {1: 21.0}, soll=21.0))
    _pruefen(a2)
    _pruefen(b2)
    assert _writes(a2) == []  # Kreis A ruht seit dem Eintritt
    assert _writes(b2) == RESTORE  # Kreis B tritt neu ein


def test_der_soll_kaelteschutz_startet_in_der_testphase_und_endet_wieder_in_der_ruhe(make_store, wall, uhr):  # noqa: F811
    rt = _kreis_ohne(make_store, wall, uhr, {1: 21.0})
    _pruefen(rt)
    rt.ha_api.set_number_value.reset_mock()
    _ist(rt, r1=18.0)  # 3 K unter Soll
    _lauf(rt, 4 * STUNDE)
    assert rt.store.state.kaelteschutz_art == "soll"
    assert _lauft(rt) and _writes(rt) == EMERGENCY  # sein Schreiben ist erlaubt
    _ist(rt, r1=21.0)  # Ankunft: der Kaelteschutz endet
    _lauf(rt, STUNDE)
    assert not _lauft(rt) and rt.store.state.kaelteschutz_art is None
    assert rt.store.state.abo_finished is False
    # Rueckweg = Ursprungswerte: das Ende des Laufs stellt die Hebel zurueck, der naechste Check noch einmal (Zone und
    # Hilfswerte; die Attrappe merkt sich ihren Stand nicht, eine echte Anlage ueberspringt das zweite Schreiben).
    assert _writes(rt) == EMERGENCY + RESTORE + RESTORE
    rt.ha_api.set_number_value.reset_mock()
    _lauf(rt, 2 * STUNDE)
    assert _writes(rt) == []  # danach wieder Ruhe


def test_der_frostnaehe_kaelteschutz_startet_in_der_testphase(make_store, wall, uhr):  # noqa: F811
    rt = _kreis_ohne(make_store, wall, uhr, {1: 21.0})
    _pruefen(rt)
    rt.ha_api.set_number_value.reset_mock()
    _ist(rt, r1=4.0)  # nahe Frost, unabhaengig vom Soll
    _pruefen(rt)
    _lauf(rt, 15 * MINUTE, schritt=MINUTE)
    assert _lauft(rt) and rt.store.state.kaelteschutz_art == "frost"
    assert _writes(rt) == EMERGENCY


# --- Rueckgabe von pruefen (reine Entscheidung, ohne Anlage) ---

def _stub(zustand, *, abo_finished=False, rueckgestellt=False, restore_ok=True):
    aufrufe = []
    override = types.SimpleNamespace(restore_for_pause=lambda: aufrufe.append(1) or restore_ok)
    state = types.SimpleNamespace(abo_finished=abo_finished, testphase_rueckgestellt=rueckgestellt)
    store = types.SimpleNamespace(state=state, update=lambda **felder: state.__dict__.update(felder))
    rt = types.SimpleNamespace(
        config=types.SimpleNamespace(zustand_source=lambda: zustand), store=store, override=override,
        zone_prepared=True)
    return rt, aufrufe


def test_nach_dem_abo_ende_gibt_es_nichts_zurueckzustellen():
    rt, aufrufe = _stub(OHNE, abo_finished=True)
    assert testphase.pruefen(rt) is False
    assert aufrufe == []


def test_ausserhalb_der_testphase_ist_der_kreis_nicht_in_ruhe_und_der_merker_faellt():
    rt, aufrufe = _stub(BETRIEB, rueckgestellt=True)
    assert testphase.pruefen(rt) is False
    assert rt.store.state.testphase_rueckgestellt is False
    assert aufrufe == []


def test_der_kreis_ruht_erst_nach_erfolgreicher_rueckstellung():
    rt, aufrufe = _stub(OHNE)
    assert testphase.pruefen(rt) is True
    assert rt.store.state.testphase_rueckgestellt is True
    assert testphase.pruefen(rt) is True
    assert aufrufe == [1]  # nur beim Eintritt
    offen, _ = _stub(OHNE, restore_ok=False)
    assert testphase.pruefen(offen) is False
    assert offen.store.state.testphase_rueckgestellt is False


def test_nach_der_rueckstellung_gilt_die_zone_als_nicht_vorbereitet():
    """Die Rueckstellung bringt die Zone auf das Zeitprogramm; nach der Rueckkehr in den Betrieb bereitet der lokale Check
    sie neu vor, statt die Betriebsart als Eingriff von Hand zu werten."""
    rt, _ = _stub(OHNE)
    assert testphase.pruefen(rt) is True
    assert rt.zone_prepared is False
    offen, _ = _stub(OHNE, restore_ok=False)
    testphase.pruefen(offen)
    assert offen.zone_prepared is True  # nichts zurueckgestellt, die Zone steht noch auf Manuell
