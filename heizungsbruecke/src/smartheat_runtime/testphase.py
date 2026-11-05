"""Testphase ohne Setwell (Kreiszustand `testphase_ohne`): beim Eintritt stellt die Pipeline die Hebel einmal auf die
gemerkten Ursprungswerte, danach schreibt Setwell nichts mehr an die Hebel. Telemetrie, Snapshots, lokale Grenzen und der
Kaelteschutz laufen weiter. Der Zustand wird bei jedem lokalen Check frisch gelesen; ein Wechsel braucht keinen Neustart."""
import logging

from smartheat_runtime.runtime import Runtime

logger = logging.getLogger(__name__)

#: Wert von `kreise[n].zustand` fuer die Testphase ohne Setwell.
TESTPHASE_OHNE = "testphase_ohne"


def aktiv(rt: Runtime) -> bool:
    """Steht der Kreis jetzt in der Testphase ohne Setwell? Liest den Zustand bei jedem Aufruf frisch."""
    return rt.config.zustand_source() == TESTPHASE_OHNE


def pruefen(rt: Runtime) -> bool:
    """Fuehrt den Eintritt in die Testphase nach und gibt zurueck, ob der Kreis in Ruhe ist (Testphase und Rueckstellung
    erledigt). Beim Eintritt wird einmal zurueckgestellt; scheitert das, bleibt die Rueckstellung offen und der naechste
    Check versucht es erneut. Ein Wechsel zurueck nach `in_betrieb` setzt den Merker zurueck, damit eine weitere Phase
    wieder zurueckstellt. Nach einem Abo-Ende gibt es nichts zurueckzustellen."""
    if rt.store.state.abo_finished:
        return False
    if not aktiv(rt):
        rt.testphase_rueckgestellt = False
        return False
    if not rt.testphase_rueckgestellt:
        if rt.override.restore_for_pause():
            rt.testphase_rueckgestellt = True
            logger.info("Testphase ohne Setwell: Hebel auf die Ursprungswerte zurueckgestellt")
        else:
            logger.info("Testphase ohne Setwell: Rueckstellung offen, naechster Check versucht es erneut")
    return rt.testphase_rueckgestellt
