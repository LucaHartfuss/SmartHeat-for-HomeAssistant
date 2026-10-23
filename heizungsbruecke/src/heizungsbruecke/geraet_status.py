"""Status-Ereignis des HA-Add-ons: smartheat_status Schema 3 = der Laufzeit-Status (Schema 2,
smartheat_runtime.status, unveraendert fuer Gateway und up/status) plus Geraet (ID, Zustand, Verbindung), wirksamer
Raum-Soll (Soll-Speicher) und Hinweise fuer die Reparatur-Hinweise der Integration. Nie der Uebernahme-Code (der steht
nur in der Discovery): ins Ereignis kommen nur die benannten Felder, nie ein ganzes Objekt der Quellen. Gesendet bei
jedem Ereignis der Laufzeit (auch dem Lebenszeichen alle 300 s) und bei einer inhaltlichen Aenderung der Anzeige.
Felder sind Cross-Repo-Vertrag mit der Integration (Contract-Check)."""
import logging
import threading
import time
from collections.abc import Callable

from heizungsbruecke.ha_sinks import EVENT_TYPE
from heizungsbruecke.version import ADDON_VERSION
from smartheat_device.host import Anzeige
from smartheat_runtime import status

logger = logging.getLogger(__name__)

EVENT_SCHEMA = 3
GERAET_FIELDS = ("geraet_id", "geraet_zustand", "server_verbunden", "raum_soll", "geraet_hinweise")
EVENT_FIELDS = (*status.EVENT_FIELDS, *GERAET_FIELDS)
HINWEIS_FIELDS = ("key", "stufe", "text")
KONFIGURATION_ABGELEHNT = "konfiguration_abgelehnt"
#: HA nicht erreichbar: so lange Pause bis zum naechsten Versuch des Takts (wie discovery.RETRY_SECONDS: Log nicht fuellen).
WIEDERHOLEN_SECONDS = 60
ZUSTAND_TEXTE = {
    "update_noetig": "Das Add-on ist zu alt für den SmartHeat-Dienst. Bitte das Add-on aktualisieren.",
    "gesperrt": "Dieses Gerät ist beim SmartHeat-Dienst gesperrt. Bitte den Support kontaktieren.",
}


def _hinweis(key: str, stufe: str, text: str) -> dict:
    return dict(zip(HINWEIS_FIELDS, (key, stufe, text), strict=True))


def hinweise(anzeige: Anzeige) -> list[dict]:
    """Hinweise der Anzeige als {key, stufe, text}: Hinweise des Servers, eigene Meldungen des Geraets, abgelehnte
    Konfiguration und die Zustaende update_noetig/gesperrt. Bei update_noetig mit abgelehnter Konfiguration stehen
    bewusst beide Hinweise da (Brief-Test): der Grund der Ablehnung und die Handlungsanweisung."""
    result = [_hinweis(str(h["key"]), h.get("stufe") or "warnung", h.get("text") or "")
              for h in anzeige.hinweise if isinstance(h, dict) and h.get("key")]
    result += [_hinweis(str(m["key"]), "warnung", m.get("text") or "")
               for m in anzeige.meldungen if isinstance(m, dict) and m.get("key")]
    if anzeige.konfiguration_abgelehnt:
        result.append(_hinweis(KONFIGURATION_ABGELEHNT, "kritisch", anzeige.konfiguration_abgelehnt))
    if anzeige.zustand in ZUSTAND_TEXTE:
        result.append(_hinweis(anzeige.zustand, "kritisch", ZUSTAND_TEXTE[anzeige.zustand]))
    return result


