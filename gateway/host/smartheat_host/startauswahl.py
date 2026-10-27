"""Startauswahl des Gateways (Raspberry-Pi-Firmware tryboot, Slots A = Partition 2, B = Partition 3).

Lage aus zwei Quellen, nie aus dem DT-Merker tryboot (der bleibt nach mark-good bis zum naechsten Start 1):
gestartet = /proc/device-tree/chosen/bootloader/partition, primaer = [all] boot_partition in /bootfs/autoboot.txt.
Probestart <=> gestartet != primaer. Die Datei autoboot.txt hat immer genau die kanonische Form (autoboot_text);
sie wird nur ueber schreibe_primaer geaendert. Das RAUC-Backend (rauc_backend.py) und der Updater benutzen
dieselben Funktionen und Klassen."""
import contextlib
import logging
import os
import shlex
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

AUTOBOOT = Path("/bootfs/autoboot.txt")
DT_BOOTLOADER = Path("/proc/device-tree/chosen/bootloader")
SLOT_VON_PARTITION = {2: "A", 3: "B"}
PARTITION_VON_SLOT = {slot: partition for partition, slot in SLOT_VON_PARTITION.items()}
FRIST_SEKUNDEN = 60.0  # jeder Aufruf ausser rauc install
INSTALL_FRIST_SEKUNDEN = 1800.0
IDLE_WARTEN_SEKUNDEN = 1800.0  # nach abgelaufener Install-Frist: so lange hoechstens auf idle warten
IDLE_ABSTAND_SEKUNDEN = 10.0


class StartauswahlFehler(Exception):
    """Lage nicht lesbar, autoboot.txt nicht schreibbar oder ein Befehl von RAUC bzw. systemd gescheitert."""


class _FristAbgelaufen(StartauswahlFehler):
    """Ein Befehl hat seine Frist ueberschritten (nur rauc install behandelt das anders als jeden Fehler)."""


class Startauswahl(Protocol):
    def gestartet(self) -> str: ...
    def primaer(self) -> str: ...
    def im_probestart(self) -> bool: ...
    def compatible(self) -> str: ...
    def installieren(self, bundle: Path) -> None: ...
    def probestart(self) -> None: ...
    def bestaetigen(self) -> None: ...
    def zurueckschalten(self) -> None: ...
    def normal_neustarten(self) -> None: ...


def andere(slot: str) -> str:
    return "B" if slot == "A" else "A"


def lese_gestartet(dt: Path = DT_BOOTLOADER) -> str:
    """Slot der gestarteten Boot-Partition (Big-Endian-u32 in der DT-Datei partition)."""
    try:
        roh = (dt / "partition").read_bytes()
    except OSError as fehler:
        raise StartauswahlFehler(f"{dt / 'partition'} nicht lesbar: {type(fehler).__name__}") from None
    partition = int.from_bytes(roh, "big") if len(roh) == 4 else None
    if partition not in SLOT_VON_PARTITION:
        raise StartauswahlFehler(f"{dt / 'partition'}: unerwarteter Inhalt {roh.hex()} (erwartet Partition 2 oder 3)")
    return SLOT_VON_PARTITION[partition]


def lese_primaer(autoboot: Path = AUTOBOOT) -> str:
    """Slot von [all] boot_partition; fehlt die Zeile oder ist die Partition weder 2 noch 3, ein Fehler."""
    try:
        text = autoboot.read_text(encoding="ascii")
    except (OSError, ValueError) as fehler:
        raise StartauswahlFehler(f"{autoboot} nicht lesbar: {type(fehler).__name__}") from None
    abschnitt, wert = "", None
    for zeile in text.splitlines():
        zeile = zeile.strip()
        if zeile.startswith("["):
            abschnitt = zeile
        elif abschnitt == "[all]" and "=" in zeile:
            schluessel, _, rest = zeile.partition("=")
            if schluessel.strip() == "boot_partition":
                wert = rest.strip()
    if wert is None or not wert.isdigit() or int(wert) not in SLOT_VON_PARTITION:
        raise StartauswahlFehler(f"{autoboot}: [all] boot_partition fehlt oder ist weder 2 noch 3")
    return SLOT_VON_PARTITION[int(wert)]


def autoboot_text(primaer: str) -> str:
    """Die einzige Form von autoboot.txt: [all] = primaer, [tryboot] = der andere Slot."""
    return (f"[all]\ntryboot_a_b=1\nboot_partition={PARTITION_VON_SLOT[primaer]}\n"
            f"[tryboot]\nboot_partition={PARTITION_VON_SLOT[andere(primaer)]}\n")


