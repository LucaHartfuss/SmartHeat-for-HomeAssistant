"""Das Geraet (Spec 5b 5.1): verbindet Identitaet, Bootstrap, Link, Dokumente, Befehle, Bedienwunsch und Inventur und
laeuft in einem eigenen Thread (run). paho, Host und Laufzeit stellen nur Ereignisse in die Warteschlange; nur dieser
Thread aendert Zustand und ruft den Host.

Ablauf: Identitaet laden; ohne Zugang registrieren (Backoff); Link starten. Nach jedem Verbinden zuerst hello (schemata,
Versionen, Faehigkeiten, boot_id, uhr_synchron, bekannter Uebernahmestand; claim_state und new_claim_code setzen ihn,
Spec 5c F1), dann Status, gepufferter Wunsch und offene Meldungen. Host-Rueckrufe des Takts laufen ueber _host_wert
(5b-D1-4: ein werfender Rueckruf verschiebt Registrierung bzw. hello, laesst up/status dieses Takts aus (runtime_status,
raum), gilt bei uhr_synchron als None und bei ursprungswerte als {}, die Anzeige entfaellt; je Rueckruf und Fehlerart
eine Logzeile). Dokumente gehen an den Dokumentspeicher, das Ergebnis als up/result, das Uebernommene an den Host;
das Software-Angebot jeder Konfiguration mit bekanntem Schema (documents.angebot_aus) geht an den Host, auch wenn die
Konfiguration abgelehnt wurde (Plan-Praezisierung). Befehle gehen an das Register, down/setpoints an die Laufzeit.
Drei Anmeldefehler in Folge am Broker -> Rettungsweg (bootstrap.Bootstrap); gesperrt -> Link aus, neuer Versuch nach
6 h. up/status nur bei inhaltlicher Aenderung (ohne Zeitstempel, A4-35), hoechstens alle STATUS_MIN_SECONDS;
up/notifications gebuendelt hoechstens alle MELDUNGEN_MIN_SECONDS (Drossel des Dienstes: 20 Nachrichten/min, Plan
S2)."""
import json
import logging
import math
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from smartheat_core import config_check
from smartheat_device import bootstrap, commands, fehler, identity, inventory, nachrichten, wire, wish
from smartheat_device.documents import BEDIENUNG, KONFIGURATION, DocumentStore, angebot_aus
from smartheat_device.host import Anzeige, DeviceHost
from smartheat_device.laufzeit import RuntimeChannel
from smartheat_device.link import ANMELDUNG_SCHEITERT, GESENDET, GETRENNT, NACHRICHT, VERBUNDEN, Link
from smartheat_runtime import dokument_config
from smartheat_runtime.notifier import category
from smartheat_runtime.worker import RegulationWorker

logger = logging.getLogger(__name__)

DEVICE_DIR = "device"
TICK_SECONDS = 1.0
STATUS_MIN_SECONDS = 10
MELDUNGEN_MIN_SECONDS = 10
MELDUNGEN_JE_NACHRICHT = 50
SERVER_DOWN_SECONDS = 300
PRUEFUNG_WIEDERHOLEN_SECONDS = 30
#: Lokaler Hinweis bei einer abgelehnten Konfiguration (Spec Stufe 3 S5 6.3, Entscheidung E2 A): den Text der Ablehnung
#: bildet der Server, das Geraet nennt nur den Code.
ABLEHNUNG_ANZEIGE = "Die Einrichtung wurde abgelehnt (Code {grund}). Die Begründung zeigt der Einrichtungsassistent."
#: Down-Topic eines Dokuments -> Dokumentart (wire.DOKUMENT_TOPICS umgekehrt)
_DOKUMENT_ART = {topic: art for art, topic in wire.DOKUMENT_TOPICS.items()}
_RUNNING = ("regelt", "abo_inaktiv")
_FAULT = ("notbetrieb", "datenfehler", "konfigurationsfehler", "zugang_abgelehnt", "abo_beendet")
#: Laufzeitstatus, solange der Rueckruf runtime_status scheitert (nur fuer Zustand und Anzeige, nie im up/status):
#: wie eine startende Laufzeit, kein erfundener Betriebszustand.
_LAUFZEIT_UNBEKANNT = {"status": "startet"}
_T = TypeVar("_T")


