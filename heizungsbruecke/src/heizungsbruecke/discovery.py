"""Supervisor-Discovery des Add-ons (Spec 5c 1.1): meldet der HA-Instanz Geraete-ID, Uebernahmestand und - solange das
Geraet nicht uebernommen ist - den Uebernahme-Code; HA startet daraufhin den Assistenten der Integration
(async_step_hassio). Die Daten bleiben in der Instanz (Supervisor <-> Core). Die Probe auf einer echten HA-OS-Instanz
(startet die Meldung den Flow der HACS-Integration?) steht noch aus (W Teil A); scheitert sie, wechselt dieses Modul auf
die Variante "Ereignis" (HA-Ereignis smartheat_discovery mit denselben Feldern).

Neu gemeldet wird nur bei einer Aenderung; die vorige Meldung (auch die eines frueheren Prozesses, UUID in
discovery.json) wird danach entfernt - gelingt das nicht, bleibt sie vorgemerkt und wird erneut versucht, damit kein
alter Code im Supervisor stehen bleibt. Wirft nie. Ein Fehler wird fruehestens nach RETRY_SECONDS wiederholt (der
Aufruf kommt im Sekundentakt des Geraets; der Supervisor wird nicht bestuermt, das Log nicht gefuellt), eine geaenderte
Meldung geht sofort hinaus. Der Code steht nie im Log und nie in discovery.json (Regel 6)."""
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
        if self._clock() < self._aufraeumen_ab:
            return
        for uuid in list(self._veraltet):
            try:
                self._supervisor.discovery_entfernen(uuid)
            except Exception as error:
                self._aufraeumen_ab = self._clock() + RETRY_SECONDS
                logger.warning("Alte Discovery nicht entfernt (%s), neuer Versuch in %d s", type(error).__name__,
                               RETRY_SECONDS)
                break
            self._veraltet.remove(uuid)
        self._speichern()

    def _speichern(self) -> None:
        write_json(self._pfad, {"uuid": self._uuid, "entfernen": self._veraltet}, private=True)
