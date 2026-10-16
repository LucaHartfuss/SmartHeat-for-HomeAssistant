"""Lebenszyklus des Geraeteprozesses (Audit 5 A5-26; Spec Stufe 3 S6 4.2/4.3), fuer beide Hosts gleich: Geraete-Thread
und Laufzeit bis zum Ende des Prozesses, ein Weg fuer jeden Neustart, sauberes Trennen vor jedem Ende, Aufraeumen beim
Start. Der Host liefert nur, wie er die Laufzeit startet und wie er neu startet: HA per exec (der Supervisor startet ein
sauber beendetes Add-on nicht zuverlaessig neu), das Gateway mit Exit 0 (Compose startet neu).

- Neustart (EV_RELOAD aus dem Geraete-Host; neustart_der_laufzeit, der Rueckruf, den beide Hosts app.start uebergeben:
  Abo am Fristende wieder aktiv, Neupruefung im Konfigurationsfehler, Neustart nach dem Zuruecksetzen): der Worker endet
  mit Neustart; beenden trennt zuerst den Link (MQTT-DISCONNECT; sonst meldet IoT Core DUPLICATE_CLIENTID, wenn der
  neue Prozess verbindet), dann der Neustart des Hosts. Globales wird dafuer nicht ueberschrieben (Spec 4.2).
- Unerwarteter Fehler im Start oder im Worker: Fehlerart ins Log, beenden, FEHLER_PAUSE_SECONDS Pause, dann derselbe
  Neustart (keine schnelle Schleife gegen IoT Core; bis Stufe 3 S6 hatte das Gateway keine Pause). Vor der Pause ruft
  laufen den optionalen Rueckruf vor_pause des Hosts: das Gateway meldet sich darin ungesund, damit der Updater ein
  Update, dessen Laufzeit gleich nach dem Start wirft, weiter zurueckrollt (Abschlussreview S6 I-2); HA hat keinen.
- SIGTERM (SystemExit): beenden, kein Neustart; auch waehrend der Pause."""
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

from smartheat_device.geraet_host_basis import EV_RELOAD
from smartheat_runtime import app
from smartheat_runtime import aufraeumen as markierung
from smartheat_runtime.runtime import Runtime

logger = logging.getLogger(__name__)

#: Ende des Prozesses: so lange darf der Geraete-Thread sein laufendes Ereignis noch abschliessen.
JOIN_SECONDS = 10.0
#: Unerwarteter Fehler: Pause vor dem Neustart, damit ein dauerhafter Fehler keine schnelle Schleife wird.
FEHLER_PAUSE_SECONDS = 60.0


class Neustart(BaseException):
    """Aus dem Regel-Worker: Prozess nach sauberem Trennen neu starten. BaseException wie SystemExit: der Worker faengt
    in Handlern nur Exception ab, diese hier geht bis laufen() durch."""


def _neustart(anlass: str) -> None:
    # "Laufzeit startet neu": tools/e2e/drive_gateway2.py (ankuendigungen) zaehlt die Zeile als angekuendigten Neustart
    logger.info("%s, Laufzeit startet neu", anlass)
    raise Neustart()


def neustart_der_laufzeit() -> None:
    """Der Rueckruf neustart fuer app.start (Spec Stufe 3 S6 4.2), beide Hosts: Abo am Fristende wieder aktiv,
    Neupruefung im Ruhezustand Konfigurationsfehler, Neustart nach dem Zuruecksetzen. Wirft Neustart; laufen trennt dann
    sauber und ruft den Neustart des Hosts. Den Anlass loggt die Laufzeit an der Aufrufstelle (abo.check_grace_end,
    app._idle, app._nach_zuruecksetzen) unmittelbar davor."""
    _neustart("Neustart der Laufzeit")


def reload_anhaengen(result) -> None:
    """Haengt EV_RELOAD aus dem Geraete-Host (neue laufzeitrelevante Konfiguration, sign_off) an den Worker: Neustart
    mit sauberem Trennen. Die Neustarts der Laufzeit selbst gehen ueber den Rueckruf neustart_der_laufzeit
    (app.start); in den Ruhezustaenden abgemeldet und abo_beendet bleibt EV_RECHECK das erneute Zuruecksetzen der
    Laufzeit."""
    result.worker.register(EV_RELOAD, lambda event: _neustart("Neue Konfiguration oder Abmelden"))


def _schritt(name: str, aufruf: Callable[[], object]) -> None:
    try:
        aufruf()
    except Exception as error:  # das Ende darf nie scheitern: sonst entfiele der Neustart
        logger.warning("Beenden: %s gescheitert (%s)", name, type(error).__name__)


def _warten(thread: threading.Thread, join_seconds: float) -> None:
    thread.join(join_seconds)
    if thread.is_alive():
        logger.warning("Geraete-Thread nach %.0f s nicht beendet, Prozess endet trotzdem", join_seconds)