def derive_zustand(*, gesperrt: bool, update_noetig: bool, server_seen: bool, server_down_seconds: float | None,
                   eingerichtet: bool, device_state: str | None, runtime_status: dict | None) -> str:
    """Zustand fuer LED, Seite und up/status (wire.ZUSTAENDE); die Reihenfolge ist der Vorrang."""
    if gesperrt:
        return "gesperrt"
    if update_noetig:
        return "update_noetig"
    if server_down_seconds is not None and server_down_seconds > SERVER_DOWN_SECONDS:
        return "keine_verbindung"
    if not server_seen:
        return "startet"
    if not eingerichtet:
        return "nicht_uebernommen" if device_state == "nicht_uebernommen" else "wartet_auf_einrichtung"
    status = (runtime_status or {}).get("status")
    if status in (None, "abgemeldet"):
        return "wartet_auf_einrichtung"
    if status in _RUNNING:
        return "regelt"
    if status in _FAULT:
        return "stoerung"
    return "startet"


def _iso(wall: float) -> str:
    return datetime.fromtimestamp(wall, UTC).isoformat(timespec="seconds")


def _ursprungswert_ok(wert) -> bool:
    """Wert in den Grenzen von V1 (strenger als der Server: kein bool, keine NaN/Unendlich)."""
    if isinstance(wert, bool):
        return False
    if isinstance(wert, int | float):
        # Betrag zuerst: math.isfinite wirft bei riesigen int (OverflowError); NaN faellt beim Vergleich durch.
        return abs(wert) <= wire.URSPRUNG_WERT_MAX and math.isfinite(wert)
    return isinstance(wert, str) and 0 < len(wert) <= wire.URSPRUNG_HEBEL_MAX_CHARS


def _ursprungsname_ok(name) -> bool:
    return isinstance(name, str) and 0 < len(name) <= wire.URSPRUNG_HEBEL_MAX_CHARS


def _vergleichbar(body: dict) -> dict:
    """Status ohne Zeitstempel (A4-35: raum traegt bei jeder Veroeffentlichung einen frischen ts)."""
    raum = body.get("raum")
    return {**body, "raum": {k: v for k, v in raum.items() if k != "ts"} if isinstance(raum, dict) else raum}


