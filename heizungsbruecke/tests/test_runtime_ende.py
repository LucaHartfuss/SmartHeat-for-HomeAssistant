"""Das Ende der Regelung (Abo-Frist abgelaufen) im HA-Host: es ueberlebt den Neustart, auch bei unsicherer Uhr, und das
Soll-Ende (der gemerkte Wunsch an die Soll-Quelle) geht nicht verloren. Die Uhr kommt wie produktiv vom Supervisor
(FakeGeraet.zeitdienst_setzen), die Wanduhr ist eine feste Attrappe."""
import json
from datetime import datetime, timedelta

import pytest
from fakes import WallClock
from runtime_helpers import _backup, _links, _quiet_backup, _start_bridge

from smartheat_runtime import app, entitlement

START = datetime(2026, 1, 15, 8, 0, tzinfo=WallClock.ZONE)


@pytest.fixture
def wall(monkeypatch):
    wall = WallClock(START)
    monkeypatch.setattr("smartheat_core.wallclock._now", wall.now)
    return wall


def _abo_seit(env, wall, tage: int = 31) -> None:
    """Das Abo ist seit `tage` Tagen inaktiv (Wanduhr der Attrappe, die echte Fristpruefung)."""
    _quiet_backup(env)
    env.abo["status"] = entitlement.INACTIVE
    entitlement.mark_inactive(env.paths["ENTITLEMENT_PATH"], wall.now() - timedelta(days=tage))


def test_nach_dem_ende_bleibt_die_regelung_auch_bei_unsicherer_uhr_im_ruhezustand(env, wall):
    _abo_seit(env, wall)
    bridge = _start_bridge(env)
    assert isinstance(bridge, app.IdleBridge) and bridge.reason == "abo_beendet"
    assert entitlement.ist_beendet(env.paths["ENTITLEMENT_PATH"])
    env.ha.writes.clear()
    env.ha.pushes.clear()

    neu = _start_bridge(env, zeit_synchron=False)  # Neustart vor dem Zeitabgleich (Pi ohne Echtzeituhr)

    assert isinstance(neu, app.IdleBridge) and neu.reason == "abo_beendet"
    assert env.ha.writes == [] and env.ha.pushes == []  # kein Hebel-, Zonen- oder Soll-Schreiben
    assert _links(env) == []  # kein Link zum Server


def test_das_erste_ende_bei_unsicherer_uhr_bleibt_offen(env, wall):
    _abo_seit(env, wall)
    bridge = _start_bridge(env, zeit_synchron=False)
    assert not isinstance(bridge, app.IdleBridge)  # eine unsichere Uhr beendet nie: Verlaengerung ist die sichere Richtung
    assert not entitlement.ist_beendet(env.paths["ENTITLEMENT_PATH"])


# --- Soll-Ende: die Soll-Entity zeigt nach dem Ende den gemerkten Wunsch ---------------------------------------------

CLIMATE = "climate.wz"
CLIMATE_SOLL = f"{CLIMATE}::temperature"
WUNSCH = 21.0


def _climate_welt(env, wall, *, angezeigt) -> None:
    """Eine Climate-Entity als Soll-Quelle, der gemerkte Wunsch 21,0 und ein Abo, das seit 31 Tagen inaktiv ist."""
    env.ha.states.update({CLIMATE: "heat", CLIMATE_SOLL: angezeigt})
    env.paths["ROOM_TARGET_PATH"].parent.mkdir(parents=True, exist_ok=True)
    env.paths["ROOM_TARGET_PATH"].write_text(json.dumps({"value": WUNSCH, "source": "portal", "ts": "x"}))
    _abo_seit(env, wall)


def _climate_start(env, **kwargs):
    return _start_bridge(env, entity_room_target=CLIMATE_SOLL, **kwargs)


def _soll_schreiben(env):
    return [w for w in env.ha.writes if w[0] == CLIMATE_SOLL]


def _unbekannt() -> Exception:
    return RuntimeError("Entity nicht erreichbar")


def test_das_erste_ende_merkt_das_soll_ende_und_schreibt_den_wunsch_sofort(env, wall):
    _climate_welt(env, wall, angezeigt=19.0)

    bridge = _climate_start(env)

    assert isinstance(bridge, app.IdleBridge) and bridge.reason == "abo_beendet"
    assert _soll_schreiben(env) == [(CLIMATE_SOLL, WUNSCH)]  # die Soll-Quelle zeigt nie weniger als den Wunsch
    assert _backup(env).get("soll_ende_offen") is not True  # nach dem Erfolg ist der Merker weg


