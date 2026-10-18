"""Supervisor-Discovery des Add-ons (Spec 5c 1.1): meldet der HA-Instanz Geraete-ID, Uebernahmestand (gesperrt bei
Sperre) und - solange das Geraet nicht uebernommen ist - den Uebernahme-Code; HA startet daraufhin den Assistenten der
Integration (async_step_hassio). Die Daten bleiben in der Instanz (Supervisor <-> Core). Belegt durch die Probe auf
einer echten HA-OS-Instanz (W Teil A, 2026-10-12, Spec 5c §8): die Meldung startet den Flow der Integration aus
custom_components; der Supervisor haelt eine Meldung je Add-on und Dienst und ersetzt sie bei einem neuen POST unter
derselben UUID.

Neu gemeldet wird nur bei einer Aenderung; vergibt der Supervisor dabei eine neue UUID (die alte Meldung war verloren),
wird die vorige (auch die eines frueheren Prozesses, UUID in discovery.json) danach entfernt - gelingt das nicht,
bleibt sie vorgemerkt und wird erneut versucht, nie aufgegeben (5c-D-9): der Abstand waechst je Fehlversuch von
RETRY_SECONDS bis ENTFERNEN_MAX_SECONDS, damit kein alter Code im Supervisor stehen bleibt (hoechstens VERALTET_MAX
vorgemerkt). Wirft nie. Eine gescheiterte Meldung wird fruehestens nach RETRY_SECONDS wiederholt (der Aufruf kommt im
Sekundentakt des Geraets; der Supervisor wird nicht bestuermt, das Log nicht gefuellt); die Drossel merkt sich jede
gescheiterte Meldung (hoechstens DROSSEL_MAX, die aelteste faellt heraus), eine andere Meldung geht sofort hinaus.

Stuerzt der Prozess zwischen POST und Speichern der UUID ab, bleibt keine verwaiste Meldung: der Supervisor haelt eine
Meldung je Add-on und Dienst und ersetzt sie beim ersten POST des naechsten Starts unter derselben UUID (Probe
2026-10-12); GET /discovery, mit dem sich fremde Meldungen auflisten liessen, ist dem Add-on verwehrt. Der Code steht
nie im Log und nie in discovery.json (Regel 6)."""
import logging
import time
from collections.abc import Callable
from pathlib import Path

from smartheat_core import dateien

logger = logging.getLogger(__name__)

DIENST = "smartheat"
FIELDS = ("geraet_id", "zustand", "claim_code")
NICHT_UEBERNOMMEN = "nicht_uebernommen"
RETRY_SECONDS = 60
ENTFERNEN_MAX_SECONDS = 3600  # groesster Abstand zwischen zwei Versuchen, eine alte Meldung zu entfernen
DROSSEL_MAX = 16  # so viele gescheiterte Meldungen merkt sich die Drossel
VERALTET_MAX = 16  # so viele alte Meldungen bleiben zum Entfernen vorgemerkt, die aelteste faellt mit einer Warnung weg


def _schluessel(config: dict) -> tuple:
    return tuple(sorted(config.items()))


def _pause(versuche: int) -> float:
    """Abstand nach dem n-ten gescheiterten Entfernen: 60, 120, 240 ... s, hoechstens ENTFERNEN_MAX_SECONDS."""
    return min(RETRY_SECONDS * 2 ** min(versuche - 1, 6), ENTFERNEN_MAX_SECONDS)