class Device:
    def __init__(self, host: DeviceHost, data_dir: Path, bootstrap_url: str, *,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
                 bootstrap_client: Callable[[identity.Identity], bootstrap.BootstrapClient] | None = None,
                 link_factory: Callable[..., Link] = Link,
                 befehl_fehler: Callable[[Exception], commands.Failed | None] = lambda error: None) -> None:
        self.host = host
        self._dir = data_dir / DEVICE_DIR
        self._clock, self._wall = clock, wall
        self._client_for = bootstrap_client or (
            lambda ident: bootstrap.BootstrapClient(bootstrap_url, ident, now=wall))
        self._link_factory = link_factory
        self._inbox: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self.identity = identity.load_or_create(self._dir)
        self.boot_id = identity.new_boot_id()
        self.documents = DocumentStore(self._dir, host=host.host, software_version=host.version,
                                       treiber_pruefen=host.treiber_pruefen,
                                       vor_speichern=host.konfiguration_vorbereiten)
        self.hinweise = commands.Hinweise(self._dir)
        self.befehle = commands.CommandRegister(self._dir, self._ergebnis_senden, send_inventory=self._inventur_senden,
                                                clock=clock, uebersetzen=befehl_fehler)
        self.befehle.register("hinweis", self.hinweise.handler)
        self.befehle.register("new_claim_code", self._new_claim_code)
        self.befehle.register("claim_state", self._claim_state)
        host.befehle_eintragen(self.befehle)
        self.wunsch_puffer = wish.WunschPuffer(self._dir)
        self._bootstrap = bootstrap.Bootstrap(
            self._dir, lambda: self._client_for(self.identity), register_args=self._register_args,
            uhr_synchron=self._uhr_synchron, identity=lambda: self.identity, clock=clock)
        self.link: Link | None = None
        self._bereit = False  # verbunden und hello gesendet
        self._hello_offen = False  # verbunden, hello noch nicht gesendet (Takt versucht es erneut)
        self._rettung = False
        self._channel: RuntimeChannel | None = None
        self._eigene: dict[str, str] = {}  # eigene Meldungen des Geraets: key -> Text
        self._meldungen: dict[str, dict] = {}  # offene Meldungen (Geraet und Laufzeit)
        self._meldungen_neu: dict[str, dict] = {}  # geaendert, noch nicht gesendet
        self._meldungen_at: float | None = None
        self._status: dict | None = None
        self._status_at: float | None = None
        self._hello_fehler: str | None = None  # letzte Fehlerart beim Bauen von hello (je Art einmal loggen)
        self._status_fehler: str | None = None  # letzter Vertragsverstoss beim Bauen (je Text einmal loggen)
        self._wunsch_fehler: str | None = None  # dito beim Bauen von up/wish
        self._wunsch_abgelehnt: set[str] = set()  # Gruende abgelehnter Bedienwuensche, schon geloggt (Host-Thread)
        self._publish_fehler: dict[str, str] = {}  # Topic -> letzter Text, mit dem der Link das Senden verweigerte
        self._ursprung_gewarnt: set[str] = set()  # nicht meldbare Ursprungswerte: je Name einmal loggen (Takt: 1 s)
        self._host_fehler: set[tuple[str, str]] = set()  # (Rueckruf, Fehlerart) gescheiterter Host-Werte, schon geloggt
        self._zurueckgehalten: dict[str, dict] = {}  # Dokument, dessen Pruefung vorlaeufig nicht moeglich war
        self._pruefung_at = 0.0
        self._started_at = clock()
        self._last_ok: float | None = None

    # --- oeffentlich (threadsicher, soweit nicht anders gesagt) ---

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run, name="geraet", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()
        link = self.link
        if link is not None:
            link.stop()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._inbox.get(timeout=TICK_SECONDS)
            except queue.Empty:
                item = None
            if self._stop.is_set():  # 5b-D2-4: nach stop() kein Takt mehr (er koennte einen neuen Link oeffnen)
                break
            self._sicher(item)

    def schritt(self) -> None:
        """Ein Durchlauf ohne Warten: alle anstehenden Ereignisse, dann der Takt (Tests, Szenarien)."""
        while True:
            try:
                item = self._inbox.get_nowait()
            except queue.Empty:
                break
            self._sicher(item, takt=False)
        self._sicher(None)

    def wunsch(self, wert, *, herkunft: str | None, durch: str | None = None) -> float:
        """Lokaler Sollwert vom Host: sofort begrenzt zurueck (der Host wendet ihn an), Versand im Geraete-Thread.
        ValueError, wenn der Wert keine endliche Zahl ist oder herkunft bzw. durch kein Vertragswert (dann kommt nichts
        in die Inbox). Geloggt wird je Grund einmal, ohne den Wert (der Host kann wiederholen)."""
        try:
            wish.pruefen_herkunft(herkunft, durch)
            begrenzt, roh = wish.begrenzen(wert)
        except ValueError as error:
            grund = str(error)  # feste Texte aus wish ohne Wert
            if grund not in self._wunsch_abgelehnt:
                self._wunsch_abgelehnt.add(grund)
                logger.error("Bedienwunsch verworfen (%s)", grund)
            raise
        self._inbox.put(("wunsch", begrenzt, roh, herkunft, durch))
        return begrenzt

    def soll_quelle_aus(self, aus: bool, wirksam: float | None = None) -> None:
        """Soll-Quelle aus (Thermostat aus/Frostschutz, Spec 3.2): kein Wunsch, nur die Meldung soll_quelle_aus. Ohne
        wirksames Soll bleibt sie offen, nur ohne Wert im Text; schliessen kann sie nur aus=False."""
        text = wish.soll_quelle_aus_text(wirksam) if aus else None
        self._inbox.put(("eigene", wish.SOLL_QUELLE_AUS_KEY, text))

    def meldung(self, key: str, text: str, *, offen: bool, kritisch: bool = True) -> None:
        """Meldung der Laufzeit (NotifySink des Hosts): Upsert je key ueber up/notifications."""
        self._inbox.put(("meldung", nachrichten.meldung(key=key, kategorie=category(key), kritisch=kritisch, text=text,
                                                        offen=offen, ts=_iso(self._wall()))))

    def ergebnis_software(self, version: str, ok: bool, grund: str | None = None) -> bool:
        """Ergebnis eines Updates (Spec 5.3: up/result mit dokument software, version = Software-Version). Nur nach
        hello; False = nicht gesendet (der Host versucht es spaeter erneut). Der Grund muss in SOFTWARE_ERRORS stehen;
        den Text bildet der Server."""
        if not ok and grund not in wire.SOFTWARE_ERRORS:
            raise ValueError(f"Grund {grund!r} steht nicht in SOFTWARE_ERRORS")
        error = None
        if not ok:
            assert grund is not None
            error = fehler.body(grund, None)
        body = nachrichten.ergebnis_dokument(wire.DOKUMENT_SOFTWARE, version, ok, error)
        return self._publish_nach_hello("up/result", body) is not None

    def laufzeit_kanal(self, worker: RegulationWorker) -> RuntimeChannel:
        """mqtt_factory fuer app.start (Spec 5b 5.2): Tick und Telemetrie ueber den Link."""
        channel = RuntimeChannel(self._publish_nach_hello,
                                 lambda: self._bereit and (link := self.link) is not None and link.connected,
                                 lambda: link.connect_failures if (link := self.link) is not None else 0)
        self._channel = channel
        channel.binden(worker)
        return channel

    def konfiguration(self) -> dict | None:
        return self.documents.inhalt(KONFIGURATION)

    def bedienung(self) -> dict | None:
        return self.documents.inhalt(BEDIENUNG)

    def abo_status(self) -> str:
        """abo_source der Laufzeit (dokument_config.runtime_config)."""
        return dokument_config.abo_status(self.konfiguration())

    def zustand(self) -> str:
        """Scheitert der Rueckruf runtime_status, gilt die Laufzeit als startend (_LAUFZEIT_UNBEKANNT)."""
        status, gescheitert = self._host_wert("runtime_status", self.host.runtime_status)
        return self._zustand(_LAUFZEIT_UNBEKANNT if gescheitert else status)

    def _zustand(self, runtime_status: dict | None) -> str:
        connected = (link := self.link) is not None and link.connected
        since = self._last_ok if self._last_ok is not None else self._started_at
        return derive_zustand(
            gesperrt=self._bootstrap.gesperrt, update_noetig=self.documents.update_noetig,
            server_seen=self._last_ok is not None,
            server_down_seconds=None if connected else self._clock() - since,
            eingerichtet=self.documents.eingerichtet(), device_state=bootstrap.device_state(self._dir),
            runtime_status=runtime_status)

    def anzeige(self) -> Anzeige:
        ablehnung = self.documents.ablehnung(KONFIGURATION)
        return Anzeige(
            zustand=self.zustand(), server_ok=(link := self.link) is not None and link.connected,
            device_state=bootstrap.device_state(self._dir), eingerichtet=self.documents.eingerichtet(),
            hinweise=self.hinweise.alle(),
            meldungen=tuple({"key": key, "text": text} for key, text in sorted(self._eigene.items())),
            konfiguration_abgelehnt=(ABLEHNUNG_ANZEIGE.format(grund=ablehnung[1]) if ablehnung is not None
                                     else None))

    # --- Geraete-Thread ---

    def _ereignis(self, art: str, *daten) -> None:
        """Rueckruf des Links (paho-Thread): nur einstellen."""
        self._inbox.put(("link", art, daten))

    def _sicher(self, item, *, takt: bool = True) -> None:
        try:
            if item is not None:
                self._verarbeiten(item)
            if takt:
                self._takt()
        except Exception as error:
            # nur Fehlerart und innerster Frame: Meldungen von Host-Rueckrufen koennen Treiberparameter tragen (Regel 6)
            logger.error("Fehler im Geraete-Thread (%s)%s, weiter im naechsten Takt", type(error).__name__,
                         commands.fehlerort(error))

    def _verarbeiten(self, item: tuple) -> None:
        quelle = item[0]
        if quelle == "link":
            _, art, daten = item
            if art == VERBUNDEN:
                self._verbunden()
            elif art == GETRENNT:
                self._bereit = False
                self.wunsch_puffer.verbindung_verloren()
            elif art == ANMELDUNG_SCHEITERT:
                self._rettung = True
            elif art == NACHRICHT:
                self._nachricht(*daten)
            elif art == GESENDET:
                self.wunsch_puffer.bestaetigt(daten[0])
        elif quelle == "wunsch":
            self._wunsch(*item[1:])
        elif quelle == "eigene":
            self._eigene_meldung(item[1], item[2])
        elif quelle == "meldung":
            self._meldung_merken(item[1])

    def _takt(self) -> None:
        now = self._clock()
        if self._rettung:
            if self._bootstrap.faellig():
                self._retten()
        elif self.link is None:
            self._link_aufbauen()
        link = self.link
        if link is not None:
            link.port_wechseln_falls_noetig()
            if link.connected:
                self._last_ok = now
                if self._hello_offen and not self._bereit:
                    self._hello_senden()
        if self._bereit:
            self._zurueckgehaltene_pruefen(now)
        self.befehle.pruefen()
        self._wunsch_senden()
        self._status_senden(now, force=False)
        self._meldungen_senden(now)
        self._host_wert("anzeigen", self.host.anzeigen, self.anzeige())  # gescheitert: diese Anzeige entfaellt

    def _register_args(self, faehigkeiten: dict | None = None) -> dict:
        """faehigkeiten: schon geprueft (_link_aufbauen), sonst ruft es den Host."""
        return {"version": self.host.version, "host": self.host.host,
                "capabilities": self.host.faehigkeiten() if faehigkeiten is None else faehigkeiten}

    def _link_aufbauen(self) -> None:
        zugang = bootstrap.laden(self._dir)
        if zugang is None:
            if not self._bootstrap.faellig():
                return
            faehigkeiten, gescheitert = self._host_wert("faehigkeiten", self.host.faehigkeiten)
            if gescheitert:
                return  # Registrierung verschieben, der naechste Takt versucht es erneut (ohne Backoff)
            zugang = self._bootstrap.holen(rettung=False, register_args=self._register_args(faehigkeiten))
            if zugang is None:
                return
        self._link_starten(zugang)

    def _link_starten(self, zugang: bootstrap.Zugang) -> None:
        link = self._link_factory(self.identity.device_id, zugang, identity.tls_key_pem(self.identity), self._ereignis)
        try:
            link.start()
        except ValueError as error:  # TransportConfigError: gespeicherter Zugang unbrauchbar -> Rettungsweg
            logger.error("MQTT-Zugang unbrauchbar (%s), nehme den Rettungsweg", type(error).__name__)
            self._rettung = True
            self._bootstrap.spaeter()  # Backoff wie ein gescheiterter Bootstrap, nicht jeden Takt ein Zertifikat
            return
        self.link = link

    def _retten(self) -> None:
        zugang = self._bootstrap.holen(rettung=True)
        if zugang is None:
            if self._bootstrap.gesperrt and self.link is not None:
                self.link.stop()  # gesperrt: keine sinnlosen Verbindungsversuche bis zum naechsten Versuch
                self.link = None
                self._bereit = False
            return
        self._rettung = False
        if self.link is not None:
            self.link.stop()
            self.link = None
        self._bereit = False
        self._link_starten(zugang)

    def _publish(self, name: str, payload: dict) -> int | None:
        link = self.link
        if link is None:
            return None
        try:
            mid = link.publish(name, payload)
        except ValueError as error:
            # Der Takt versucht es erneut (Status, Wunsch, hello): je Topic und Text einmal loggen, nicht jede Sekunde
            if self._publish_fehler.get(name) != str(error):
                self._publish_fehler[name] = str(error)
                logger.error("%s nicht gesendet: %s", name, error)
            return None
        self._publish_fehler.pop(name, None)
        return mid

    def _publish_nach_hello(self, name: str, payload: dict) -> int | None:
        """Laufzeit-Nachrichten (Snapshot, Telemetrie) erst nach hello (Spec 5b 5.1: hello zuerst)."""
        return self._publish(name, payload) if self._bereit else None

    def _hello(self) -> dict | None:
        """None: der Rueckruf faehigkeiten ist gescheitert (geloggt), hello wartet auf einen spaeteren Takt."""
        faehigkeiten, gescheitert = self._host_wert("faehigkeiten", self.host.faehigkeiten)
        if gescheitert:
            return None
        # device_state: Spec 5c F1, der Server gleicht ab
        return nachrichten.hello(
            konfiguration_version=self.documents.version(KONFIGURATION),
            bedienung_version=self.documents.version(BEDIENUNG), software_version=self.host.version,
            host=self.host.host, faehigkeiten=faehigkeiten, boot_id=self.boot_id,
            uhr_synchron=self._uhr_synchron() is True, device_state=bootstrap.device_state(self._dir))

    def _uhr_synchron(self) -> bool | None:
        """Rueckruf uhr_synchron; gescheitert gilt als None (unbekannt = nicht synchron), auch fuer Bootstrap."""
        synchron, gescheitert = self._host_wert("uhr_synchron", self.host.uhr_synchron)
        return None if gescheitert else synchron

    def _verbunden(self) -> None:
        if not self._bootstrap.gesperrt:
            # der bisherige Zugang funktioniert: ein noch offener Rettungsversuch (gescheitert, z. B. Uhr nicht synchron)
            # ist hinfaellig, sonst holte der naechste Backoff ein neues Zertifikat und braeche den Link ab
            self._rettung = False
            self._bootstrap.erfolg()
        self._bereit = False
        self._hello_offen = True
        self._hello_senden()

    def _hello_senden(self) -> None:
        try:
            hello = self._hello()
        except Exception as error:
            # Host-Rueckruf gescheitert oder Vertragsverstoss: Fehlerart und Ort, nie die Meldung (Regel 6); der
            # naechste Takt versucht es erneut. Je Fehlerart und Ort einmal loggen (der Takt ruft das jede Sekunde).
            art = f"{type(error).__name__}{commands.fehlerort(error)}"
            if art != self._hello_fehler:
                self._hello_fehler = art
                logger.error("hello nicht gebaut (%s), erneuter Versuch im naechsten Takt", art)
            return
        self._hello_fehler = None
        if hello is None:
            return
        if self._publish("up/hello", hello) is None:
            return  # schon wieder getrennt: das naechste "verbunden" schickt hello
        self._hello_offen = False
        self._bereit = True
        now = self._clock()
        self._last_ok = now
        self._status_senden(now, force=True)
        self._wunsch_senden()
        self._meldungen_neu.update(self._meldungen)
        self._meldungen_at = None
        self._meldungen_senden(now)
        if self._channel is not None:
            self._channel.verbunden()

    def _nachricht(self, name: str, raw: bytes) -> None:
        if name not in wire.DOWN_TOPICS:  # Spec 2.1: unbekannte Topics ignorieren, ohne die Nachricht zu lesen
            logger.debug("Topic %s unbekannt, ignoriert (Spec 2.1)", name)
            return
        if len(raw) > wire.MAX_MESSAGE_BYTES:
            logger.warning("%s zu gross, verworfen", name)
            return
        try:
            body = json.loads(raw)
        except (ValueError, RecursionError):
            logger.warning("%s ist kein JSON, verworfen", name)
            return
        if not isinstance(body, dict):
            return
        if name in _DOKUMENT_ART:
            self._dokument(_DOKUMENT_ART[name], body)
        elif name == "down/command":
            self.befehle.empfangen(body)
        elif name == "down/setpoints" and self._channel is not None:
            self._channel.setpoints(body)

    def _dokument(self, art: str, body: dict) -> None:
        """Erst das Ergebnis und die Uebergabe, dann software: jeder Host-Rueckruf ist einzeln abgesichert, damit ein
        gescheiterter die anderen nicht verhindert (das Dokument ist dann schon gespeichert).

        Kann der Host jetzt nicht pruefen (config_check.NichtPruefbar, HA aus) oder scheitert seine Vorbereitung vor dem
        Speichern (konfiguration_vorbereiten, Aufraeum-Markierung): weder Ergebnis noch Uebernahme. Der
        Server stellt ein Dokument nur einmal je Verbindung zu und eine abgelehnte Version nie erneut; das Geraet haelt
        es deshalb zurueck und prueft alle PRUEFUNG_WIEDERHOLEN_SECONDS erneut. Eine neue Zustellung ersetzt es."""
        self._zurueckgehalten.pop(art, None)
        alt = self.documents.inhalt(art)
        try:
            ergebnis = self.documents.empfangen(art, body)
        except config_check.NichtPruefbar:
            logger.warning("%s kann vorerst nicht geprueft werden, neuer Versuch in %s s", art,
                           PRUEFUNG_WIEDERHOLEN_SECONDS)
            self._zurueckgehalten[art] = body
            self._pruefung_at = self._clock() + PRUEFUNG_WIEDERHOLEN_SECONDS
            return
        if ergebnis is None:
            return
        self._publish("up/result", ergebnis.payload())
        if ergebnis.uebernommen:
            neu = self.documents.inhalt(art)
            assert neu is not None
            if art == KONFIGURATION:
                if not config_check.ist_leer(neu):
                    bootstrap.device_state_speichern(self._dir, "uebernommen")
                self._host_rufen("konfiguration_uebernommen", self.host.konfiguration_uebernommen, alt, neu)
            else:
                self._host_rufen("bedienung_uebernommen", self.host.bedienung_uebernommen, neu)
        if (angebot := angebot_aus(art, body)) is not None:
            # dasselbe Angebot, das der Speicher merkt, auch aus einer abgelehnten Konfiguration (ein Update kann der
            # Ausweg sein); ein ungueltiges laesst das vorherige stehen (R15, documents.angebot_aus)
            self._host_rufen("software_soll", self.host.software_soll, angebot.software)

    def _zurueckgehaltene_pruefen(self, now: float) -> None:
        if not self._zurueckgehalten or now < self._pruefung_at:
            return
        for art, body in list(self._zurueckgehalten.items()):
            self._dokument(art, body)

    @staticmethod
    def _host_rufen(name: str, rueckruf: Callable[..., None], *args) -> None:
        try:
            rueckruf(*args)
        except Exception as error:
            # nur Fehlerart und innerster Frame: die Meldung kann Treiberparameter tragen (Regel 6)
            logger.error("Host-Rueckruf %s gescheitert (%s)%s", name, type(error).__name__, commands.fehlerort(error))

    def _host_wert(self, name: str, rueckruf: Callable[..., _T], *args) -> tuple[_T | None, bool]:
        """Host-Rueckruf im Takt (5b-D1-4): (Wert, gescheitert). Gescheitert loggt je Rueckruf und Fehlerart einmal
        (der Takt ruft jede Sekunde, Regel 6: nur Art und Ort); nach einem Erfolg meldet sich ein neuer Fehler wieder.
        Was ein gescheiterter Rueckruf bedeutet, entscheidet die Aufrufstelle (nie ein erfundener Wert zum Server)."""
        try:
            wert = rueckruf(*args)
        except Exception as error:
            art = f"{type(error).__name__}{commands.fehlerort(error)}"
            if (name, art) not in self._host_fehler:
                self._host_fehler.add((name, art))
                logger.error("Host-Rueckruf %s gescheitert (%s), weiter im Takt", name, art)
            return None, True
        if self._host_fehler:
            self._host_fehler = {eintrag for eintrag in self._host_fehler if eintrag[0] != name}
        return wert, False

    def _ergebnis_senden(self, body: dict) -> bool:
        return self._publish_nach_hello("up/result", body) is not None

    def _inventur_senden(self, command_id: str, daten: dict) -> bool:
        teile = inventory.nachrichten(command_id, daten)  # ValueError (zu gross) wertet das Register aus
        if not self._bereit:
            return False
        return all(self._publish("up/inventory", nachricht) is not None for nachricht in teile)

    def _claim_state(self, payload: dict) -> commands.Outcome:
        """Uebernahmestand vom Server (Spec 5c F1): Discovery, Seite und Zustand folgen im naechsten Takt. Gueltig ist
        nur uebernommen: frei wird das Geraet nur ueber new_claim_code mit neuem Code (der Server sendet claim_state
        nie anders); nicht_uebernommen zeigte hier den alten Code wieder, den der vorige Besitzer kennt (Final-Review
        F-3)."""
        state = payload.get("device_state")
        if state != "uebernommen":
            return commands.Failed("ungueltige_nutzlast", fehler.details("device_state", "unbekannt"))
        bootstrap.device_state_speichern(self._dir, state)
        return commands.Done({})

    def _new_claim_code(self, payload: dict) -> commands.Outcome:
        """Neuer Uebernahme-Code (Spec 3.4): erst beim Server melden (register mit neuem Code-Hash; der Server
        uebernimmt ihn nur fuer ein nicht uebernommenes Geraet), dann speichern."""
        code = identity.new_claim_code()
        kandidat = replace(self.identity, claim_code=code)
        try:
            antwort = self._client_for(kandidat).register(**self._register_args())
        except bootstrap.BootstrapError as error:
            logger.warning("Neuer Uebernahme-Code nicht gemeldet (%s)", error)
            return commands.Failed("intern")
        bootstrap.device_state_speichern(self._dir, antwort.device_state)
        if antwort.device_state != "nicht_uebernommen":
            return commands.Failed("bereits_uebernommen")
        self.identity = identity.save_claim_code(self._dir, self.identity, code)
        return commands.Done({})

    def _wunsch(self, begrenzt: float, roh: float | None, herkunft: str | None, durch: str | None) -> None:
        # herkunft und durch hat Device.wunsch schon geprueft (wish.pruefen_herkunft)
        neu = wish.neu(begrenzt, roh, basis_version=self.documents.version(BEDIENUNG),
                       uhr_synchron=self._uhr_synchron() is True, herkunft=herkunft, durch=durch, wall=self._wall())
        self.wunsch_puffer.setzen(neu)
        self.documents.mark_dirty()
        text = wish.begrenzt_text(roh, begrenzt, herkunft) if roh is not None else None
        self._eigene_meldung(wish.BEGRENZT_KEY, text)
        self._wunsch_senden()

    def _wunsch_senden(self) -> None:
        wunsch = self.wunsch_puffer.wunsch
        if self._bereit and wunsch is not None and self.wunsch_puffer.offen():
            try:
                body = wunsch.payload()
            except nachrichten.Vertragsfehler as error:
                # Programmierfehler (Baustein: nur Feldnamen im Text, nie Werte); der Wunsch bleibt gepuffert, der
                # Takt ruft das jede Sekunde: je Text einmal loggen, der Rest des Takts laeuft weiter
                if str(error) != self._wunsch_fehler:
                    self._wunsch_fehler = str(error)
                    logger.error("up/wish nicht gebaut: %s", error)
                return
            self._wunsch_fehler = None
            self.wunsch_puffer.gesendet(self._publish("up/wish", body))

    def _eigene_meldung(self, key: str, text: str | None) -> None:
        if key not in wire.MELDUNGEN:
            raise nachrichten.Vertragsfehler("eigene Meldung: key steht nicht in MELDUNGEN")
        if self._eigene.get(key) == text:
            return
        vorher = self._eigene.pop(key, None)
        if text is not None:
            self._eigene[key] = text
        self._meldung_merken(nachrichten.meldung(key=key, kategorie=key, kritisch=False, text=text or vorher or "",
                                                 offen=text is not None, ts=_iso(self._wall())))

    def _meldung_merken(self, item: dict) -> None:
        key = item["key"]
        if item["offen"]:
            self._meldungen[key] = item
        else:
            self._meldungen.pop(key, None)
        self._meldungen_neu[key] = item

    def _meldungen_senden(self, now: float) -> None:
        if not self._bereit or not self._meldungen_neu:
            return
        if self._meldungen_at is not None and now - self._meldungen_at < MELDUNGEN_MIN_SECONDS:
            return
        items = list(self._meldungen_neu.values())[:MELDUNGEN_JE_NACHRICHT]
        if self._publish("up/notifications", nachrichten.notifications(items)) is None:
            return
        self._meldungen_at = now
        for item in items:
            if self._meldungen_neu.get(item["key"]) is item:
                del self._meldungen_neu[item["key"]]

    def _ursprungswerte_neu(self) -> dict:
        """Spec Stufe 3 S3 5.1: nur Namen, die in der Konfiguration (ursprungswerte, vom Server gespiegelt) fehlen, in
        den Grenzen von V1; was nicht passt, geht nicht hinaus (Log ohne Wert)."""
        konfiguration = self.konfiguration() or {}
        bekannt = konfiguration.get("ursprungswerte")
        bekannt = bekannt if isinstance(bekannt, dict) else {}
        werte: dict = {}
        gemerkt, gescheitert = self._host_wert("ursprungswerte", self.host.ursprungswerte)
        if gescheitert or not isinstance(gemerkt, dict):
            return {}  # Rueckruf gescheitert: keine Ursprungswerte in diesem Status
        for name, wert in sorted(gemerkt.items(), key=lambda paar: str(paar[0])):
            if name in bekannt:
                continue
            if not _ursprungsname_ok(name) or not _ursprungswert_ok(wert):
                self._ursprung_warnen(name, "nicht meldbar")
            elif len(werte) >= wire.URSPRUNG_MAX_HEBEL:
                self._ursprung_warnen(name, f"ueber dem Limit von {wire.URSPRUNG_MAX_HEBEL} Hebeln, nicht gemeldet")
            else:
                werte[name] = wert
        return werte

    def _ursprung_warnen(self, name, grund: str) -> None:
        """Je Name einmal je Prozess, ohne Wert (der Takt ruft das jede Sekunde)."""
        anzeige = name if _ursprungsname_ok(name) else "?"
        if anzeige in self._ursprung_gewarnt:
            return
        self._ursprung_gewarnt.add(anzeige)
        logger.warning("Ursprungswert %s %s", anzeige, grund)

    def _status_senden(self, now: float, *, force: bool) -> None:
        if not self._bereit:
            return
        runtime_status, gescheitert = self._host_wert("runtime_status", self.host.runtime_status)
        raum, raum_gescheitert = self._host_wert("raum", self.host.raum)
        if gescheitert or raum_gescheitert:
            return  # kein Status mit erfundenem Laufzeitstatus oder Raum; der naechste Takt versucht es erneut
        felder = {"zustand": self._zustand(runtime_status), "runtime_status": runtime_status, "raum": raum,
                  "ursprungswerte": self._ursprungswerte_neu() or None}
        try:
            body = nachrichten.status(**felder, ts=_iso(self._wall()))
        except nachrichten.Vertragsfehler as error:
            # Vertragsverstoss der Rueckruf-Ergebnisse (Baustein: nur Feldnamen im Text, nie Werte); je Text einmal
            # loggen, der Takt ruft das jede Sekunde
            if str(error) != self._status_fehler:
                self._status_fehler = str(error)
                logger.error("up/status nicht gebaut: %s", error)
            return
        self._status_fehler = None
        vergleich = _vergleichbar({k: v for k, v in body.items() if k != "ts"})
        if not force:
            if vergleich == self._status:
                return
            if self._status_at is not None and now - self._status_at < STATUS_MIN_SECONDS:
                return
        if self._publish("up/status", body) is not None:
            self._status, self._status_at = vergleich, now
