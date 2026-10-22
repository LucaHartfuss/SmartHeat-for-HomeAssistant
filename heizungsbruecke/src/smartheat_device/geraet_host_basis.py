"""Gemeinsamer Teil der Geraete-Hosts (Audit 5 A5-26; Spec Stufe 3 S6 4.2): Bindung des Regel-Workers, Nachstellen von
Neustart und Bedienung, die vor dem Binden kamen, gemerkte Ursprungswerte, die Sicht der Laufzeit auf die
Soll-Dokumente, Bedienwunsch, Aufraeum-Markierung und das Aufheben der Abmelde-Markierung (5b-D2-7). HaDeviceHost und
GatewayDeviceHost erben davon und liefern nur ihre Hostteile (Faehigkeiten, Uhr, Treiberpruefung, Befehle, Anzeige,
Software, Pfade der Markierungen). Ebenfalls hier, weil beide Hosts es gleich brauchen (A6-60): die Konfiguration der
Laufzeit beim Start (laufzeit_konfiguration, aus der beim Start einmal gelesenen Aufraeum-Markierung), die Ursprungswerte
des Servers (ursprungswerte_server: die Markierung hat Vorrang vor einer neuen Einrichtung) und das Warten von sign_off
auf den Status "abgemeldet" (abmeldung_abwarten, wiederherstellungspunkt, SIGN_OFF_WAIT_SECONDS). Rueckrufe des
Geraetekerns laufen im Geraete-Thread, worker_binden im Hauptthread; die Sperre trennt beide. Kein Rueckruf wirft (V2,
Konformitaetssuite testhilfen/host_konformitaet.py) ausser konfiguration_vorbereiten: scheitert dort das Schreiben der
Markierung (vor dem Speichern), geht der OSError an den Dokumentspeicher, und das Geraet haelt das Dokument zurueck
(Spec Testoffensive 12.6, Nr. 38)."""
import threading
from collections.abc import Callable
from pathlib import Path

