"""Status-Ereignis des HA-Add-ons: smartheat_status Schema 4 = die Felder der Anlage aus dem Laufzeit-Status
(Schema 2, smartheat_runtime.status, unveraendert fuer Gateway und up/status), Geraet (ID, Zustand, Verbindung),
Anlagenname, Version des Konfigurationsdokuments und dessen kritische Mindestversion der Integration, Hinweise fuer die
Reparatur-Hinweise der Integration, dazu `kreise`: je Kreis der Konfiguration
Name, Raum (Ist, wirksamer Soll aus dem Soll-Speicher), die Kreisfelder der Laufzeit (Boost, Abo, Hebel, Zustand,
Kaelteschutz ...) und die Felder des Anzeige-Dokuments. Nie der Uebernahme-Code (der steht nur in der Discovery):
ins Ereignis kommen nur die benannten Felder, nie ein ganzes Objekt der Quellen. Der Kreisname ist ein
Kundendatum: er steht nur in diesem lokalen Ereignis, nie in Log, Topic, Push oder Alarm. Gesendet bei jedem Ereignis der Laufzeit (auch dem
Lebenszeichen alle 300 s) und bei einer inhaltlichen Aenderung der Anzeige. Felder sind Cross-Repo-Vertrag mit der
Integration (Contract-Check)."""
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping

from heizungsbruecke.ha_sinks import EVENT_TYPE
from heizungsbruecke.version import ADDON_VERSION
from smartheat_core import kreisansicht
from smartheat_device.documents import Dokument
from smartheat_device.host import Anzeige
from smartheat_runtime import status

logger = logging.getLogger(__name__)

EVENT_SCHEMA = 4
GERAET_FIELDS = ("geraet_id", "geraet_zustand", "server_verbunden", "geraet_hinweise", "anlage_name",
                 "konfig_version", "mindest_integration")
#: Form der kritischen Mindestversion der Integration im Ereignis (wie die Pruefung der Integration): drei Zahlen,
#: hoechstens eine Endung, hoechstens 32 Zeichen; was nicht passt, geht nie ins Ereignis (kein Freitext des Servers).
MINDEST_INTEGRATION_RE = re.compile(r"(?=.{1,32}\Z)\d{1,6}\.\d{1,6}\.\d{1,6}(?:[.+-][0-9A-Za-z]{1,16})?")
#: Felder der Anlage im Ereignis und Felder eines Kreises: die Kreisfelder der Laufzeit plus die lokalen.
LOKAL_KREIS_FIELDS = ("name", "raum_soll", "raum_ist", "einlernphase_tag", "testphase_bis", "befunde")
EVENT_FIELDS = (*status.ANLAGE_FELDER, *GERAET_FIELDS, "kreise")
KREIS_EVENT_FIELDS = ("kreis", *LOKAL_KREIS_FIELDS, *status.KREIS_FELDER)
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


def _zahl(wert) -> float | None:
    return float(wert) if isinstance(wert, int | float) and not isinstance(wert, bool) else None


def _version(dokument: Dokument | None) -> int | None:
    version = dokument.version if dokument is not None else None
    return version if isinstance(version, int) and not isinstance(version, bool) else None


def _mindest_integration(konf: Mapping | None) -> str | None:
    wert = konf.get("mindest_integration") if isinstance(konf, Mapping) else None
    return wert if isinstance(wert, str) and MINDEST_INTEGRATION_RE.fullmatch(wert) else None


def lokale_sicht(konfiguration: Callable[[], Dokument | None], soll: Callable[[int], float | None],
                 raum_ist: Callable[[int], float | None], anzeige: Callable[[], Mapping | None]) -> Callable[[], dict]:
    """Die lokale Sicht des Ereignisses: Anlagenname, Version des Konfigurationsdokuments und dessen kritische
    Mindestversion der Integration (beide null ohne Dokument bzw. ohne gueltigen Wert) und je Kreis der Konfiguration
    Name, Soll (Soll-Speicher), Ist des Raums und die Felder des Anzeige-Dokuments (heute ohne Befund: null/null/[]).
    Version und Inhalt kommen aus einem Dokument, damit sie zusammenpassen. Liest nur, ohne Sperre."""
    def sicht() -> dict:
        dokument_konf = konfiguration()
        konf = dokument_konf.inhalt if dokument_konf is not None else None
        dokument = anzeige()
        eintraege = dokument.get("kreise") if isinstance(dokument, Mapping) else None
        je_kreis = {e.get("kreis"): e for e in eintraege if isinstance(e, Mapping)} if isinstance(eintraege, list) else {}
        kreise = {}
        for eintrag in kreisansicht.kreise(konf):
            nummer = eintrag.get("kreis")
            if not isinstance(nummer, int) or isinstance(nummer, bool):
                continue
            angezeigt = je_kreis.get(nummer, {})
            befunde = angezeigt.get("befunde")
            name = eintrag.get("name")
            kreise[nummer] = {
                "name": name if isinstance(name, str) else None, "raum_soll": _zahl(soll(nummer)),
                "raum_ist": _zahl(raum_ist(nummer)), "einlernphase_tag": angezeigt.get("einlernphase_tag"),
                "testphase_bis": angezeigt.get("testphase_bis"), "befunde": list(befunde) if isinstance(befunde, list) else [],
            }
        name = konf.get("anlage_name") if isinstance(konf, Mapping) else None
        return {"anlage_name": name if isinstance(name, str) else None, "konfig_version": _version(dokument_konf),
                "mindest_integration": _mindest_integration(konf), "kreise": kreise}
    return sicht