def test_das_soll_ende_uebersteht_den_neustart_auch_bei_unsicherer_uhr(env, wall):
    _climate_welt(env, wall, angezeigt=_unbekannt())  # die Anzeige ist beim ersten Ende nicht zu lesen
    _climate_start(env)
    assert _backup(env)["soll_ende_offen"] is True and _soll_schreiben(env) == []
    env.ha.states[CLIMATE_SOLL] = 19.0  # das Thermostat zeigt weniger als der Wunsch

    neu = _climate_start(env, zeit_synchron=False)

    assert isinstance(neu, app.IdleBridge) and neu.reason == "abo_beendet"
    assert _soll_schreiben(env) == [(CLIMATE_SOLL, WUNSCH)]
    assert _backup(env).get("soll_ende_offen") is not True


def test_das_soll_ende_bleibt_offen_solange_die_anzeige_unbekannt_ist_und_wird_im_takt_nachgeholt(env, wall):
    _climate_welt(env, wall, angezeigt=_unbekannt())
    _climate_start(env)
    neu = _climate_start(env, zeit_synchron=False)
    assert _soll_schreiben(env) == [] and _backup(env)["soll_ende_offen"] is True
    env.ha.states[CLIMATE_SOLL] = 18.0

    env.clock.advance(3600)  # weit ueber dem Wiederholtakt des Ruhezustands
    neu.worker.run_pending()

    assert _soll_schreiben(env) == [(CLIMATE_SOLL, WUNSCH)]
    assert _backup(env).get("soll_ende_offen") is not True


@pytest.mark.parametrize("angezeigt", [WUNSCH, 22.5, 7.0, 5.0], ids=["gleich", "mehr", "frostschutz", "darunter"])
def test_das_soll_ende_senkt_nie_und_stoert_den_frostschutz_nicht(env, wall, angezeigt):
    _climate_welt(env, wall, angezeigt=_unbekannt())
    _climate_start(env)
    env.ha.states[CLIMATE_SOLL] = angezeigt

    _climate_start(env, zeit_synchron=False)

    assert _soll_schreiben(env) == []
    assert _backup(env).get("soll_ende_offen") is not True  # erledigt: nichts zu tun


def test_ohne_climate_entity_oder_wunsch_faellt_der_merker_ohne_schreiben(env, wall):
    _abo_seit(env, wall)  # Soll-Entity ist ein Sensor (nicht schreibbar), kein gemerkter Wunsch
    _start_bridge(env)
    assert env.ha.writes == [] or all(w[0] != "sensor.room_target" for w in env.ha.writes)
    assert _backup(env).get("soll_ende_offen") is not True


# --- Abmelden: auch hier zeigt die Soll-Quelle danach den gemerkten Wunsch ----------------------------------------------

def _climate_abgemeldet(env, *, angezeigt, **kwargs):
    env.ha.states.update({CLIMATE: "heat", CLIMATE_SOLL: angezeigt})
    env.paths["ROOM_TARGET_PATH"].parent.mkdir(parents=True, exist_ok=True)
    env.paths["ROOM_TARGET_PATH"].write_text(json.dumps({"value": WUNSCH, "source": "portal", "ts": "x"}))
    _quiet_backup(env)
    return _climate_start(env, abgemeldet=True, **kwargs)


def test_abmelden_zeigt_an_der_soll_quelle_den_gemerkten_wunsch(env):
    bridge = _climate_abgemeldet(env, angezeigt=19.0)
    assert isinstance(bridge, app.IdleBridge) and bridge.reason == "abgemeldet"
    assert _soll_schreiben(env) == [(CLIMATE_SOLL, WUNSCH)]
    assert _backup(env).get("soll_ende_offen") is not True


def test_abmelden_mit_unbekannter_anzeige_bleibt_offen_und_holt_es_im_takt_nach(env):
    bridge = _climate_abgemeldet(env, angezeigt=_unbekannt())
    assert _soll_schreiben(env) == [] and _backup(env)["soll_ende_offen"] is True
    env.ha.states[CLIMATE_SOLL] = 18.0

    env.clock.advance(3600)
    bridge.worker.run_pending()

    assert _soll_schreiben(env) == [(CLIMATE_SOLL, WUNSCH)]
    assert _backup(env).get("soll_ende_offen") is not True


def test_abmelden_senkt_nie(env):
    _climate_abgemeldet(env, angezeigt=22.5)
    assert _soll_schreiben(env) == []