from smartheat_core import dateien
from smartheat_device.commands import Done, Waiting
from smartheat_device.hintergrund import Hintergrund
from smartheat_device.host import Anzeige, MeldungSink
from smartheat_runtime import aufraeumen, dokument_config
from smartheat_runtime.soll_eingang import EV_TARGET_INPUT
from smartheat_runtime.soll_speicher import SOURCE_PORTAL
from smartheat_runtime.status import STATUS_ABGEMELDET
from smartheat_runtime.worker import Event, RegulationWorker

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
        #: (prozess.aufraeumen_beim_start); der Host setzt sie vor dem Start der Laufzeit. Jeder Neustart beginnt einen
        #: frischen Prozess, sie veraltet im Lauf nie.
        self.start_markierung: aufraeumen.Markierung | None = None
        self.letzte_anzeige: Anzeige | None = None
        #: blockierende Host-Aufrufe der Befehle neben dem Geraete-Thread; prozess.beenden stoppt ihn
        self.hintergrund = Hintergrund()
        #: Meldungen der Laufzeit an den Geraetekern (up/notifications); einer je Prozess
        self.notify_sink = MeldungSink(self._melden)
        self._worker: RegulationWorker | None = None
        self._soll_eingang = True  # False im Ruhezustand (IdleBridge): der Worker hat keinen Soll-Eingang
        self._lock = threading.Lock()  # binden (Hauptthread) gegen Rueckrufe (Geraete-Thread)
        self._neustart = False
        self._ursprungswerte: dict = {}  # Hauptthread schreibt, Geraet liest
        self._raum_soll: float | None = None  # letzte Bedienung, bevor der Worker gebunden ist

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

    def worker_binden(self, worker: RegulationWorker, *, soll_eingang: bool = True) -> None:
        """soll_eingang=False im Ruhezustand (IdleBridge, kein Trigger-Eingang): Bedienungen gehen nicht an den Worker
        (sonst je Dokument "Kein Handler ... verworfen"); sie bleiben im Geraetekern gespeichert und gelten beim
        naechsten Start mit Regelung. Neustart und letzte Bedienung aus der Zeit vor dem Binden werden nachgestellt."""
        with self._lock:
            self._worker = worker
            self._soll_eingang = soll_eingang
            if self._raum_soll is not None and soll_eingang:
                worker.post(Event(EV_TARGET_INPUT, {"value": self._raum_soll, "source": SOURCE_PORTAL}))
            self._raum_soll = None
            if self._neustart:
                worker.post(Event(EV_RELOAD))

    def ursprungswerte_merken(self, werte: dict) -> None:
        with self._lock:
            self._ursprungswerte = dict(werte)

    def ursprungswerte(self) -> dict:
        with self._lock:
            return dict(self._ursprungswerte)

    def neustart_anfordern(self) -> None:
        with self._lock:
            self._neustart = True
            if self._worker is not None:
                self._worker.post(Event(EV_RELOAD))

    def _melden(self, key: str, text: str, *, offen: bool, kritisch: bool) -> None:
        if self.device is not None:
            self.device.meldung(key, text, offen=offen, kritisch=kritisch)

    # --- Sicht der Laufzeit ---

    def konfiguration(self) -> dict | None:
        return self.device.konfiguration() if self.device is not None else None

    def bedienung(self) -> tuple[dict | None, bool]:
        if self.device is None:
            return None, False
        doc = self.device.documents.aktuell("bedienung")
        return self.device.bedienung(), bool(doc is not None and doc.dirty)

    def laufzeit_konfiguration(self) -> tuple[dict, bool]:
        """(Konfiguration fuer die Laufzeit, im Modus abgemeldet starten?), Spec Stufe 3 S3 Abschnitt 4. Steht nach einer
        leeren Konfiguration das Zuruecksetzen aus (start_markierung) und ist die gespeicherte Konfiguration noch nicht
        eingerichtet, die alte Konfiguration aus der Markierung; sonst die gespeicherte (leer: {})."""
        gespeichert = self.konfiguration()
        alt = self.start_markierung.konfiguration if self.start_markierung is not None else None
        if alt is not None and not dokument_config.eingerichtet(gespeichert):
            return dict(alt), True
        return dict(gespeichert or {}), False

    def ursprungswerte_server(self, konfiguration: dict, leer_abmelden: bool) -> dict:
        """Die Ursprungswerte, die der Server fuer die Wiederherstellung kennt; konfiguration und leer_abmelden stammen
        aus laufzeit_konfiguration. Steht das Zuruecksetzen nach einer leeren Konfiguration aus, gelten die Werte der
        Markierung -- auch wenn inzwischen eine neue Einrichtung (womoeglich einer anderen Anlage) eingetroffen ist
        (die Markierung hat Vorrang); sonst die aktuelle Konfiguration, solange sie eingerichtet ist."""
        if leer_abmelden:
            return dokument_config.ursprungswerte(konfiguration)
        aktuell = self.konfiguration()
        return dokument_config.ursprungswerte(aktuell if dokument_config.eingerichtet(aktuell) else konfiguration)

    def abo_status(self) -> str:
        return self.device.abo_status() if self.device is not None else dokument_config.abo_status(None)

    def wunsch(self, wert: float, herkunft: str | None = None, durch: str | None = None) -> float:
        """Bedienwunsch (HA: Soll-Entity oder smartheat_wunsch; Gateway: Thermostat): begrenzt vom Geraetekern, der ihn
        sendet; ohne Geraet unveraendert."""
        if self.device is None:
            return wert
        return self.device.wunsch(wert, herkunft=herkunft or self.wunsch_herkunft, durch=durch)

    def soll_quelle_aus(self, aus: bool, wirksam: float | None) -> None:
        if self.device is not None:
            self.device.soll_quelle_aus(aus, wirksam)

    # --- Rueckrufe des Geraetekerns (DeviceHost) ---

    def konfiguration_vorbereiten(self, alt: dict | None, neu: dict) -> None:
        if dokument_config.eingerichtet(alt) and not dokument_config.eingerichtet(neu):
            assert alt is not None
            aufraeumen.markieren(self.aufraeumen_pfad(), alt)  # vor dem Speichern (Spec S3 4, Testoffensive 12.6)

    def konfiguration_uebernommen(self, alt: dict | None, neu: dict) -> None:
        """Neustart bei laufzeitrelevanter Aenderung und wenn eine neue, nicht leere Konfiguration derselben Anlage die
        Abmelde-Markierung aufhebt (5b-D2-7: das Geraet folgt dem Server und verlaesst "abgemeldet"). Eine leere
        Konfiguration oder eine andere Anlage laesst die Markierung stehen; dieselbe Version erneut ruft hier nicht.
        Kein Neustart, solange weder die alte noch die neue Konfiguration eingerichtet ist (L4-02): der Ruhezustand
        "nicht eingerichtet" liest kein Laufzeitfeld, und die erste leere Konfiguration nach dem Erstkontakt risse
        sonst einen laufenden Befehl mit."""
        aufgehoben = (dokument_config.eingerichtet(neu)
                      and dokument_config.abmeldung_aufheben(self.abgemeldet_pfad(), neu["anlage"]))
        einrichtung_beteiligt = dokument_config.eingerichtet(alt) or dokument_config.eingerichtet(neu)
        if aufgehoben or (einrichtung_beteiligt and dokument_config.laufzeit_relevant(alt, neu)):
            self.neustart_anfordern()

    def bedienung_uebernommen(self, inhalt: dict) -> None:
        value = inhalt.get("raum_soll")
        if not isinstance(value, int | float) or isinstance(value, bool):
            return
        with self._lock:
            if self._worker is None:
                self._raum_soll = float(value)  # worker_binden stellt die letzte nach
            elif self._soll_eingang:
                self._worker.post(Event(EV_TARGET_INPUT, {"value": float(value), "source": SOURCE_PORTAL}))