class GeraetStatus:
    """Sendet nie zwei Ereignisse zugleich (_senden_lock) und ruft HA ausserhalb der Zustandssperre (_lock) auf: jedes
    Ereignis ist ein stimmiger Schnappschuss, und laufzeit_merken (sign_off) wartet nie auf HA. Ist HA nicht
    erreichbar (startup: services startet das Add-on vor HA Core), versucht der Takt des Geraete-Threads es erst nach
    WIEDERHOLEN_SECONDS erneut, ein Laufzeit-Ereignis in dieser Pause hoechstens einmal je Pause; eine Warnung je
    Ausfall (nur die Fehlerart), eine Info-Zeile bei der Erholung. Nach einem Fehler sendet der naechste erlaubte
    Versuch den aktuellen Stand, auch wenn er sich nicht geaendert hat."""

    def __init__(self, ha_api, geraet_id: str, lokal: Callable[[], Mapping], *,
                 laufzeit_merken: Callable[[dict], None], clock: Callable[[], float] = time.monotonic) -> None:
        """`lokal` liefert die lokale Sicht (`lokale_sicht`); es wird ausserhalb der Zustandssperre gerufen."""
        self._ha, self._geraet_id, self._lokal, self._merken = ha_api, geraet_id, lokal, laufzeit_merken
        self._clock = clock
        self._lock = threading.Lock()  # Zustand: Laufzeit-Ereignis und Anzeige
        self._senden_lock = threading.Lock()  # ein Sender zur Zeit; die Felder darunter nur mit ihm
        self._laufzeit: dict = {**dict.fromkeys(status.ANLAGE_FELDER), "status": status.STATUS_STARTET,
                                "addon_version": ADDON_VERSION}
        self._laufzeit_kreise: dict[int, dict] = {}
        self._anzeige: Anzeige | None = None
        self._zuletzt: dict | None = None
        self._pause_bis: float | None = None  # nach einem Fehler: vorher kein Versuch des Takts
        self._erzwungen_versucht = False  # ein Laufzeit-Ereignis in dieser Pause schon versucht

    def publish(self, event: dict) -> None:
        """StatusSink der Laufzeit (Worker-Thread). Wirft nicht: Fehler und Wiederholung regelt GeraetStatus (der
        Takt sendet nach der Pause den aktuellen Stand). Das flache Laufzeit-Ereignis ohne Kreisliste (tenant_id,
        status, grund fuer sign_off) geht an laufzeit_merken."""
        kreise = event.get("kreise")
        with self._lock:
            self._laufzeit = {key: event.get(key) for key in status.ANLAGE_FELDER}
            self._laufzeit_kreise = {
                eintrag["kreis"]: {key: eintrag.get(key) for key in status.KREIS_FELDER}
                for eintrag in (kreise if isinstance(kreise, list) else []) if isinstance(eintrag, dict)}
            self._merken(status.ohne_kreise(event))
        self._senden(force=True)

    def anzeige(self, anzeige: Anzeige) -> None:
        """Jeder Takt des Geraete-Threads; gesendet nur bei Aenderung (oder nach einem Fehler, nach der Pause)."""
        with self._lock:
            self._anzeige = anzeige
        self._senden(force=False)

    def _ereignis(self, lokal: Mapping) -> dict:
        anzeige = self._anzeige
        lokale_kreise = lokal["kreise"]
        kreise = [{
            "kreis": nummer,
            **{feld: lokale_kreise.get(nummer, {}).get(feld, [] if feld == "befunde" else None)
               for feld in LOKAL_KREIS_FIELDS},
            **{feld: self._laufzeit_kreise.get(nummer, {}).get(feld) for feld in status.KREIS_FELDER},
        } for nummer in sorted({*lokale_kreise, *self._laufzeit_kreise})]
        return {**self._laufzeit, "schema": EVENT_SCHEMA, "geraet_id": self._geraet_id,
                "geraet_zustand": anzeige.zustand if anzeige is not None else "startet",
                "server_verbunden": anzeige.server_ok if anzeige is not None else False,
                "geraet_hinweise": hinweise(anzeige) if anzeige is not None else [],
                "anlage_name": lokal["anlage_name"], "konfig_version": lokal["konfig_version"],
                "mindest_integration": lokal["mindest_integration"], "kreise": kreise}

    def _senden(self, *, force: bool) -> None:
        with self._senden_lock:
            lokal = self._lokal()  # ausserhalb der Zustandssperre: liest Konfiguration und Soll-Speicher
            with self._lock:
                event = self._ereignis(lokal)
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
