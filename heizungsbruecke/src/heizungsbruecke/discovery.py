"""Supervisor-Discovery des Add-ons (Spec 5c 1.1): meldet der HA-Instanz Geraete-ID, Uebernahmestand (gesperrt bei
Sperre) und - solange das Geraet nicht uebernommen ist - den Uebernahme-Code; HA startet daraufhin den Assistenten der
Integration (async_step_hassio). Die Daten bleiben in der Instanz (Supervisor <-> Core). Belegt durch die Probe auf
einer echten HA-OS-Instanz (W Teil A, 2026-10-12, Spec 5c §8): die Meldung startet den Flow der Integration aus
custom_components; der Supervisor haelt eine Meldung je Add-on und Dienst und ersetzt sie bei einem neuen POST unter
derselben UUID.

Neu gemeldet wird nur bei einer Aenderung; vergibt der Supervisor dabei eine neue UUID (die alte Meldung war verloren),
wird die vorige (auch die eines frueheren Prozesses, UUID in discovery.json) danach entfernt - gelingt das nicht,
bleibt sie vorgemerkt und wird erneut versucht, damit kein alter Code im Supervisor stehen bleibt. Wirft nie. Ein
Fehler wird fruehestens nach RETRY_SECONDS wiederholt (der Aufruf kommt im Sekundentakt des Geraets; der Supervisor
wird nicht bestuermt, das Log nicht gefuellt), eine geaenderte Meldung geht sofort hinaus. Der Code steht nie im Log
und nie in discovery.json (Regel 6)."""
import logging
import time
from collections.abc import Callable
from pathlib import Path

from smartheat_device.documents import read_json, write_json

logger = logging.getLogger(__name__)

DIENST = "smartheat"
FIELDS = ("geraet_id", "zustand", "claim_code")
NICHT_UEBERNOMMEN = "nicht_uebernommen"
RETRY_SECONDS = 60
MAX_ENTFERNEN_VERSUCHE = 10  # danach gilt eine nicht entfernbare alte Meldung als weg (einmal im Log)


class Discovery:
    def __init__(self, supervisor, pfad: Path, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._supervisor, self._pfad, self._clock = supervisor, pfad, clock
        self._gemeldet: dict | None = None
        self._melden_ab: tuple[dict, float] | None = None  # (fehlgeschlagene Meldung, fruehester neuer Versuch)
        self._aufraeumen_ab = 0.0
        stored = read_json(pfad)
        stored = stored if isinstance(stored, dict) else {}
        self._uuid: str | None = stored.get("uuid") if isinstance(stored.get("uuid"), str) else None
        veraltet = stored.get("entfernen")
        self._veraltet: list[str] = [u for u in veraltet if isinstance(u, str)] if isinstance(veraltet, list) else []
        versuche = stored.get("versuche")
        self._versuche: dict[str, int] = ({u: n for u, n in versuche.items() if isinstance(n, int)}
                                          if isinstance(versuche, dict) else {})

    def aktualisieren(self, geraet_id: str, zustand: str | None, claim_code: str | None) -> None:
        config = {"geraet_id": geraet_id, "zustand": zustand or "startet"}
        if zustand == NICHT_UEBERNOMMEN and claim_code:
            config["claim_code"] = claim_code
        try:
            if config != self._gemeldet:
                self._melden(config)
            if self._veraltet:
                self._aufraeumen()
        except Exception as error:  # nie werfen: die Anzeige des Geraets laeuft weiter
            logger.warning("Discovery fehlgeschlagen (%s)", type(error).__name__)

    def _melden(self, config: dict) -> None:
        sperre = self._melden_ab
        if sperre is not None and sperre[0] == config and self._clock() < sperre[1]:
            return
        try:
            uuid = self._supervisor.discovery_anmelden(config)
        except Exception as error:
            self._melden_ab = (config, self._clock() + RETRY_SECONDS)
            logger.warning("Discovery nicht gemeldet (%s), neuer Versuch in %d s", type(error).__name__, RETRY_SECONDS)
            return
        self._melden_ab = None
        alt, self._uuid, self._gemeldet = self._uuid, uuid, config
        if alt and alt != uuid and alt not in self._veraltet:
            self._veraltet.append(alt)
        self._veraltet = [u for u in self._veraltet if u != uuid]
        self._speichern()

    def _aufraeumen(self) -> None:
        """Entfernt vorgemerkte alte Meldungen. Eine, die sich nicht entfernen laesst, haelt die uebrigen nicht auf; nach
        MAX_ENTFERNEN_VERSUCHEN Fehlschlaegen wird sie aufgegeben (der Supervisor kennt sie evtl. nicht mehr)."""
        if self._clock() < self._aufraeumen_ab:
            return
        vorher = (list(self._veraltet), dict(self._versuche))
        gescheitert = False
        for uuid in list(self._veraltet):
            try:
                self._supervisor.discovery_entfernen(uuid)
            except Exception as error:
                gescheitert = True
                n = self._versuche[uuid] = self._versuche.get(uuid, 0) + 1
                if n < MAX_ENTFERNEN_VERSUCHE:
                    logger.warning("Alte Discovery nicht entfernt (%s), neuer Versuch in %d s", type(error).__name__,
                                   RETRY_SECONDS)
                    continue
                logger.warning("Alte Discovery nicht entfernt (%s), nach %d Versuchen aufgegeben",
                               type(error).__name__, n)
            self._veraltet.remove(uuid)
            self._versuche.pop(uuid, None)
        if gescheitert:
            self._aufraeumen_ab = self._clock() + RETRY_SECONDS
        if (self._veraltet, self._versuche) != vorher:
            self._speichern()

    def _speichern(self) -> None:
        write_json(self._pfad, {"uuid": self._uuid, "entfernen": self._veraltet, "versuche": self._versuche},
                   private=True)
