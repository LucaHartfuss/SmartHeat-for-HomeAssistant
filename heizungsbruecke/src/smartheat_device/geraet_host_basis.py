"""Gemeinsamer Teil der Geraete-Hosts: Bindung des Regel-Workers, Nachstellen von
Neustart und Bedienung, die vor dem Binden kamen, gemerkte Ursprungswerte, die Sicht der Laufzeit auf die
Soll-Dokumente, Bedienwunsch, Aufraeum-Markierung und das Aufheben der Abmelde-Markierung. HaDeviceHost und
GatewayDeviceHost erben davon und liefern nur ihre Hostteile (Faehigkeiten, Uhr, Treiberpruefung, Befehle, Anzeige,
Software, Pfade der Markierungen). Ebenfalls hier, weil beide Hosts es gleich brauchen: die Konfiguration der
Laufzeit beim Start (laufzeit_konfiguration, aus der beim Start einmal gelesenen Aufraeum-Markierung), die Ursprungswerte
des Servers (ursprungswerte_server: die Markierung hat Vorrang vor einer neuen Einrichtung) und das Warten von sign_off
auf den Status "abgemeldet" (abmeldung_abwarten, wiederherstellungspunkt, SIGN_OFF_WAIT_SECONDS). Rueckrufe des
Geraetekerns laufen im Geraete-Thread, worker_binden im Hauptthread; die Sperre trennt beide. Kein Rueckruf wirft (V2,
Konformitaetssuite testhilfen/host_konformitaet.py) ausser konfiguration_vorbereiten: scheitert dort das Schreiben der
Markierung (vor dem Speichern), geht der OSError an den Dokumentspeicher, und das Geraet haelt das Dokument zurueck
(Nr. 38)."""
import logging
import threading
from collections.abc import Callable
from pathlib import Path

from smartheat_core import dateien
from smartheat_device.commands import Done, Waiting
from smartheat_device.hintergrund import Hintergrund
from smartheat_device.host import Anzeige, MeldungSink
from smartheat_runtime import aufraeumen, dokument_config
from smartheat_runtime.soll_eingang import EV_TARGET_INPUT
from smartheat_runtime.soll_speicher import KREIS, SOURCE_PORTAL, raum_soll_von
from smartheat_runtime.status import STATUS_ABGEMELDET
from smartheat_runtime.worker import Event, RegulationWorker

logger = logging.getLogger(__name__)

#: Neustart der Laufzeit (neue laufzeitrelevante Konfiguration, sign_off); prozess.reload_anhaengen haengt ihn an.
EV_RELOAD = "geraet_reload"
#: So lange wartet sign_off auf den Status "abgemeldet" (Frist des Befehls, beide Hosts).
SIGN_OFF_WAIT_SECONDS = 24 * 3600


def wiederherstellungspunkt(backup_pfad: Path) -> dict:
    """Der Wiederherstellungspunkt aus der Sicherung (restore_point), das Ergebnis `werte` von sign_off; fehlt er oder
    ist die Sicherung kaputt, {}."""
    backup = dateien.lesen_json(backup_pfad)
    punkt = backup.get("restore_point") if isinstance(backup, dict) else None
    return dict(punkt) if isinstance(punkt, dict) else {}


def abmeldung_abwarten(anlage: str, runtime_status: Callable[[], dict | None], backup_pfad: Path,
                       frist_bis: float) -> Waiting:
    """Der Wartezustand von sign_off (beide Hosts): fertig erst, wenn der Status der Laufzeit "abgemeldet" fuer diese
    Anlage zeigt (ein anderer Status oder eine andere Anlage wartet weiter); zurueckgesetzt ist, wer ohne `grund`
    abgemeldet ist, werte der Wiederherstellungspunkt. frist_bis: Ende des Wartens auf der Uhr des Hosts
    (Uhr + SIGN_OFF_WAIT_SECONDS)."""

    def check() -> Done | None:
        status = runtime_status()
        if not status or status.get("status") != STATUS_ABGEMELDET or status.get("tenant_id") != anlage:
            return None
        return Done({"zurueckgesetzt": status.get("grund") is None, "werte": wiederherstellungspunkt(backup_pfad)})

    return Waiting(check, frist_bis)


