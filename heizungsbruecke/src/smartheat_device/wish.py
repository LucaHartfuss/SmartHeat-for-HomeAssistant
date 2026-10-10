"""Bedienwunsch (Spec 5b 3.2/3.3): ein lokal gestellter Sollwert wirkt sofort lokal (der Host wendet den begrenzten Wert
an, Boost wie heute). Das Geraet begrenzt ihn auf den Bedienbereich 15-25 °C und rundet kaufmaennisch auf 0,5 K (keine
Sicherheitsgrenze, Spec 3.2), schickt ihn als up/wish mit der Version der Bedienung, auf der es steht (basis_version),
und puffert ohne Verbindung nur den juengsten (<daten>/device/wunsch.json). Erledigt ist er mit dem PUBACK des Brokers;
eine Trennung vorher schickt ihn nach dem naechsten hello erneut. Danach entscheidet der Server (Spec 3.3) und schickt
die Bedienung zurueck, falls der Wunsch verliert."""
import math
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from smartheat_device import wire
from smartheat_device.documents import read_json, write_json

WUNSCH_FILE = "wunsch.json"
BEGRENZT_KEY = "wunsch_begrenzt"
SOLL_QUELLE_AUS_KEY = "soll_quelle_aus"
_QUELLE = {"thermostat": "Thermostat", "soll_entity": "Wunschtemperatur", "smartheat_entity": "Wunschtemperatur"}


def begrenzen(wert) -> tuple[float, float | None]:
    """(begrenzter Wert, roh) - roh ist der gestellte Wert, wenn Bereich oder Rundung ihn veraendert haben."""
    if isinstance(wert, bool) or not isinstance(wert, (int, float)):
        raise ValueError("Wunsch ist keine Zahl")
    try:
        endlich = math.isfinite(wert)
    except OverflowError:
        endlich = False
    if not endlich:
        raise ValueError("Wunsch ist keine endliche Zahl")
    gerundet = math.floor(wert / wire.RAUM_SOLL_STEP + 0.5) * wire.RAUM_SOLL_STEP
    begrenzt = float(min(wire.RAUM_SOLL_MAX, max(wire.RAUM_SOLL_MIN, gerundet)))
    return begrenzt, (None if begrenzt == wert else float(wert))


def _zahl(value: float) -> str:
    return f"{value:g}".replace(".", ",")


def begrenzt_text(roh: float, begrenzt: float, herkunft: str | None) -> str:
    quelle = _QUELLE.get(herkunft or "", "Wunschtemperatur")
    return f"{quelle} auf {_zahl(roh)} °C gestellt, SmartHeat regelt auf {_zahl(begrenzt)} °C"


def soll_quelle_aus_text(wirksam: float) -> str:
    return (f"Thermostat ist aus, SmartHeat regelt weiter auf {_zahl(wirksam)} °C; Heizung aus über die Sommersperre "
            "bzw. im Portal")


@dataclass(frozen=True)
class Wunsch:
    wish_id: str
    basis_version: int
    ts: str
    uhr_synchron: bool
    raum_soll: float
    roh: float | None = None
    herkunft: str | None = None
    durch: str | None = None

    def payload(self) -> dict:
        body = {"schema": wire.SCHEMA, "wish_id": self.wish_id, "basis_version": self.basis_version, "ts": self.ts,
                "uhr_synchron": self.uhr_synchron, "werte": {"raum_soll": self.raum_soll}}
        for key in wire.WISH_OPTIONAL:
            value = getattr(self, key)
            if value is not None:
                body[key] = value
        return body


def neu(raum_soll: float, roh: float | None, *, basis_version: int, uhr_synchron: bool, herkunft: str | None,
        durch: str | None, wall: float) -> Wunsch:
    if herkunft is not None and herkunft not in wire.HERKUNFT:
        raise ValueError(f"herkunft {herkunft!r} unbekannt")
    if durch is not None and durch not in wire.DURCH:
        raise ValueError(f"durch {durch!r} unbekannt")
    ts = datetime.fromtimestamp(wall, UTC).isoformat(timespec="seconds")
    return Wunsch(uuid.uuid4().hex, basis_version, ts, uhr_synchron, raum_soll, roh, herkunft, durch)


def _laden(path: Path) -> Wunsch | None:
    raw = read_json(path)
    if not isinstance(raw, dict):
        return None
    try:
        wunsch = Wunsch(**raw)
    except TypeError:
        return None
    gueltig = (isinstance(wunsch.wish_id, str) and isinstance(wunsch.basis_version, int)
               and isinstance(wunsch.ts, str) and isinstance(wunsch.uhr_synchron, bool)
               and isinstance(wunsch.raum_soll, int | float))
    return wunsch if gueltig else None


class WunschPuffer:
    """Juengster ungesendeter Wunsch (persistiert) und die mid seines laufenden Sendens."""

    def __init__(self, device_dir: Path) -> None:
        self._path = device_dir / WUNSCH_FILE
        self.wunsch = _laden(self._path)
        self._mid: int | None = None

    def offen(self) -> bool:
        """Zu senden: ein Wunsch liegt vor und ist nicht unterwegs."""
        return self.wunsch is not None and self._mid is None

    def setzen(self, wunsch: Wunsch) -> None:
        self.wunsch, self._mid = wunsch, None
        write_json(self._path, asdict(wunsch))

    def gesendet(self, mid: int | None) -> None:
        self._mid = mid

    def bestaetigt(self, mid: int) -> bool:
        if self.wunsch is None or self._mid is None or mid != self._mid:
            return False
        self.wunsch, self._mid = None, None
        self._path.unlink(missing_ok=True)
        return True

    def verbindung_verloren(self) -> None:
        self._mid = None
