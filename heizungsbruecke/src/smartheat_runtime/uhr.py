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

#: so lange gilt eine Antwort, ohne den Rueckruf neu zu rufen
UHR_ZWISCHENSPEICHER_S = 30.0
#: so lange wartet ein Aufruf hoechstens auf die laufende Abfrage
UHR_WARTEZEIT_S = 0.5

logger = logging.getLogger(__name__)


class UhrPort:
    def __init__(self, abfrage: Callable[[], object], *, clock: Callable[[], float],
                 zwischenspeicher_s: float = UHR_ZWISCHENSPEICHER_S, wartezeit_s: float = UHR_WARTEZEIT_S) -> None:
        self._abfrage, self._clock = abfrage, clock
        self._zwischenspeicher = zwischenspeicher_s
        self._wartezeit = min(wartezeit_s, zwischenspeicher_s)
        self._lock = threading.Lock()
        self._wert = False
        self._stand: float | None = None  # Zeitpunkt der letzten fertigen Antwort
        self._laeuft = False

    def uhr_synchron(self) -> bool:
        with self._lock:
            if self._frisch():
                return self._wert
            if self._laeuft:
                return False
            self._laeuft = True
        fertig = threading.Event()
        threading.Thread(target=self._fragen, args=(fertig,), name="uhr-abfrage", daemon=True).start()
        fertig.wait(self._wartezeit)
        with self._lock:
            return self._wert if self._frisch() else False

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
                self._wert, self._stand, self._laeuft = wert, self._clock(), False
            fertig.set()
