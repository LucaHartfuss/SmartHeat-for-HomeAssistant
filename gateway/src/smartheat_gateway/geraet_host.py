"""Das Gateway als Host des Geraetekerns: liefert dem Paket Faehigkeiten, Uhrzeit,
Treiberpruefung, Befehle und Anzeige und nimmt Konfiguration, Bedienung und Soll-Software entgegen. Alle Rueckrufe des
Geraetekerns laufen im Geraete-Thread.

Status und Raum der Laufzeit liegen im Prozess (GeraetZustand). Meldungen der Laufzeit gehen als up/notifications an den Server (MeldungSink:
show = offene kritische Meldung, withdraw = geschlossen, push = Ereignis). Die LED-Datei data/agent_state.json
schreibt die ZustandsDatei bei jedem Wechsel und mindestens jede Minute. Eine laufzeitrelevante neue
Konfiguration fordert einen Neustart an (geraet_host_basis.EV_RELOAD im Regel-Worker), ebenso eine neue, nicht leere
Konfiguration derselben Anlage, die die Abmelde-Markierung aufhebt; eine neue Bedienung geht als Soll-Eingang
an den Worker; kommt beides, bevor main() den Worker bindet (der Geraete-Thread laeuft schon), stellt
worker_binden den Neustart bzw. die letzte Bedienung nach. Der Wechsel von eingerichtet auf leer markiert das
Aufraeumen, das der naechste Start ausfuehrt (vergessen). Die Software-Bruecke sendet die Meldungen des
Updaters (software_meldungen.json, fortlaufende seq) als up/result mit dokument software und quittiert die zuletzt
gesendete seq in software_quittung.json.

Der Uebernahme-Code entsteht nur nach einem Knopfdruck am Geraet (SG-02, GPC21, S-2): der Knopf-Dienst des Hosts schreibt
host/knopf.json {seq, ts}; ein Druck ist eine Aenderung dieses Paars gegenueber dem zuletzt gesehenen (nicht "seq
groesser": der Host setzt eine zerstoerte Datei auf seq 1 zurueck). Die erste nach dem Start lesbare Datei (oder ihr
Fehlen) ist nur Grundlinie, ein Neustart oeffnet kein Fenster. Ein Druck ruft Device.neuer_uebernahme_code; "neu" oeffnet
das Code-Fenster fuer 15 min, ein Druck binnen 60 s nach dem letzten neuen Code verlaengert es nur. Fenster und Drossel
stehen nur im Speicher (monotone Uhr); die LED-Datei meldet das offene Fenster als code_fenster."""
import json
import logging
import shutil
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from smartheat_core import dateien
from smartheat_core.logtext import lesbar
from smartheat_device import fehler, nachrichten, wire
from smartheat_device.commands import CommandRegister
from smartheat_device.geraet_host_basis import GeraetHostBasis
from smartheat_device.host import Anzeige
from smartheat_gateway import befehle
from smartheat_gateway import config as gateway_config
from smartheat_gateway.drivers import registry
from smartheat_gateway.drivers.base import KIND_CLOUD
from smartheat_gateway.paths import Paths
from smartheat_gateway.version import GATEWAY_VERSION
from smartheat_gateway.zigbee import ZigbeeMirror

logger = logging.getLogger(__name__)

UHR_MAX_ALTER_SECONDS = 300
SOFTWARE_SECONDS = 10
HOST_FIELDS = ("netz", "dns", "zeit_synchron")
CODE_FENSTER_SEKUNDEN = 900.0  # der Code ist 15 min sichtbar (GPC21)
CODE_ABSTAND_SEKUNDEN = 60.0  # hoechstens ein neuer Code je 60 s (GPC21)
KNOPF_DATEI = "knopf.json"  # im Host-Ordner, geschrieben vom Knopf-Dienst (smartheat_host.knopf)
_UNBEKANNT = object()  # noch keine Datei gelesen


def _iso(wall: float) -> str:
    return datetime.fromtimestamp(wall, UTC).isoformat()


