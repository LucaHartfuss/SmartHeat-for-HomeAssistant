"""Der Watchdog treibt die Uhr des Kaelteschutzes: ein Fuehler, der einen konstanten Wert meldet, loest in HA kein
Ereignis aus; ohne den Watchdog-Takt liefe die Frostnaehe-Uhr (15 Minuten) und die Soll-Uhr (3 Stunden) nie ab. Volle
Laufzeit mit echtem Worker und Fake-Trigger-Client (`env`), die Wanduhr der Entscheidung steht im Gleichschritt mit der
monotonen Uhr der Laufzeit."""
import pytest
from runtime_helpers import _advance, _quiet_backup, _start, _trigger
from test_runtime_kaelteschutz import MINUTE, STUNDE, wall  # noqa: F401 - Fixture

TAKT = 5 * MINUTE  # local_check_interval der Testumgebung


def _takte(env, bridge, wall, anzahl):  # noqa: F811
    for _ in range(anzahl):
        wall.advance(TAKT)
        _advance(env, bridge, TAKT)


def _gestartet(env, wall, ist):  # noqa: F811
    """Laufzeit gestartet, der Fuehler meldet einmal `ist` (dieses Ereignis ist der erste Check, der die Zaehlung
    beginnt); danach aendert sich nichts mehr."""
    _quiet_backup(env)
    bridge = _start(env)
    env.ha.states["sensor.room_actual"] = ist
    _trigger(env, bridge, "sensor.room_actual")
    return bridge


def _art(bridge):
    return bridge.store.state.kaelteschutz_art


def test_ein_konstanter_frostwert_startet_den_kaelteschutz_nach_fuenfzehn_minuten_bei_verbundenem_trigger(env, wall):  # noqa: F811
    bridge = _gestartet(env, wall, 4.0)
    assert env.trigger_clients[-1].connected

    _takte(env, bridge, wall, 2)  # 10 Minuten
    assert _art(bridge) is None
    assert bridge.store.state.kaelteschutz_zaehlung.frost_seit

    _takte(env, bridge, wall, 1)  # 15 Minuten
    assert _art(bridge) == "frost"
    assert bridge.store.state.emergency_boost_active


def test_ein_konstanter_soll_abstand_startet_den_kaelteschutz_nach_drei_stunden_bei_verbundenem_trigger(env, wall):  # noqa: F811
    bridge = _gestartet(env, wall, 18.0)  # 3 K unter dem Soll 21,0, ueber der Frostnaehe

    _takte(env, bridge, wall, 3 * STUNDE // TAKT - 1)  # 2:55
    assert _art(bridge) is None
    assert bridge.store.state.kaelteschutz_zaehlung.soll_seit is not None

    _takte(env, bridge, wall, 1)  # 3:00
    assert _art(bridge) == "soll"


def test_ein_laufender_lauf_wird_vom_watchdog_bei_verbundenem_trigger_beendet(env, wall):  # noqa: F811
    """Ein aktiver Lauf haelt den Watchdog-Check an: die Hoechstdauer von vier Stunden endet ohne neues Ereignis."""
    bridge = _gestartet(env, wall, 4.0)
    _takte(env, bridge, wall, 3)
    assert _art(bridge) == "frost"

    _takte(env, bridge, wall, 4 * STUNDE // TAKT)

    assert _art(bridge) is None


@pytest.mark.parametrize("ist", [21.0, 7.0])
def test_ohne_zaehler_und_ohne_lauf_loest_der_watchdog_bei_verbundenem_trigger_keinen_check_aus(env, wall, ist):  # noqa: F811
    """Regression: wie bisher passiert bei verbundenem Trigger ohne Kaelteschutz nichts (ein neuer Soll in der Entity
    wird erst mit dem Ereignis gelesen). Die Zaehlung ist leer: Ankunft bzw. Ist ueber der Frostnaehe."""
    bridge = _gestartet(env, wall, ist)
    assert not bridge.store.state.kaelteschutz_zaehlung.frost_seit
    env.ha.states["sensor.room_target"] = 20.5

    _takte(env, bridge, wall, 3)

    assert bridge.store.state.stable_target == 21.0


def test_bei_getrenntem_trigger_laeuft_der_check_wie_bisher_in_jedem_takt(env, wall):  # noqa: F811
    bridge = _gestartet(env, wall, 21.0)
    env.trigger_clients[-1].connected = False
    env.ha.states["sensor.room_target"] = 20.5

    _takte(env, bridge, wall, 1)

    assert bridge.store.state.stable_target == 20.5