class GeraetStatus:
    """Sendet nie zwei Ereignisse zugleich (_senden_lock) und ruft HA ausserhalb der Zustandssperre (_lock) auf: jedes
    Ereignis ist ein stimmiger Schnappschuss, und laufzeit_merken (sign_off) wartet nie auf HA. Ist HA nicht
    erreichbar (startup: services startet das Add-on vor HA Core), versucht der Takt des Geraete-Threads es erst nach
    WIEDERHOLEN_SECONDS erneut, ein Laufzeit-Ereignis in dieser Pause hoechstens einmal je Pause; eine Warnung je
    Ausfall (nur die Fehlerart), eine Info-Zeile bei der Erholung. Nach einem Fehler sendet der naechste erlaubte
    Versuch den aktuellen Stand, auch wenn er sich nicht geaendert hat."""

    def __init__(self, ha_api, geraet_id: str, raum_soll: Callable[[], float | None], *,
                 laufzeit_merken: Callable[[dict], None], clock: Callable[[], float] = time.monotonic) -> None:
        self._ha, self._geraet_id, self._raum_soll, self._merken = ha_api, geraet_id, raum_soll, laufzeit_merken
        self._clock = clock
        self._lock = threading.Lock()  # Zustand: Laufzeit-Ereignis und Anzeige
        self._senden_lock = threading.Lock()  # ein Sender zur Zeit; die Felder darunter nur mit ihm
        self._laufzeit: dict = {**dict.fromkeys(status.EVENT_FIELDS), "status": status.STATUS_STARTET,
                                "addon_version": ADDON_VERSION}
        self._anzeige: Anzeige | None = None
        self._zuletzt: dict | None = None
        self._pause_bis: float | None = None  # nach einem Fehler: vorher kein Versuch des Takts
        self._erzwungen_versucht = False  # ein Laufzeit-Ereignis in dieser Pause schon versucht

    def publish(self, event: dict) -> None:
        """StatusSink der Laufzeit (Worker-Thread). Wirft nicht: Fehler und Wiederholung regelt GeraetStatus (der
        Takt sendet nach der Pause den aktuellen Stand). Das unveraenderte Laufzeit-Ereignis (tenant_id, status, grund
        fuer sign_off) geht an laufzeit_merken."""
        with self._lock:
            self._laufzeit = {key: event.get(key) for key in status.EVENT_FIELDS}
            self._merken(dict(event))
        self._senden(force=True)

    def anzeige(self, anzeige: Anzeige) -> None:
        """Jeder Takt des Geraete-Threads; gesendet nur bei Aenderung (oder nach einem Fehler, nach der Pause)."""
        with self._lock:
            self._anzeige = anzeige
        self._senden(force=False)

    def _ereignis(self) -> dict:
        anzeige = self._anzeige
        return {**self._laufzeit, "schema": EVENT_SCHEMA, "geraet_id": self._geraet_id,
                "geraet_zustand": anzeige.zustand if anzeige is not None else "startet",
                "server_verbunden": anzeige.server_ok if anzeige is not None else False,
                "raum_soll": self._raum_soll(), "geraet_hinweise": hinweise(anzeige) if anzeige is not None else []}

    def _senden(self, *, force: bool) -> None:
        with self._senden_lock:
            with self._lock:
                event = self._ereignis()
            if not force and event == self._zuletzt:
                return
            now = self._clock()
            in_pause = self._pause_bis is not None and now < self._pause_bis
            if in_pause:
                if not force or self._erzwungen_versucht:
                    return
                self._erzwungen_versucht = True
            try:
                self._ha.fire_event(EVENT_TYPE, event)
            except Exception as error:
                self._zuletzt = None  # der naechste erlaubte Versuch sendet den aktuellen Stand
                if not in_pause:  # Fehler im Pausenversuch verlaengert die Pause nicht
                    if self._pause_bis is None:
                        logger.warning("Status-Ereignis nicht an Home Assistant gesendet (%s), neuer Versuch alle "
                                       "%s s", type(error).__name__, WIEDERHOLEN_SECONDS)
                    self._pause_bis, self._erzwungen_versucht = now + WIEDERHOLEN_SECONDS, False
                return
            if self._pause_bis is not None:
                logger.info("Status-Ereignis wieder an Home Assistant gesendet")
                self._pause_bis, self._erzwungen_versucht = None, False
            self._zuletzt = event
