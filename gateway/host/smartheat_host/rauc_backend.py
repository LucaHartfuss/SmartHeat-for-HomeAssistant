"""Custom-Bootloader-Backend fuer RAUC auf der Pi-Firmware (tryboot). Aufruf durch RAUC (system.conf
bootloader-custom-backend, [system] activate-installed=false): rauc-backend <Befehl> [Slot] [Zustand].

RAUC ruft bei rauc install nur set-state <Ziel> bad, bei mark-good und mark-bad nur set-state (RAUC 1.13,
install.c:1442-1443, mark.c:170,189); set-primary kommt mit activate-installed=false nie. Der Probestart selbst
(Neustart mit "0 tryboot") ist Sache des Updaters. Lage aus den Dateien (startauswahl.py): gestartet = DT-Datei,
primaer = [all] boot_partition; gelesen wird nur, was der Befehl braucht. Jeder Fehler (unlesbare Lage, Zustand ohne
Zeile in der Tabelle) ist Exit 1 ohne Schreiben."""
import sys
from pathlib import Path

from smartheat_host import startauswahl
from smartheat_host.startauswahl import StartauswahlFehler

SLOTS = ("A", "B")


def _slot(wert: str) -> str:
    if wert not in SLOTS:
        raise StartauswahlFehler(f"unbekannter Slot {wert!r}")
    return wert


def _set_state(slot: str, zustand: str, autoboot: Path, dt: Path) -> None:
    primaer = startauswahl.lese_primaer(autoboot)
    if zustand == "good":
        if slot == primaer:
            return  # idempotent (zweites mark-good nach einem Stromausfall)
        if slot != startauswahl.lese_gestartet(dt):
            raise StartauswahlFehler(f"{slot} ist weder gestartet noch primaer, nicht als good markierbar")
        startauswahl.schreibe_primaer(autoboot, slot)  # Probestart bestaetigt
    elif zustand == "bad":
        if slot != primaer:
            return  # Folge von rauc install (Ziel) und mark-bad im Probestart: [all] bleibt der alte Slot
        if slot != startauswahl.lese_gestartet(dt):
            raise StartauswahlFehler(f"{slot} ist primaer, aber nicht gestartet, nicht als bad markierbar")
        startauswahl.schreibe_primaer(autoboot, startauswahl.andere(slot))  # Rueckfall nach der Bestaetigung
    else:
        raise StartauswahlFehler(f"unbekannter Zustand {zustand!r}")


def _ausfuehren(argv: list[str], autoboot: Path, dt: Path) -> str | None:
    """Fuehrt den Befehl aus; gibt zurueck, was auf stdout soll (oder None)."""
    befehl, args = (argv[0], argv[1:]) if argv else ("", [])
    if befehl == "get-primary" and not args:
        return startauswahl.lese_primaer(autoboot)
    if befehl == "get-current" and not args:
        return startauswahl.lese_gestartet(dt)
    if befehl == "get-state" and len(args) == 1:
        return "good" if _slot(args[0]) == startauswahl.lese_primaer(autoboot) else "bad"
    if befehl == "set-state" and len(args) == 2:
        _set_state(_slot(args[0]), args[1], autoboot, dt)
        return None
    if befehl == "set-primary" and len(args) == 1:
        if _slot(args[0]) != startauswahl.lese_primaer(autoboot):
            raise StartauswahlFehler("set-primary auf einen anderen Slot ist nicht vorgesehen (activate-installed=false)")
        return None
    raise StartauswahlFehler(f"unbekannter Aufruf: {' '.join(argv)!r}")


def main(argv: list[str], *, autoboot: Path = startauswahl.AUTOBOOT, dt: Path = startauswahl.DT_BOOTLOADER) -> int:
    try:
        ausgabe = _ausfuehren(argv, autoboot, dt)
    except StartauswahlFehler as fehler:
        print(f"rauc-backend: {fehler}", file=sys.stderr)
        return 1
    if ausgabe is not None:
        print(ausgabe)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
