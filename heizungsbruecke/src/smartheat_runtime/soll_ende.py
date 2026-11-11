"""Soll-Ende im Ruhezustand: nach dem Ende der Regelung (oder beim Abmelden) soll die Soll-Quelle den gemerkten Wunsch
zeigen. Im Ruhezustand laeuft kein SollEingang; diese Funktion arbeitet den persistierten Merker `soll_ende_offen` ab
und loescht ihn erst nach Erfolg (`soll_eingang.ende_schreiben`). Nur im Worker-Thread des Ruhezustands."""
import logging
from collections.abc import Callable
from dataclasses import dataclass

from smartheat_runtime.soll_eingang import SollQuelle, ende_schreiben
from smartheat_runtime.state import StorageError
from smartheat_runtime.worker import Event, RegulationWorker

logger = logging.getLogger(__name__)

EV_RUHE_SOLL_ENDE = "shg_ruhe_soll_ende"


@dataclass(frozen=True)
class SollEnde:
    """Was ein Host fuers Soll-Ende liefert: den gemerkten Wunsch und die Quelle, an die er geht."""
    wunsch: float
    quelle: SollQuelle


def merker_setzen(store, offen: bool) -> None:
    """Setzt den Merker `soll_ende_offen`. Scheitert nur das Schreiben von backup.json, gilt der Wert im Speicher weiter
    (der Takt der Gesundheitspruefung holt das Schreiben nach); der Fehler wird geloggt statt den Aufrufer abzubrechen."""
    try:
        store.update(soll_ende_offen=offen)
    except StorageError:
        logger.exception("Merker 'Soll-Ende offen' konnte nicht gespeichert werden")


def ruhezustand_abarbeiten(worker: RegulationWorker, store, soll_ende: Callable[[], SollEnde | None],
                           wiederholung_s: float) -> None:
    """Arbeitet einen offenen Merker `soll_ende_offen` im Worker des Ruhezustands ab: sofort ein Versuch, bei
    unbekannter Anzeige oder gescheitertem Schreiben im Takt `wiederholung_s` erneut. Ohne Wunsch oder Quelle (Host liefert
    None) ist nichts zu schreiben und der Merker faellt. Ein Fehler der Quelle wird wie ein gescheiterter Versuch
    behandelt. Ohne offenen Merker passiert nichts."""
    if not store.state.soll_ende_offen:
        return

    def _versuch(event: Event | None = None) -> None:
        if not store.state.soll_ende_offen:
            return
        try:
            ziel = soll_ende()
            if ziel is None:
                logger.info("Soll-Ende: kein gemerkter Wunsch oder keine schreibbare Soll-Quelle, nichts zu schreiben")
                erledigt = True
            else:
                erledigt = ende_schreiben(ziel.wunsch, ziel.quelle)
        except Exception:
            logger.exception("Soll-Ende gescheitert - erneuter Versuch in %s s", wiederholung_s)
            erledigt = False
        if erledigt:
            merker_setzen(store, False)
            return
        worker.schedule(wiederholung_s, Event(EV_RUHE_SOLL_ENDE))

    worker.register(EV_RUHE_SOLL_ENDE, _versuch)
    _versuch()
