"""FakeStartauswahl: die Startauswahl des Gateways im Speicher, fuer die Tests des Updaters (Task 3). Lage wie beim
echten Geraet: [all] = primaer, tryboot nur fuer einen Start; neustart() spielt die Firmware nach. Das Fake kennt weder
Dateien noch Prozesse; die echte Folge (Backend, autoboot.txt) prueft test_rauc_backend.py."""
from pathlib import Path

from smartheat_host.startauswahl import StartauswahlFehler


def andere(slot: str) -> str:
    return "B" if slot == "A" else "A"


class FakeStartauswahl:
    def __init__(self, gestartet: str = "A", primaer: str | None = None, compatible: str = "smartheat-gateway-pi5") -> None:
        self._gestartet = gestartet
        self._primaer = primaer or gestartet
        self._compatible = compatible
        self.tryboot_vorgemerkt = False  # Neustart mit "0 tryboot" angefordert, noch nicht ausgefuehrt
        self.neustart_angefordert = False  # irgendein Neustart angefordert (mit oder ohne tryboot)
        self.aufrufe: list[tuple] = []
        self.installieren_fehler: StartauswahlFehler | None = None  # wird beim naechsten installieren() ausgeloest
        self.fehler: dict[str, StartauswahlFehler] = {}  # Methodenname -> Fehler, den der Aufruf ausloest

    def _ruf(self, name: str, *args) -> None:
        self.aufrufe.append((name, *args))
        if name in self.fehler:
            raise self.fehler[name]

    def gestartet(self) -> str:
        return self._gestartet

    def primaer(self) -> str:
        return self._primaer

    def im_probestart(self) -> bool:
        return self._gestartet != self._primaer

    def compatible(self) -> str:
        return self._compatible

    def installieren(self, bundle: Path) -> None:
        self._ruf("installieren", bundle)
        if self.installieren_fehler is not None:
            fehler, self.installieren_fehler = self.installieren_fehler, None
            raise fehler

    def probestart(self) -> None:
        self._ruf("probestart")
        self.tryboot_vorgemerkt = True
        self.neustart_angefordert = True

    def bestaetigen(self) -> None:
        self._ruf("bestaetigen")
        if self._gestartet != self._primaer:
            self._primaer = self._gestartet

    def zurueckschalten(self) -> None:
        self._ruf("zurueckschalten")
        if self._gestartet == self._primaer:  # bestaetigt und gestartet: Rueckfall auf den anderen Slot
            self._primaer = andere(self._primaer)
        self.tryboot_vorgemerkt = False
        self.neustart_angefordert = True

    def normal_neustarten(self) -> None:
        self._ruf("normal_neustarten")
        self.tryboot_vorgemerkt = False
        self.neustart_angefordert = True

    def neustart(self) -> None:
        """Die Firmware: mit vorgemerktem tryboot startet [tryboot] (der andere Slot), einmalig; sonst [all]."""
        self._gestartet = andere(self._primaer) if self.tryboot_vorgemerkt else self._primaer
        self.tryboot_vorgemerkt = False
        self.neustart_angefordert = False