def beenden(device, thread: threading.Thread | None, hintergrund, *, join_seconds: float = JOIN_SECONDS) -> None:
    """Ende des Prozesses (Neustart, SIGTERM, Fehler): erst den Geraetekern stoppen (sauberes Trennen) und wartende
    Host-Aufrufe der Befehle abbrechen, dann den Geraete-Thread sein laufendes Ereignis abschliessen lassen (z. B.
    software_soll nach konfiguration_uebernommen). Danach noch einmal stop(): Device.run fuehrt nach stop() noch einen
    Takt aus, und ein Port-Rueckfall oder eine Rettung darin koennte einen neuen Link geoeffnet haben (5b-D2-4). Wirft
    nie: jeder Schritt einzeln, ins Log nur die Fehlerart."""
    _schritt("Geraetekern stoppen", device.stop)
    if hintergrund is not None:
        _schritt("Hintergrund stoppen", hintergrund.stop)
    if thread is not None:
        _schritt("Geraete-Thread abwarten", lambda: _warten(thread, join_seconds))
    _schritt("Geraetekern stoppen", device.stop)


def laufen(device, hintergrund, starten: Callable[[], Runtime | app.IdleBridge], neustart: Callable[[], None], *,
           vor_pause: Callable[[], None] | None = None, sleep: Callable[[float], None] | None = None) -> None:
    """Geraete-Thread und Laufzeit bis zum Ende des Prozesses. starten() startet die Laufzeit (Runtime oder IdleBridge,
    mit dem Rueckruf neustart_der_laufzeit und EV_RELOAD umgeleitet) und bindet den Worker; neustart() ist der Neustart
    des Hosts; vor_pause() laeuft nach einem unerwarteten Fehler nach beenden und vor der Pause (darf scheitern, Pause
    und Neustart bleiben). Ein unerwarteter Fehler beendet den Prozess nie still."""
    thread: threading.Thread | None = None
    neu = fehler = False
    try:
        thread = device.start()
        result = starten()
        logger.info("Laufzeit gestartet (%s)", type(result).__name__)
        result.worker.run()
    except Neustart:
        neu = True
    except Exception as error:
        logger.error("Unerwarteter Fehler der Laufzeit (%s), Neustart in %.0f s", type(error).__name__,
                     FEHLER_PAUSE_SECONDS)
        logger.debug("Fehler der Laufzeit", exc_info=True)
        neu = fehler = True
    finally:
        beenden(device, thread, hintergrund)
    if fehler:
        if vor_pause is not None:
            try:
                vor_pause()
            except Exception as error:  # Pause und Neustart duerfen nie entfallen
                logger.warning("Vor der Pause: Rueckruf des Hosts gescheitert (%s)", type(error).__name__)
        (sleep or time.sleep)(FEHLER_PAUSE_SECONDS)  # zur Laufzeit gelesen: Tests ersetzen time.sleep
    if neu:
        logger.info("Neustart nach sauberem Trennen")
        neustart()


def aufraeumen_beim_start(pfad: Path, gespeichert: Callable[[], object], loeschen: Callable[[], bool], *,
                          wer: str) -> None:
    """Markierung aufraeumen.json vor dem Start des Geraetekerns auswerten (Spec Stufe 3 S3 Abschnitt 4), beide Hosts:
    wieder eingerichtet -> Markierung weg, Zustand bleibt; Zuruecksetzen steht aus -> nichts loeschen (die Laufzeit
    setzt mit der Konfiguration der Markierung zurueck und startet danach neu); Markierung unlesbar (EIO, EACCES) ->
    nichts loeschen, der Start geht weiter, der naechste versucht es erneut (S3S3-Marker); sonst loescht der Host den
    anlagengebundenen Zustand, und die Markierung geht erst, wenn er fertig meldet. gespeichert() liest die
    gespeicherte Konfiguration nur, wenn es eine Markierung gibt."""
    try:
        if not pfad.exists():
            return
    except OSError as error:  # Path.exists wirft bei EIO/EACCES im stat: Markierung bleibt, nichts wird geloescht
        logger.warning("%s nicht pruefbar (%s): Markierung bleibt, nichts wird geloescht", pfad.name,
                       type(error).__name__)
        return
    entscheidung = markierung.entscheiden(pfad, gespeichert())
    if entscheidung == markierung.KEINE:  # zwischendurch verschwunden
        return
    if entscheidung == markierung.BEHALTEN:  # unlesbar: nichts loeschen, der naechste Start versucht es erneut
        return
    if entscheidung == markierung.VERWERFEN:
        logger.info("Aufraeumen entfaellt: %s ist inzwischen neu eingerichtet", wer)
        pfad.unlink(missing_ok=True)
        return
    if entscheidung == markierung.ZURUECKSETZEN:
        logger.info("Anlage wird vor dem Aufraeumen zurueckgesetzt")
        return
    if loeschen():
        pfad.unlink(missing_ok=True)
