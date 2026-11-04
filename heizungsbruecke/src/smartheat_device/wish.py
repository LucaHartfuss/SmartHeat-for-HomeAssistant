"""Bedienwunsch: ein lokal gestellter Sollwert wirkt sofort lokal (der Host wendet den begrenzten Wert
an, Boost wie heute). Das Geraet begrenzt ihn auf den Bedienbereich 15-25 °C und rundet kaufmaennisch auf 0,5 K (keine
Sicherheitsgrenze), schickt ihn als up/wish mit der Version der Bedienung, auf der es steht (basis_version),
und puffert ohne Verbindung je Kreis nur den juengsten (<daten>/device/wunsch-<kreis>.json). Erledigt ist er mit dem
PUBACK des Brokers; eine Trennung vorher schickt ihn nach dem naechsten hello erneut. Danach entscheidet der Server und schickt
die Bedienung zurueck, falls der Wunsch verliert."""
import math
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from smartheat_core import dateien
from smartheat_core.zahlen import ist_endliche_zahl
from smartheat_device import nachrichten, wire

WUNSCH_MUSTER = "wunsch-*.json"
#: Nach einem PUBACK mit Fehlercode (typisch Drosselung, 0x97) geht der Wunsch fruehestens nach dieser Zeit erneut
#: hinaus (monotone Uhr); ein neuer Wunsch und ein Verbindungsverlust heben die Sperre auf.
WUNSCH_NACH_ABLEHNUNG_SECONDS = 60.0
BEGRENZT_KEY = "wunsch_begrenzt"
SOLL_QUELLE_AUS_KEY = "soll_quelle_aus"
_QUELLE = {"thermostat": "Thermostat", "soll_entity": "Wunschtemperatur", "ha": "Wunschtemperatur"}


def begrenzen(wert) -> tuple[float, float | None]:
    """(begrenzter Wert, roh) - roh ist der gestellte Wert, wenn Bereich oder Rundung ihn veraendert haben."""
    if isinstance(wert, bool) or not isinstance(wert, int | float):
        raise ValueError("Wunsch ist keine Zahl")
    if not ist_endliche_zahl(wert):
        raise ValueError("Wunsch ist keine endliche Zahl")
    # erst in den Bereich, dann runden: riesige endliche Werte laufen so nicht in einen Ueberlauf (die Grenzen liegen
    # auf dem Raster, das Ergebnis ist dasselbe wie Runden und dann Begrenzen)
    im_bereich = min(wire.RAUM_SOLL_MAX, max(wire.RAUM_SOLL_MIN, wert))
    begrenzt = float(math.floor(im_bereich / wire.RAUM_SOLL_STEP + 0.5) * wire.RAUM_SOLL_STEP)
    return begrenzt, (None if begrenzt == wert else float(wert))


def wunsch_datei(kreis: int) -> str:
    return f"wunsch-{kreis}.json"


def _zahl(value: float) -> str:
    return f"{value:g}".replace(".", ",")


def begrenzt_text(roh: float, begrenzt: float, herkunft: str | None) -> str:
    quelle = _QUELLE.get(herkunft or "", "Wunschtemperatur")
    return f"{quelle} auf {_zahl(roh)} °C gestellt, SmartHeat regelt auf {_zahl(begrenzt)} °C"


def soll_quelle_aus_text(wirksam: float | None) -> str:
    """Ohne wirksames Soll (unbekannt) nennt der Text keinen Wert; die Meldung bleibt trotzdem offen."""
    regelt = "SmartHeat regelt weiter" if wirksam is None else f"SmartHeat regelt weiter auf {_zahl(wirksam)} °C"
    return f"Thermostat ist aus, {regelt}; Heizung aus über die Sommersperre bzw. im Portal"


@dataclass(frozen=True)
class Wunsch:
    wish_id: str
    kreis: int
    basis_version: int
    ts: str
    uhr_synchron: bool
    raum_soll: float
    roh: float | None = None
    herkunft: str | None = None
    durch: str | None = None

    def payload(self) -> dict:
        werte = nachrichten.pruefen("wunsch.werte", {"raum_soll": self.raum_soll}, wire.WISH_WERTE)
        body = {"schema": wire.SCHEMA, "wish_id": self.wish_id, "kreis": self.kreis,
                "basis_version": self.basis_version, "ts": self.ts,
                "uhr_synchron": self.uhr_synchron, "werte": werte}
        for key in wire.WISH_OPTIONAL:
            value = getattr(self, key)
            if value is not None:
                body[key] = value
        return nachrichten.pruefen("wunsch", body, wire.WISH_FIELDS, wire.WISH_OPTIONAL)


def pruefen_herkunft(herkunft: str | None, durch: str | None) -> None:
    """ValueError, wenn herkunft oder durch kein Vertragswert ist (None = nicht angegeben). Device.wunsch prueft so
    schon im aufrufenden Thread, wie bei einer nicht endlichen Zahl."""
    if herkunft is not None and herkunft not in wire.HERKUNFT:
        raise ValueError("herkunft unbekannt")
    if durch is not None and durch not in wire.DURCH:
        raise ValueError("durch unbekannt")