class Discovery:
    def __init__(self, supervisor, pfad: Path, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._supervisor, self._pfad, self._clock = supervisor, pfad, clock
        self._gemeldet: dict | None = None
        self._gedrosselt: dict[tuple, float] = {}  # gescheiterte Meldung -> fruehester neuer Versuch
        self._entfernen_ab: dict[str, float] = {}  # alte UUID -> fruehester neuer Versuch (nur im Speicher)
        stored = dateien.lesen_json(pfad)
        stored = stored if isinstance(stored, dict) else {}
        self._uuid: str | None = stored.get("uuid") if isinstance(stored.get("uuid"), str) else None
        veraltet = stored.get("entfernen")
        self._veraltet: list[str] = [u for u in veraltet if isinstance(u, str)] if isinstance(veraltet, list) else []
        versuche = stored.get("versuche")
        self._versuche: dict[str, int] = ({u: n for u, n in versuche.items() if isinstance(n, int)}
                                          if isinstance(versuche, dict) else {})
        self._begrenzen()

    def aktualisieren(self, geraet_id: str, zustand: str | None, claim_code: str | None) -> None:
        werte = {"geraet_id": geraet_id, "zustand": zustand or "startet",
                 "claim_code": claim_code if zustand == NICHT_UEBERNOMMEN and claim_code else None}
        config = {feld: werte[feld] for feld in FIELDS if werte[feld] is not None}
        try:
            if config != self._gemeldet:
                self._melden(config)
            if self._veraltet:
                self._aufraeumen()
        except Exception as error:  # nie werfen: die Anzeige des Geraets laeuft weiter
            logger.warning("Discovery fehlgeschlagen (%s)", type(error).__name__)

    def _melden(self, config: dict) -> None:
        schluessel, now = _schluessel(config), self._clock()
        if now < self._gedrosselt.get(schluessel, now):
            return
        try:
            uuid = self._supervisor.discovery_anmelden(config)
        except Exception as error:
            self._drosseln(schluessel, now + RETRY_SECONDS)
            logger.warning("Discovery nicht gemeldet (%s), neuer Versuch in %d s", type(error).__name__, RETRY_SECONDS)
            return
        self._gedrosselt.pop(schluessel, None)
        alt, self._uuid, self._gemeldet = self._uuid, uuid, config
        if alt and alt != uuid and alt not in self._veraltet:
            self._veraltet.append(alt)
        self._veraltet = [u for u in self._veraltet if u != uuid]
        self._begrenzen()
        self._speichern()

    def _begrenzen(self) -> None:
        """Hoechstens VERALTET_MAX vorgemerkte alte Meldungen (vergibt der Supervisor immer neue UUIDs und scheitert
        jedes Entfernen, wuechse die Liste sonst ohne Grenze): die aeltesten fallen samt Zaehler weg, mit einer
        Warnung."""
        while len(self._veraltet) > VERALTET_MAX:
            uuid = self._veraltet.pop(0)
            self._versuche.pop(uuid, None)
            self._entfernen_ab.pop(uuid, None)
            logger.warning("Alte Discovery verworfen: mehr als %d nicht entfernte Meldungen vorgemerkt", VERALTET_MAX)

    def _drosseln(self, schluessel: tuple, bis: float) -> None:
        self._gedrosselt.pop(schluessel, None)
        self._gedrosselt = {k: t for k, t in self._gedrosselt.items() if t > self._clock()}
        while len(self._gedrosselt) >= DROSSEL_MAX:
            del self._gedrosselt[next(iter(self._gedrosselt))]
        self._gedrosselt[schluessel] = bis

    def _aufraeumen(self) -> None:
        """Entfernt vorgemerkte alte Meldungen. Eine, die sich nicht entfernen laesst, haelt die uebrigen nicht auf und
        wird nach einer je Fehlversuch wachsenden Pause erneut versucht (der Zaehler ueberlebt den Neustart, die Pause
        nicht: nach dem Start wird sofort versucht)."""
        now = self._clock()
        vorher = (list(self._veraltet), dict(self._versuche))
        for uuid in list(self._veraltet):
            if now < self._entfernen_ab.get(uuid, now):
                continue
            try:
                self._supervisor.discovery_entfernen(uuid)
            except Exception as error:
                n = self._versuche[uuid] = self._versuche.get(uuid, 0) + 1
                pause = _pause(n)
                self._entfernen_ab[uuid] = now + pause
                logger.warning("Alte Discovery nicht entfernt (%s), neuer Versuch in %d s", type(error).__name__, pause)
                continue
            self._veraltet.remove(uuid)
            self._versuche.pop(uuid, None)
            self._entfernen_ab.pop(uuid, None)
        if (self._veraltet, self._versuche) != vorher:
            self._speichern()

    def _speichern(self) -> None:
        dateien.schreiben_json(self._pfad,
                               {"uuid": self._uuid, "entfernen": self._veraltet, "versuche": self._versuche},
                               modus=dateien.PRIVAT)
