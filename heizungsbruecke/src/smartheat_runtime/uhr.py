"""Port der Uhr in der Laufzeit: ob der Zeitdienst des Systems die Uhr abgeglichen meldet.

Beide Hosts liefern die Antwort ueber den Geraete-Host (HA: Supervisor, Gateway: Status des Hosts). Fuer die Laufzeit gilt
nur ein echtes True als synchron; ein Fehler des Rueckrufs, eine Antwort anderer Art, eine fehlende oder zu spaete
Antwort gelten als nicht synchron. Das ist die sichere Richtung: ohne verlaessliche Uhr ruhen die zeitabhaengigen
Betriebsarten und das Soll ist der Wunsch.

Der Takt wartet nie laenger als die Zwischenspeicherzeit: die Abfrage laeuft in einem eigenen Thread, der Aufruf wartet
hoechstens `wartezeit_s` (nie mehr als `zwischenspeicher_s`) auf sie, eine haengende Abfrage wird nicht ein zweites Mal
gestartet. Eine Antwort gilt, solange sie juenger als die Zwischenspeicherzeit ist; danach wird neu gefragt, und bis dahin
gilt "nicht synchron"."""
import logging
import threading
from collections.abc import Callable
from datetime import datetime
from typing import NamedTuple

from smartheat_core import wallclock

#: so lange gilt eine Antwort, ohne den Rueckruf neu zu rufen
UHR_ZWISCHENSPEICHER_S = 30.0
#: so lange wartet ein Aufruf hoechstens auf die laufende Abfrage
UHR_WARTEZEIT_S = 0.5

logger = logging.getLogger(__name__)


class UhrAntwort(NamedTuple):
    """Gueltige Antwort des Zeitdienstes. Meldet er synchron, tragen `wand` und `mono` die Wanduhr und die monotone Uhr
    im Augenblick der Antwort (nicht des Aufrufs, die Antwort kann zwischengespeichert sein); sonst sind sie None."""

    synchron: bool
    wand: datetime | None
    mono: float | None


class UhrPort:
    def __init__(self, abfrage: Callable[[], object], *, clock: Callable[[], float],
                 zwischenspeicher_s: float = UHR_ZWISCHENSPEICHER_S, wartezeit_s: float = UHR_WARTEZEIT_S) -> None:
        self._abfrage, self._clock = abfrage, clock
        self._zwischenspeicher = zwischenspeicher_s
        self._wartezeit = min(wartezeit_s, zwischenspeicher_s)
        self._lock = threading.Lock()
        self._wert = False
        self._stand: float | None = None  # monotoner Zeitpunkt der letzten fertigen Antwort
        self._wand: datetime | None = None  # Wanduhr zum selben Zeitpunkt
        self._laeuft = False

    def uhr_synchron(self) -> bool:
        return self.antwort().synchron

    def antwort(self) -> UhrAntwort:
        """Wie `uhr_synchron`, mit den Uhrzeiten der Antwort."""
        with self._lock:
            if self._frisch():
                return self._lesen()
            if self._laeuft:
                return _KEINE
            self._laeuft = True
        fertig = threading.Event()
        threading.Thread(target=self._fragen, args=(fertig,), name="uhr-abfrage", daemon=True).start()
        fertig.wait(self._wartezeit)
        with self._lock:
            return self._lesen() if self._frisch() else _KEINE

    def _lesen(self) -> UhrAntwort:
        return UhrAntwort(True, self._wand, self._stand) if self._wert else _KEINE

    def _frisch(self) -> bool:
        return self._stand is not None and self._clock() - self._stand < self._zwischenspeicher

    def _fragen(self, fertig: threading.Event) -> None:
        wert = False
        try:
            wert = self._abfrage() is True
        except Exception as error:
            logger.warning("Uhrzeit des Hosts unbekannt (%s)", type(error).__name__)
        finally:
            with self._lock:
                self._wert, self._stand, self._wand, self._laeuft = wert, self._clock(), wallclock.now(), False
            fertig.set()


_KEINE = UhrAntwort(False, None, None)


class UhrVertrauen:
    """Ob die Wanduhr jetzt als Grundlage fuer Fristen gilt (`wallclock.vertrauenswuerdig`). Jeder Aufruf fragt den Zeitdienst
    (zwischengespeichert, `UhrPort.antwort`), fuehrt den Beleg zum Zeitpunkt der Antwort fort (nicht zum Zeitpunkt des
    Aufrufs: ein Wanduhrsprung innerhalb der Zwischenspeicherzeit wird sonst zum Beleg) und beurteilt die Wanduhr gegen
    die monotone Uhr. Nur im Worker-Thread aufrufen (der Beleg ist nicht gesperrt)."""

    def __init__(self, antwort: Callable[[], UhrAntwort], *, clock: Callable[[], float]) -> None:
        self._antwort, self._clock = antwort, clock
        self._beleg: wallclock.UhrBeleg | None = None

    def __call__(self) -> bool:
        wand, mono = wallclock.now(), self._clock()
        antwort = self._antwort()
        if antwort.synchron and antwort.wand is not None and antwort.mono is not None:
            self._beleg = wallclock.beleg_fortschreiben(self._beleg, synchron=True, wand_jetzt=antwort.wand,
                                                        mono_jetzt=antwort.mono)
        return wallclock.vertrauenswuerdig(self._beleg, wand_jetzt=wand, mono_jetzt=mono)
