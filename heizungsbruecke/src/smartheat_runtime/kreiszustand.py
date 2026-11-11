"""Kreis ausser Betrieb (Kreiszustand `ausser_betrieb`): beim Eintritt stellt die Pipeline die Hebel einmal auf die
gemerkten Ursprungswerte und leert die gemerkten Werte (der Kreis lernt bei der Rueckkehr neu ein), danach schreibt das
Geraet fuer den Kreis nur noch den Kaelteschutz (nur mit Frostnaehe-Ausloeser) und sendet nur Raumwerte. Die
Rueckstellung ist kein Abo-Ende: der Kreis kehrt ohne Prozessneustart zurueck. Der Zustand wird bei jedem Aufruf frisch
gelesen; nach `abo_finished` des ganzen Geraets gilt nichts mehr."""
import logging

from smartheat_runtime.runtime import Runtime
from smartheat_runtime.soll_eingang import EV_SOLL_ENDE
from smartheat_runtime.worker import Event

logger = logging.getLogger(__name__)

#: Wert von `kreise[n].zustand` fuer einen Kreis ausser Betrieb.
AUSSER_BETRIEB = "ausser_betrieb"


def ausser_betrieb(rt: Runtime) -> bool:
    """Steht der Kreis jetzt ausser Betrieb? Liest den Zustand bei jedem Aufruf frisch."""
    return rt.config.zustand_source() == AUSSER_BETRIEB


def pruefen(rt: Runtime) -> bool:
    """Fuehrt den Eintritt nach und gibt zurueck, ob der Kreis ruht (ausser Betrieb und Rueckstellung erledigt). Beim
    Eintritt wird einmal zurueckgestellt, auch der Comfort-Boost endet; scheitert das, bleibt die Rueckstellung offen und
    der naechste Check versucht es erneut. Ein laufender Kaelteschutz schiebt sie bis zu seinem Ende auf (er schuetzt
    weiter). Der Merker steht in backup.json: ein Neustart mitten im Zustand wiederholt die Rueckstellung nicht und
    ueberschreibt keine Handaenderung. Ein Wechsel zurueck nach `in_betrieb` setzt den Merker zurueck, damit ein
    weiterer Eintritt wieder zurueckstellt. Nach `abo_finished` des ganzen Geraets gibt es nichts zurueckzustellen."""
    state = rt.store.state
    if state.abo_finished:
        return False
    if not ausser_betrieb(rt):
        if state.ausser_betrieb_rueckgestellt:
            rt.store.update(ausser_betrieb_rueckgestellt=False)
        return False
    if not state.ausser_betrieb_rueckgestellt and state.kaelteschutz_art is None:
        if rt.override.restore_and_clear(True):
            rt.zone_prepared = False  # die Zone steht wieder auf dem Zeitprogramm: die Rueckkehr bereitet sie neu vor
            rt.store.update(ausser_betrieb_rueckgestellt=True)
            rt.worker.post(Event(EV_SOLL_ENDE))  # die Soll-Quelle zeigt den gemerkten Wunsch, nie weniger
            logger.info("Kreis ausser Betrieb: Hebel auf die Ursprungswerte zurueckgestellt")
        else:
            logger.info("Kreis ausser Betrieb: Rueckstellung offen, naechster Check versucht es erneut")
    return rt.store.state.ausser_betrieb_rueckgestellt
