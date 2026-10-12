"""Status-Ereignis des HA-Add-ons ab 1.0.0 (Spec 5c 1.1/1.3): smartheat_status Schema 3 = der Laufzeit-Status (Schema 2,
smartheat_runtime.status, unveraendert fuer Gateway und up/status) plus Geraet (ID, Zustand, Verbindung), wirksamer
Raum-Soll (Soll-Speicher) und Hinweise fuer die Reparatur-Hinweise der Integration. Nie der Uebernahme-Code (der steht
nur in der Discovery): ins Ereignis kommen nur die benannten Felder, nie ein ganzes Objekt der Quellen. Gesendet bei
jedem Ereignis der Laufzeit (auch dem Lebenszeichen alle 300 s) und bei einer inhaltlichen Aenderung der Anzeige.
Felder sind Cross-Repo-Vertrag mit der Integration (Contract-Check, Plan W)."""
import logging
import threading
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
ZUSTAND_TEXTE = {
    "update_noetig": "Das Add-on ist zu alt für den SmartHeat-Dienst. Bitte das Add-on aktualisieren.",
    "gesperrt": "Dieses Gerät ist beim SmartHeat-Dienst gesperrt. Bitte den Support kontaktieren.",
}


def hinweise(anzeige: Anzeige) -> list[dict]:
    """Hinweise der Anzeige als {key, stufe, text}: Hinweise des Servers, eigene Meldungen des Geraets, abgelehnte
    Konfiguration und die Zustaende update_noetig/gesperrt. Bei update_noetig mit abgelehnter Konfiguration stehen
    bewusst beide Hinweise da (Brief-Test): der Grund der Ablehnung und die Handlungsanweisung."""
    result = [{"key": str(h["key"]), "stufe": h.get("stufe") or "warnung", "text": h.get("text") or ""}
              for h in anzeige.hinweise if isinstance(h, dict) and h.get("key")]
    result += [{"key": str(m["key"]), "stufe": "warnung", "text": m.get("text") or ""}
               for m in anzeige.meldungen if isinstance(m, dict) and m.get("key")]
    if anzeige.konfiguration_abgelehnt:
        result.append({"key": KONFIGURATION_ABGELEHNT, "stufe": "kritisch", "text": anzeige.konfiguration_abgelehnt})
    if anzeige.zustand in ZUSTAND_TEXTE:
        result.append({"key": anzeige.zustand, "stufe": "kritisch", "text": ZUSTAND_TEXTE[anzeige.zustand]})
    return result


class GeraetStatus:
    def __init__(self, ha_api, geraet_id: str, raum_soll: Callable[[], float | None], *,
                 laufzeit_merken: Callable[[dict], None]) -> None:
        self._ha, self._geraet_id, self._raum_soll, self._merken = ha_api, geraet_id, raum_soll, laufzeit_merken
        self._lock = threading.Lock()
        self._laufzeit: dict = {**dict.fromkeys(status.EVENT_FIELDS), "status": status.STATUS_STARTET,
                                "addon_version": ADDON_VERSION}
        self._anzeige: Anzeige | None = None
        self._zuletzt: dict | None = None

    def publish(self, event: dict) -> None:
        """StatusSink der Laufzeit (Worker-Thread). Darf werfen (der Reporter sendet beim naechsten Anlass erneut).
        Das unveraenderte Laufzeit-Ereignis (tenant_id, status, grund fuer sign_off) geht an laufzeit_merken."""
        with self._lock:
            self._laufzeit = {key: event.get(key) for key in status.EVENT_FIELDS}
            self._merken(dict(event))
            self._senden(force=True)

    def anzeige(self, anzeige: Anzeige) -> None:
        """Jeder Takt des Geraete-Threads; gesendet nur bei Aenderung."""
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
        event = self._ereignis()
        if not force and event == self._zuletzt:
            return
        self._ha.fire_event(EVENT_TYPE, event)
        self._zuletzt = event