def pruefen_kreis(kreis) -> None:
    """ValueError, wenn kreis keine Kreisnummer ist (ganze Zahl von wire.KREIS_MIN bis KREIS_MAX, kein bool)."""
    if isinstance(kreis, bool) or not isinstance(kreis, int) or not wire.KREIS_MIN <= kreis <= wire.KREIS_MAX:
        raise ValueError("kreis ist keine Kreisnummer")


def neu(raum_soll: float, roh: float | None, *, kreis: int, basis_version: int, uhr_synchron: bool, herkunft: str | None,
        durch: str | None, wall: float) -> Wunsch:
    pruefen_herkunft(herkunft, durch)
    pruefen_kreis(kreis)
    ts = datetime.fromtimestamp(wall, UTC).isoformat(timespec="seconds")
    return Wunsch(uuid.uuid4().hex, kreis, basis_version, ts, uhr_synchron, raum_soll, roh, herkunft, durch)


def _laden(path: Path) -> Wunsch | None:
    raw = dateien.lesen_json(path)
    if not isinstance(raw, dict):
        return None
    try:
        wunsch = Wunsch(**raw)
    except TypeError:
        return None
    gueltig = (isinstance(wunsch.wish_id, str) and isinstance(wunsch.kreis, int) and not isinstance(wunsch.kreis, bool)
               and wire.KREIS_MIN <= wunsch.kreis <= wire.KREIS_MAX and isinstance(wunsch.basis_version, int)
               and isinstance(wunsch.ts, str) and isinstance(wunsch.uhr_synchron, bool)
               and isinstance(wunsch.raum_soll, int | float))
    return wunsch if gueltig else None


class _Platz:
    """Der Puffer eines Kreises: der Wunsch, die mid seines laufenden Sendens und die Sperre nach einer Ablehnung."""

    def __init__(self, wunsch: Wunsch | None = None) -> None:
        self.wunsch = wunsch
        self.mid: int | None = None
        self.gesperrt_bis: float | None = None  # monotone Uhr, nach einem abgelehnten PUBACK


class WunschPuffer:
    """Je Kreis der juengste ungesendete Wunsch (persistiert in wunsch-<kreis>.json) und die mid seines laufenden
    Sendens. Ein Wunsch eines Kreises ersetzt nie den eines anderen; mids sind je Link eindeutig."""

    def __init__(self, device_dir: Path, clock: Callable[[], float] = time.monotonic) -> None:
        self._dir = device_dir
        self._clock = clock
        self._plaetze: dict[int, _Platz] = {}
        for path in sorted(device_dir.glob(WUNSCH_MUSTER)):
            wunsch = _laden(path)
            if wunsch is not None and path.name == wunsch_datei(wunsch.kreis):  # Datei und Inhalt nennen denselben Kreis
                self._plaetze[wunsch.kreis] = _Platz(wunsch)

    def wunsch(self, kreis: int) -> Wunsch | None:
        platz = self._plaetze.get(kreis)
        return platz.wunsch if platz is not None else None

    def kreise(self) -> list[int]:
        """Die Kreise mit einem gepufferten Wunsch, aufsteigend."""
        return sorted(kreis for kreis, platz in self._plaetze.items() if platz.wunsch is not None)

    def offen(self, kreis: int) -> bool:
        """Zu senden: der Kreis hat einen Wunsch, er ist nicht unterwegs und nicht wegen einer Ablehnung gesperrt."""
        platz = self._plaetze.get(kreis)
        return (platz is not None and platz.wunsch is not None and platz.mid is None
                and (platz.gesperrt_bis is None or self._clock() >= platz.gesperrt_bis))

    def setzen(self, wunsch: Wunsch) -> None:
        platz = self._plaetze.setdefault(wunsch.kreis, _Platz())
        platz.wunsch, platz.mid, platz.gesperrt_bis = wunsch, None, None
        dateien.schreiben_json(self._dir / wunsch_datei(wunsch.kreis), asdict(wunsch), modus=dateien.PRIVAT)

    def gesendet(self, kreis: int, mid: int | None) -> None:
        platz = self._plaetze.get(kreis)
        if platz is not None:
            platz.mid = mid

    def bestaetigt(self, mid: int) -> bool:
        for kreis, platz in self._plaetze.items():
            if platz.wunsch is not None and platz.mid is not None and platz.mid == mid:
                platz.wunsch, platz.mid = None, None
                (self._dir / wunsch_datei(kreis)).unlink(missing_ok=True)
                return True
        return False

    def abgelehnt(self, mid: int) -> None:
        """Der Broker lehnte die Nachricht ab (PUBACK mit Fehlercode): der Wunsch ist nicht unterwegs, er geht nach
        WUNSCH_NACH_ABLEHNUNG_SECONDS erneut hinaus (Drosselung nicht durch Sekundentakt verschaerfen)."""
        for platz in self._plaetze.values():
            if platz.mid is not None and platz.mid == mid:
                platz.mid = None
                platz.gesperrt_bis = self._clock() + WUNSCH_NACH_ABLEHNUNG_SECONDS

    def verbindung_verloren(self) -> None:
        for platz in self._plaetze.values():
            platz.mid, platz.gesperrt_bis = None, None