def thermostat_unzulaessig(mirror: ZigbeeMirror, ieee) -> bool:
    """True, wenn der Spiegel `ieee` als definiertes Geraet kennt und es kein Thermostat ist."""
    geraet = mirror.device(ieee) if isinstance(ieee, str) else None
    return geraet is not None and geraet.definiert and geraet.art != "thermostat"


class GeraetZustand:
    """Status (Status-Modell Schema 2) und Raum (ist, soll, soll_quelle, ts) der Laufzeit; None = keiner."""

    def __init__(self) -> None:
        self.status: dict | None = None
        self.raum: dict | None = None


class StatusSink:
    def __init__(self, zustand: GeraetZustand) -> None:
        self._zustand = zustand

    def publish(self, event: dict) -> None:
        self._zustand.status = dict(event)


class ZustandsDatei:
    """LED-Datei: {zustand, seit, ts, code_fenster}; bei einem Wechsel von Zustand oder Code-Fenster sofort, sonst
    mindestens alle `every` s (der LED-Dienst zeigt eine Datei aelter als 5 min als stoerung). Der LED-Dienst liest
    `code_fenster` (nur ein echtes true zaehlt, smartheat_host.led.state_from_file). Wird hier nie gelesen, nur
    ueberschrieben."""

    def __init__(self, path: Path, now_iso: Callable[[], str], clock: Callable[[], float],
                 every: float = 60.0) -> None:
        self._path, self._now_iso, self._clock, self._every = path, now_iso, clock, every
        self._last: tuple[str, bool, float] | None = None
        self._since: str | None = None

    def update(self, zustand: str, code_fenster: bool = False) -> None:
        now = self._clock()
        if (self._last is not None and self._last[:2] == (zustand, code_fenster)
                and now - self._last[2] < self._every):
            return
        if self._last is None or self._last[0] != zustand:
            self._since = self._now_iso()
        dateien.schreiben_json(self._path, {"zustand": zustand, "seit": self._since, "ts": self._now_iso(),
                                            "code_fenster": code_fenster}, modus=dateien.LESBAR)
        self._last = (zustand, code_fenster, now)


def vergessen(paths: Paths) -> bool:
    """Leere Konfiguration nach einer Einrichtung: Laufzeit-Ordner (Ursprungswerte, Notbetrieb, Abo-Frist,
    Soll-Speicher, Abmelde-Markierung), Inventur-Proben und die Anmeldedaten aller Cloud-Treiber weg; Geraetekern
    (device/, auch die Rastung) und Simulation bleiben. Beim Start eines frischen Prozesses, bevor die Laufzeit liest.
    Wirft nie; True nur, wenn alles weg ist. runtime/backup.json (Bindung an Einrichtung und Anlage) zuletzt und nur,
    wenn alles andere weg ist: bleibt etwas liegen, verwirft bind_to_tenant es bei der Einrichtung einer anderen Anlage.
    Scheitert ein Loeschen (EACCES, EIO), bleibt die Aufraeum-Markierung fuer den naechsten Start (wie im Add-on);
    exists() steht mit im try (wirft bei EIO/EACCES im stat)."""
    gescheitert = False

    def entfernen(path: Path, wie: Callable[[Path], object]) -> None:
        nonlocal gescheitert
        try:
            if path.exists():
                wie(path)
        except OSError as error:
            gescheitert = True
            logger.warning("%s nicht geloescht (%s), neuer Versuch beim naechsten Start", path.name,
                           type(error).__name__)

    def datei_weg(path: Path) -> None:
        path.unlink(missing_ok=True)  # zwischen exists und unlink verschwunden: weg

    def eintrag_weg(path: Path) -> None:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)

    eintraege: list[Path] = []
    try:
        if paths.runtime_dir.exists():
            eintraege = sorted(path for path in paths.runtime_dir.iterdir() if path != paths.backup)
    except OSError as error:
        gescheitert = True
        logger.warning("%s nicht geloescht (%s), neuer Versuch beim naechsten Start", paths.runtime_dir.name,
                       type(error).__name__)
    for path in eintraege:
        entfernen(path, eintrag_weg)
    if not befehle.forget_inventory(paths):  # loggt selbst, eine Zeile je Datei
        gescheitert = True
    for driver_id, driver_type in registry.DRIVERS.items():
        if driver_type.kind != KIND_CLOUD:
            continue
        try:
            registry.create(driver_id, {}, paths, writer=False).forget_credentials()
        except Exception as error:
            gescheitert = True
            logger.error("Anmeldedaten von %s nicht geloescht (%s)", lesbar(str(driver_id)), type(error).__name__)
    # Erst backup.json, dann der (bis auf Neues) leere Ordner; jeder Schritt nur nach vollem Erfolg der vorigen.
    for path, wie in ((paths.backup, datei_weg), (paths.runtime_dir, shutil.rmtree)):
        if gescheitert:
            return False
        entfernen(path, wie)
    if gescheitert:
        return False
    logger.info("Anlagengebundener Zustand geloescht (Gateway nicht mehr eingerichtet)")
    return True