def schreibe_primaer(autoboot: Path, primaer: str) -> None:
    """Schreibt autoboot.txt neu: <datei>.neu, fsync, rename, fsync des Ordners, Zuruecklesen. Nicht ueber
    smartheat_core.dateien: das setzt per fchmod einen Modus, den FAT nicht kennt. Ein Stromausfall vor dem rename
    laesst die alte Datei stehen (die Firmware ignoriert das .neu), danach gilt die neue."""
    text = autoboot_text(primaer)
    neu = autoboot.with_name(autoboot.name + ".neu")
    try:
        with contextlib.suppress(FileNotFoundError):
            neu.unlink()
        fd = os.open(neu, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
        try:
            rest = memoryview(text.encode("ascii"))
            while rest:
                rest = rest[os.write(fd, rest):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(neu, autoboot)
        ordner = os.open(autoboot.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(ordner)
        finally:
            os.close(ordner)
        gelesen = autoboot.read_text(encoding="ascii")
    except (OSError, ValueError) as fehler:
        raise StartauswahlFehler(f"{autoboot} nicht schreibbar: {type(fehler).__name__}") from None
    if gelesen != text:
        raise StartauswahlFehler(f"{autoboot}: beim Zuruecklesen steht etwas anderes als geschrieben")


class RaucTryboot:
    """Die echte Startauswahl: RAUC (rauc install, rauc status mark-*), systemd (reboot) und die Lage aus den Dateien.
    run, sleep und monotonic sind fuer Tests austauschbar."""

    def __init__(self, autoboot: Path = AUTOBOOT, dt: Path = DT_BOOTLOADER, run=subprocess.run,
                 sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._autoboot, self._dt = autoboot, dt
        self._run, self._sleep, self._monotonic = run, sleep, monotonic

    def gestartet(self) -> str:
        return lese_gestartet(self._dt)

    def primaer(self) -> str:
        return lese_primaer(self._autoboot)

    def im_probestart(self) -> bool:
        return self.gestartet() != self.primaer()

    def _befehl(self, argv: list[str], frist: float = FRIST_SEKUNDEN) -> subprocess.CompletedProcess:
        """Fuehrt den Befehl aus; Fehlschlag, fehlendes Programm und Fristablauf sind ein StartauswahlFehler."""
        name = " ".join(argv[:3])
        try:
            ergebnis = self._run(argv, capture_output=True, text=True, timeout=frist, check=False)
        except subprocess.TimeoutExpired:
            raise _FristAbgelaufen(f"{name}: Frist von {frist:g} s ueberschritten") from None
        except OSError as fehler:
            raise StartauswahlFehler(f"{name}: {type(fehler).__name__}") from None
        if ergebnis.returncode != 0:
            raise StartauswahlFehler(f"{name}: Exit {ergebnis.returncode}: {(ergebnis.stderr or '').strip()[-300:]}")
        return ergebnis

    def compatible(self) -> str:
        """compatible aus der system.conf, wie RAUC es sieht (rauc status --output-format=shell)."""
        for zeile in self._befehl(["rauc", "status", "--output-format=shell"]).stdout.splitlines():
            schluessel, _, wert = zeile.partition("=")
            if schluessel == "RAUC_SYSTEM_COMPATIBLE":
                werte = shlex.split(wert)
                if werte:
                    return werte[0]
        raise StartauswahlFehler("rauc status: kein RAUC_SYSTEM_COMPATIBLE (compatible nicht lesbar)")

    def installieren(self, bundle: Path) -> None:
        """rauc install mit Frist 1800 s. Laeuft sie ab, arbeitet der RAUC-Dienst weiter: erst auf idle warten (hoechstens
        weitere 1800 s), nie neu installieren, waehrend er noch schreibt. Ob die Installation gelang, weiss der Aufrufer
        dann nicht, also ein Fehler; der naechste Versuch installiert dasselbe Bundle noch einmal."""
        try:
            self._befehl(["rauc", "install", str(bundle)], INSTALL_FRIST_SEKUNDEN)
            return
        except _FristAbgelaufen:
            pass
        ende = self._monotonic() + IDLE_WARTEN_SEKUNDEN
        while self._monotonic() < ende:
            # rauc status zeigt die Operation nicht; sie ist die D-Bus-Eigenschaft Operation des RAUC-Dienstes
            try:
                ausgabe = self._befehl(["busctl", "get-property", "de.pengutronix.rauc", "/",
                                        "de.pengutronix.rauc.Installer", "Operation"]).stdout
            except StartauswahlFehler as fehler:
                raise StartauswahlFehler(f"rauc install: Operation des RAUC-Dienstes nicht lesbar: {fehler}") from None
            if '"idle"' in ausgabe:
                raise StartauswahlFehler("rauc install: Frist ueberschritten, Dienst jetzt idle, Ergebnis unbekannt")
            self._sleep(IDLE_ABSTAND_SEKUNDEN)
        raise StartauswahlFehler(f"rauc install: Frist ueberschritten, Dienst nach weiteren {IDLE_WARTEN_SEKUNDEN:g} s "
                                 "nicht idle")

    def probestart(self) -> None:
        self._befehl(["systemctl", "reboot", "--reboot-argument=0 tryboot"])

    def bestaetigen(self) -> None:
        self._befehl(["rauc", "status", "mark-good"])

    def zurueckschalten(self) -> None:
        self._befehl(["rauc", "status", "mark-bad", "booted"])
        self._befehl(["systemctl", "reboot"])

    def normal_neustarten(self) -> None:
        """Neustart ohne tryboot und ohne mark-bad: die Firmware startet danach [all]. Im Probestart der Rueckweg, wenn
        zurueckschalten() scheitert (tryboot gilt nur fuer einen Start)."""
        self._befehl(["systemctl", "reboot"])