class GeraetHostBasis:
    host: str
    version: str
    #: herkunft eines Bedienwunschs ohne eigene Angabe (HA: Soll-Entity, Gateway: Thermostat)
    wunsch_herkunft: str

    def __init__(self) -> None:
        self.device = None  # smartheat_device.device.Device, nach dem Anlegen per verbinden()
        #: Die beim Start einmal gelesene Aufraeum-Markierung, wenn das Zuruecksetzen aussteht
        #: (prozess.aufraeumen_beim_start); der Host setzt sie vor dem Start der Laufzeit. Eine spaeter geschriebene
        #: Markierung kommt immer mit einer Neustart-Anforderung (konfiguration_uebernommen), der naechste Prozess
        #: liest sie.
        self.start_markierung: aufraeumen.Markierung | None = None
        self.letzte_anzeige: Anzeige | None = None
        #: blockierende Host-Aufrufe der Befehle neben dem Geraete-Thread; prozess.beenden stoppt ihn
        self.hintergrund = Hintergrund()
        #: Meldungen der Laufzeit an den Geraetekern (up/notifications); einer je Prozess
        self.notify_sink = MeldungSink(self._melden)
        self._workers: dict[int, RegulationWorker] = {}  # je Kreis der Worker seiner Laufzeit
        #: False im Ruhezustand (IdleBridge): der Worker des Kreises hat keinen Soll-Eingang
        self._soll_eingaenge: dict[int, bool] = {}
        self._lock = threading.Lock()  # binden (Hauptthread) gegen Rueckrufe (Geraete-Thread)
        self._neustart = False
        self._ursprungswerte: dict[int, dict] = {}  # je Kreis; Hauptthread schreibt, Geraet liest
        self._raum_soll: dict[int, float] = {}  # je Kreis die letzte Bedienung, bevor sein Worker gebunden ist
        #: Die Laufzeit der Anlage (kreise.Laufzeit): entscheidet je Konfigurationsaenderung, ob einzelne Kreise
        #: wieder anlaufen oder der Prozess neu startet. Ihre blockierende Wiederaufnahme laeuft in diesem eigenen
        #: Thread, nie im Geraete-Thread und nie hinter den Befehlen.
        self._laufzeit = None
        self._konfig_hintergrund = Hintergrund()

    def aufraeumen_pfad(self) -> Path:
        """Wohin konfiguration_vorbereiten die Markierung aufraeumen.json schreibt (Host)."""
        raise NotImplementedError

    def abgemeldet_pfad(self) -> Path:
        """Wo sign_off die Abmelde-Markierung abgemeldet.json schreibt (Host); konfiguration_uebernommen hebt sie
        auf."""
        raise NotImplementedError

    # --- Verdrahtung ---

    def verbinden(self, device) -> None:
        self.device = device

    def worker_binden(self, worker: RegulationWorker, *, kreis: int = KREIS, soll_eingang: bool = True) -> None:
        """Bindet den Worker des Kreises `kreis` (ein neuer ersetzt den alten, so nach einer Wiederaufnahme).
        soll_eingang=False im Ruhezustand (IdleBridge, kein Trigger-Eingang): Bedienungen gehen nicht an den Worker
        (sonst je Dokument "Kein Handler ... verworfen"); sie bleiben im Geraetekern gespeichert und gelten beim
        naechsten Start mit Regelung. Neustart und letzte Bedienung des Kreises aus der Zeit vor dem Binden werden
        nachgestellt."""
        with self._lock:
            self._workers[kreis] = worker
            self._soll_eingaenge[kreis] = soll_eingang
            gemerkt = self._raum_soll.pop(kreis, None)
            if gemerkt is not None and soll_eingang:
                worker.post(Event(EV_TARGET_INPUT, {"value": gemerkt, "source": SOURCE_PORTAL}))
            if self._neustart:
                worker.post(Event(EV_RELOAD))

    def laufzeit_binden(self, laufzeit) -> None:
        """Die Laufzeit der Anlage, der konfiguration_uebernommen jede neue Konfiguration vorlegt."""
        self._laufzeit = laufzeit

    def ursprungswerte_merken(self, werte: dict, kreis: int = KREIS) -> None:
        with self._lock:
            self._ursprungswerte[kreis] = dict(werte)

    def ursprungswerte(self) -> dict:
        """Gemeldet wird nur Kreis 1: der Statuskoerper hat eine Ursprungswerte-Tabelle je Anlage; die Namen anderer
        Kreise wuerden ihr sonst falsch zugeordnet."""
        with self._lock:
            return dict(self._ursprungswerte.get(KREIS, {}))

    def neustart_anfordern(self) -> None:
        """Neustart des Prozesses: EV_RELOAD an jeden gebundenen Worker, spaeter gebundene bekommen ihn beim Binden."""
        with self._lock:
            self._neustart = True
            for worker in self._workers.values():
                worker.post(Event(EV_RELOAD))

    def _melden(self, key: str, text: str, *, offen: bool, stufe: str) -> None:
        if self.device is not None:
            self.device.meldung(key, text, offen=offen, stufe=stufe)

    # --- Sicht der Laufzeit ---

    def konfiguration(self) -> dict | None:
        return self.device.konfiguration() if self.device is not None else None

    def bedienung(self) -> tuple[dict | None, bool]:
        if self.device is None:
            return None, False
        doc = self.device.documents.aktuell("bedienung")
        return self.device.bedienung(), bool(doc is not None and doc.dirty)

    def laufzeit_konfiguration(self) -> tuple[dict, bool]:
        """(Konfiguration fuer die Laufzeit, im Modus abgemeldet starten?). Steht nach einer
        leeren Konfiguration das Zuruecksetzen aus (start_markierung) und ist die gespeicherte Konfiguration noch nicht
        eingerichtet, die alte Konfiguration aus der Markierung; sonst die gespeicherte (leer: {})."""
        gespeichert = self.konfiguration()
        alt = self.start_markierung.konfiguration if self.start_markierung is not None else None
        if alt is not None and not dokument_config.eingerichtet(gespeichert):
            return dict(alt), True
        return dict(gespeichert or {}), False

    def ursprungswerte_server(self, konfiguration: dict, leer_abmelden: bool, kreis: int | None = None) -> dict:
        """Die Ursprungswerte, die der Server fuer die Wiederherstellung kennt; konfiguration und leer_abmelden stammen
        aus laufzeit_konfiguration. Steht das Zuruecksetzen nach einer leeren Konfiguration aus, gelten die Werte der
        Markierung -- auch wenn inzwischen eine neue Einrichtung (womoeglich einer anderen Anlage) eingetroffen ist
        (die Markierung hat Vorrang); sonst die aktuelle Konfiguration, solange sie eingerichtet ist."""
        if leer_abmelden:
            return dokument_config.ursprungswerte(konfiguration, kreis)
        aktuell = self.konfiguration()
        return dokument_config.ursprungswerte(aktuell if dokument_config.eingerichtet(aktuell) else konfiguration, kreis)

    def abo_status(self, kreis: int | None = None) -> str:
        return self.device.abo_status(kreis) if self.device is not None else dokument_config.abo_status(None)

    def wunsch(self, wert: float, herkunft: str | None = None, durch: str | None = None, kreis: int = KREIS) -> float:
        """Bedienwunsch fuer den Kreis `kreis` (HA: Soll-Entity oder smartheat_wunsch; Gateway: Thermostat): begrenzt
        vom Geraetekern, der ihn sendet; ohne Geraet unveraendert."""
        if self.device is None:
            return wert
        return self.device.wunsch(wert, herkunft=herkunft or self.wunsch_herkunft, durch=durch, kreis=kreis)

    def soll_quelle_aus(self, aus: bool, wirksam: float | None) -> None:
        if self.device is not None:
            self.device.soll_quelle_aus(aus, wirksam)

    # --- Rueckrufe des Geraetekerns (DeviceHost) ---

    def konfiguration_vorbereiten(self, alt: dict | None, neu: dict) -> None:
        if dokument_config.eingerichtet(alt) and not dokument_config.eingerichtet(neu):
            assert alt is not None
            aufraeumen.markieren(self.aufraeumen_pfad(), alt)  # vor dem Speichern

    def konfiguration_uebernommen(self, alt: dict | None, neu: dict) -> None:
        """Neustart bei laufzeitrelevanter Aenderung und wenn eine neue, nicht leere Konfiguration derselben Anlage die
        Abmelde-Markierung aufhebt (das Geraet folgt dem Server und verlaesst "abgemeldet"). Eine leere
        Konfiguration oder eine andere Anlage laesst die Markierung stehen; dieselbe Version erneut ruft hier nicht.
        Kein Neustart, solange weder die alte noch die neue Konfiguration eingerichtet ist: der Ruhezustand
        "nicht eingerichtet" liest kein Laufzeitfeld, und die erste leere Konfiguration nach dem Erstkontakt risse
        sonst einen laufenden Befehl mit."""
        aufgehoben = (dokument_config.eingerichtet(neu)
                      and dokument_config.abmeldung_aufheben(self.abgemeldet_pfad(), neu["anlage"]))
        einrichtung_beteiligt = dokument_config.eingerichtet(alt) or dokument_config.eingerichtet(neu)
        if aufgehoben:
            self.neustart_anfordern()
        elif not einrichtung_beteiligt:
            return
        elif self._laufzeit is None:
            if dokument_config.laufzeit_relevant(alt, neu):
                self.neustart_anfordern()
        else:
            laufzeit = self._laufzeit
            try:
                sofort = laufzeit.prozessneustart_noetig(alt, neu)
            except Exception:
                logger.exception("Die Laufzeit konnte die neue Konfiguration nicht pruefen, Neustart")
                sofort = True
            if sofort:
                self.neustart_anfordern()
            else:
                self._konfig_hintergrund.submit(lambda: self._laufzeit_fragen(laufzeit, alt, neu))

    def _laufzeit_fragen(self, laufzeit, alt: dict | None, neu: dict) -> None:
        """Die Laufzeit nimmt Kreise mit wieder aktivem Abo oder geaenderter Bindung allein wieder auf; verlangt sie
        den Prozessneustart oder scheitert sie, startet der Prozess neu (sicherer Weg)."""
        try:
            neustart = laufzeit.konfiguration_geaendert(alt, neu)
        except Exception:
            logger.exception("Die Laufzeit konnte die neue Konfiguration nicht anwenden, Neustart")
            neustart = True
        if neustart:
            self.neustart_anfordern()

    def bedienung_uebernommen(self, inhalt: dict) -> None:
        """Je Kreis der Raum-Soll seines Eintrags: an den Worker des Kreises, vor dem Binden gemerkt. Ein Kreis, den die
        Bedienung nicht nennt oder dessen Wert keine Zahl ist, bleibt unberuehrt."""
        with self._lock:
            eintraege = inhalt.get("kreise") if isinstance(inhalt, dict) else None
            genannt = [e["kreis"] for e in eintraege if isinstance(e, dict) and isinstance(e.get("kreis"), int)] \
                if isinstance(eintraege, list) else []
            kreise = {*self._workers, *self._raum_soll, *genannt}
            for kreis in sorted(kreise):
                value = raum_soll_von(inhalt, kreis)
                if not isinstance(value, int | float) or isinstance(value, bool):
                    continue
                worker = self._workers.get(kreis)
                if worker is None:
                    self._raum_soll[kreis] = float(value)  # worker_binden stellt die letzte nach
                elif self._soll_eingaenge.get(kreis, True):
                    worker.post(Event(EV_TARGET_INPUT, {"value": float(value), "source": SOURCE_PORTAL}))