class GatewayDeviceHost(GeraetHostBasis):
    host = "gateway"
    version = GATEWAY_VERSION
    wunsch_herkunft = "thermostat"

    def __init__(self, paths: Paths, mirror: ZigbeeMirror, *, host_dir: Path,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time) -> None:
        super().__init__()
        self.paths, self.mirror, self._host_dir = paths, mirror, host_dir
        self._clock, self._wall = clock, wall
        self.zustand = GeraetZustand()
        self.status_sink = StatusSink(self.zustand)
        self._datei = ZustandsDatei(paths.agent_state, lambda: _iso(self._wall()), clock)
        self._software_at: float | None = None
        self._led_fehler: str | None = None  # Fehlerart des letzten gescheiterten Schreibens der LED-Datei
        #: Ende des Code-Fensters und Zeitpunkt des letzten neuen Codes (monotone Uhr, nur im Speicher)
        self.code_fenster_bis: float | None = None
        self.letzter_code: float | None = None
        self._code_hinweis: tuple[str, float] | None = None  # (keine_verbindung|uebernommen, bis) des letzten Drucks
        self._knopf_stand: object = _UNBEKANNT  # zuletzt gesehenes Paar (seq, ts), None = keine Datei
        self._knopf_fehler = False  # knopf.json unlesbar und schon gemeldet

    def aufraeumen_pfad(self) -> Path:
        return self.paths.aufraeumen

    def abgemeldet_pfad(self) -> Path:
        return self.paths.abgemeldet

    # --- Sicht der Laufzeit (host.GatewayHost) ---

    def snapshot(self) -> dict:
        """Auszug fuer Diagnoseseite und Befehl diagnostics, ohne Uebernahme-Code (den fuegt nur die Seite an)."""
        anzeige = self.letzte_anzeige
        host = dateien.lesen_json(self._host_dir / "status.json")
        return {
            "geraet": self.device.identity.device_id if self.device is not None else None,
            "zustand": anzeige.zustand if anzeige is not None else "startet",
            "eingerichtet": anzeige.eingerichtet if anzeige is not None else False,
            "versionen": {"gateway": GATEWAY_VERSION},
            "server_ok": anzeige.server_ok if anzeige is not None else False,
            "laufzeit": (self.zustand.status or {}).get("status"),
            "zigbee_stick": self.mirror.online is True,
            "host": {key: host[key] for key in HOST_FIELDS if key in host} if isinstance(host, dict) else {},
            "hinweise": list(anzeige.hinweise) if anzeige is not None else [],
            "meldungen": list(anzeige.meldungen) if anzeige is not None else [],
            "konfiguration_abgelehnt": anzeige.konfiguration_abgelehnt if anzeige is not None else None,
        }

    # --- DeviceHost ---

    def faehigkeiten(self) -> dict:
        return nachrichten.pruefen("faehigkeiten", {"drivers": list(registry.driver_ids()), "zigbee": True,
                                                    "updater": True}, wire.CAPABILITY_FIELDS, wire.CAPABILITY_OPTIONAL)

    def uhr_synchron(self) -> bool | None:
        """Zeitdienst des Hosts (host/status.json, Dienst hoststatus); aelter als 5 min oder unlesbar = unbekannt."""
        status = dateien.lesen_json(self._host_dir / "status.json")
        if not isinstance(status, dict):
            return None
        raw_ts = status.get("ts")
        if not isinstance(raw_ts, str):
            return None
        try:
            ts = datetime.fromisoformat(raw_ts)
        except ValueError:
            return None
        if ts.tzinfo is None or abs(self._wall() - ts.timestamp()) > UHR_MAX_ALTER_SECONDS:
            return None
        value = status.get("zeit_synchron")
        return value if isinstance(value, bool) else None

    def treiber_pruefen(self, bindung: dict) -> dict | None:
        """Jede unerwartete Ausnahme des Treibers (z. B. OverflowError) wird eine Ablehnung: sonst bekaeme die
        Konfiguration keine Antwort und software_soll liefe nie. Nur der Typname, Regel 6.

        Davor das Thermostat: kennt der Spiegel das gebundene Geraet als definiertes, das kein Thermostat ist,
        gingen Soll-Schreibvorgaenge an einen Fuehler oder Schalter. Ein unbekanntes oder noch nicht definiertes Geraet
        (Interview laeuft) lehnt nichts ab: der Server stellt eine abgelehnte Version nie erneut zu; der Start prueft
        es noch einmal (host.load)."""
        if thermostat_unzulaessig(self.mirror, bindung.get("thermostat") if isinstance(bindung, dict) else None):
            return fehler.details("thermostat", "nicht_erlaubt")
        try:
            return gateway_config.treiber_pruefen(self.paths, bindung)
        except Exception as error:
            art = type(error).__name__
            logger.error("Treiberpruefung gescheitert (%s), Konfiguration abgelehnt", art)
            return fehler.details("parameter", "ungueltig")

    def befehle_eintragen(self, register: CommandRegister) -> None:
        befehle.eintragen(register, befehle.Kontext(
            self.paths, self.mirror, konfiguration=self.konfiguration, runtime_status=lambda: self.zustand.status,
            neustart=self.neustart_anfordern, diagnostics=self.snapshot, clock=self._clock, wall=self._wall,
            hintergrund=self.hintergrund))

    def software_soll(self, software: dict | None) -> None:
        if dateien.lesen_json(self.paths.software_soll) != software or not self.paths.software_soll.exists():
            # 0644: der Updater (root) liest
            dateien.schreiben_json(self.paths.software_soll, software, modus=dateien.LESBAR)

    def runtime_status(self) -> dict | None:
        return self.zustand.status

    def raum(self) -> dict | None:
        return self.zustand.raum

    def anzeigen(self, anzeige: Anzeige) -> None:
        self.letzte_anzeige = anzeige
        self._knopf_pruefen()
        try:
            self._datei.update(anzeige.zustand, self.code_sichtbar())
        except Exception as error:  # die Software-Bruecke haengt nicht an der LED-Datei
            # Die Anzeige kommt jeden Takt: nur beim Wechsel der Fehlerart loggen, nicht jeden Takt.
            art = type(error).__name__
            if art != self._led_fehler:
                logger.error("LED-Datei nicht geschrieben (%s)", art)
                self._led_fehler = art
        else:
            if self._led_fehler is not None:
                logger.info("LED-Datei wieder geschrieben")
                self._led_fehler = None
        self._software_ergebnisse()

    # --- Uebernahme-Code (Knopf) ---

    def code_sichtbar(self) -> bool:
        """Code-Fenster offen und das Geraet fuer die Anzeige des Codes frei (Anzeige.code_zeigen)."""
        anzeige = self.letzte_anzeige
        return (anzeige is not None and anzeige.code_zeigen and self.code_fenster_bis is not None
                and self._clock() < self.code_fenster_bis)

    def code_hinweis(self) -> str | None:
        """Ergebnis des letzten Drucks ohne neuen Code (keine_verbindung, uebernommen), 15 min lang; sonst None."""
        hinweis = self._code_hinweis
        return hinweis[0] if hinweis is not None and self._clock() < hinweis[1] else None

    def _knopf_paar(self) -> tuple[bool, tuple[int, str] | None]:
        """(lesbar, Paar (seq, ts) oder None ohne Datei). Unlesbar (kaputt, falscher Aufbau): ein Druck ist das nicht;
        gemeldet wird einmal je Stoerung, der Takt kommt jede Sekunde."""
        try:
            roh = json.loads((self._host_dir / KNOPF_DATEI).read_text())
        except FileNotFoundError:
            self._knopf_fehler = False
            return True, None
        except (OSError, ValueError) as error:
            self._knopf_melden(type(error).__name__)
            return False, None
        seq, ts = (roh.get("seq"), roh.get("ts")) if isinstance(roh, dict) else (None, None)
        if not (isinstance(seq, int) and not isinstance(seq, bool) and isinstance(ts, str)):
            self._knopf_melden("Aufbau")
            return False, None
        self._knopf_fehler = False
        return True, (seq, ts)

    def _knopf_melden(self, art: str) -> None:
        if not self._knopf_fehler:
            self._knopf_fehler = True
            logger.warning("%s nicht lesbar (%s), kein Druck gewertet", KNOPF_DATEI, art)

    def _knopf_pruefen(self) -> None:
        """Wertet host/knopf.json: eine Aenderung des Paars (seq, ts) gegenueber dem zuletzt gesehenen ist ein Druck.
        Die Grundlinie (erste lesbare Lesung nach dem Start, auch "keine Datei") loest nie etwas aus; eine unlesbare
        Lesung ist keine Grundlinie (sonst wuerde dieselbe Datei danach lesbar zum Druck: ein Neustart oeffnete ein Fenster)."""
        lesbar, paar = self._knopf_paar()
        if not lesbar:
            return
        stand, self._knopf_stand = self._knopf_stand, paar
        if stand is not _UNBEKANNT and paar is not None and paar != stand:
            self._druck()

    def _druck(self) -> None:
        """Ein Knopfdruck: neuer Code, binnen 60 s nach dem letzten neuen Code nur das Fenster verlaengern."""
        now = self._clock()
        if self.letzter_code is not None and now - self.letzter_code < CODE_ABSTAND_SEKUNDEN:
            self.code_fenster_bis = now + CODE_FENSTER_SEKUNDEN
            return
        if self.device is None:
            return
        try:
            ergebnis = self.device.neuer_uebernahme_code()
        except Exception as error:  # die Anzeige haengt nicht an einem gescheiterten Druck; Regel 6: nur die Art
            logger.error("Uebernahme-Code nicht erzeugt (%s)", type(error).__name__)
            self.code_fenster_bis = None
            return
        if ergebnis == "neu":
            self.letzter_code, self.code_fenster_bis, self._code_hinweis = now, now + CODE_FENSTER_SEKUNDEN, None
        else:
            self.code_fenster_bis, self._code_hinweis = None, (ergebnis, now + CODE_FENSTER_SEKUNDEN)

    # --- Software-Bruecke ---

    def _software_ergebnisse(self) -> None:
        now = self._clock()
        if self.device is None or (self._software_at is not None and now - self._software_at < SOFTWARE_SECONDS):
            return
        self._software_at = now
        raw = dateien.lesen_json(self.paths.software_meldungen)
        eintraege = raw.get("meldungen") if isinstance(raw, dict) else None
        if not isinstance(eintraege, list):
            return
        quittung = dateien.lesen_json(self.paths.software_quittung)
        gesendet = quittung.get("seq") if isinstance(quittung, dict) and isinstance(quittung.get("seq"), int) else 0
        offen = [e for e in eintraege if isinstance(e, dict) and isinstance(e.get("seq"), int) and e["seq"] > gesendet
                 and isinstance(e.get("version"), str) and e.get("result") in ("ok", "rollback")]
        for eintrag in sorted(offen, key=lambda e: e["seq"]):
            ok = eintrag["result"] == "ok"
            grund: str | None = None
            if not ok:
                reason = eintrag.get("reason")
                grund = reason if isinstance(reason, str) and reason in wire.SOFTWARE_ERRORS else "ungesund"
            if not self.device.ergebnis_software(eintrag["version"], ok, grund):
                return
            dateien.schreiben_json(self.paths.software_quittung, {"seq": eintrag["seq"]}, modus=dateien.LESBAR)
