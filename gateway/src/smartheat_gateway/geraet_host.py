"""Das Gateway als Host des Geraetekerns (Spec 5b 5.1/5.3, Plan D2): liefert dem Paket Faehigkeiten, Uhrzeit,
Treiberpruefung, Befehle und Anzeige und nimmt Konfiguration, Bedienung und Soll-Software entgegen. Alle Rueckrufe des
Geraetekerns laufen im Geraete-Thread.

Status und Raum der Laufzeit liegen im Prozess (GeraetZustand; bis 0.5.0 retained auf dem lokalen Bus fuer den
Agenten). Meldungen der Laufzeit gehen als up/notifications an den Server (MeldungSink, Regeln wie der Agent bis 0.5.0:
show = offene kritische Meldung, withdraw = geschlossen, push = Ereignis). Die LED-Datei data/agent_state.json (Name aus
0.5.0) schreibt die ZustandsDatei bei jedem Wechsel und mindestens jede Minute. Eine laufzeitrelevante neue
Konfiguration fordert einen Neustart an (geraet_host_basis.EV_RELOAD im Regel-Worker), eine neue Bedienung geht als Soll-Eingang an den
Worker; kommt beides, bevor main() den Worker bindet (der Geraete-Thread laeuft schon), stellt worker_binden den
Neustart bzw. die letzte Bedienung nach (Spec 3.3). Der Wechsel von eingerichtet auf leer markiert das
Aufraeumen (A4-07), das der naechste Start ausfuehrt (vergessen). Die Software-Bruecke sendet die Meldungen des
Updaters (software_meldungen.json, fortlaufende seq) als up/result mit dokument software und quittiert die zuletzt
gesendete seq in software_quittung.json."""
import logging
import shutil
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from smartheat_core import dateien
from smartheat_device import fehler, wire
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


def _iso(wall: float) -> str:
    return datetime.fromtimestamp(wall, UTC).isoformat()


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
    """LED-Datei (Spec G2b-1 5): {zustand, seit, ts}; bei einem Wechsel sofort, sonst mindestens alle `every` s (der
    LED-Dienst zeigt eine Datei aelter als 5 min als stoerung). Wird nie gelesen, nur ueberschrieben."""

    def __init__(self, path: Path, now_iso: Callable[[], str], clock: Callable[[], float],
                 every: float = 60.0) -> None:
        self._path, self._now_iso, self._clock, self._every = path, now_iso, clock, every
        self._last: tuple[str, float] | None = None
        self._since: str | None = None

    def update(self, zustand: str) -> None:
        now = self._clock()
        if self._last is not None and self._last[0] == zustand and now - self._last[1] < self._every:
            return
        if self._last is None or self._last[0] != zustand:
            self._since = self._now_iso()
        dateien.schreiben_json(self._path, {"zustand": zustand, "seit": self._since, "ts": self._now_iso()}, modus=dateien.LESBAR)
        self._last = (zustand, now)


def vergessen(paths: Paths) -> None:
    """Leere Konfiguration nach einer Einrichtung (A4-07): Laufzeit-Ordner (Ursprungswerte, Notbetrieb, Abo-Frist,
    Soll-Speicher, Abmelde-Markierung), Inventur-Proben und die Anmeldedaten aller Cloud-Treiber weg; Geraetekern
    (device/) und Simulation bleiben. Beim Start eines frischen Prozesses, bevor die Laufzeit liest. Wirft nie."""
    shutil.rmtree(paths.runtime_dir, ignore_errors=True)
    befehle.forget_inventory(paths)
    for driver_id, driver_type in registry.DRIVERS.items():
        if driver_type.kind != KIND_CLOUD:
            continue
        try:
            registry.create(driver_id, {}, paths, writer=False).forget_credentials()
        except Exception as error:
            logger.error("Anmeldedaten von %s nicht geloescht (%s)", driver_id, type(error).__name__)
    logger.info("Anlagengebundener Zustand geloescht (Gateway nicht mehr eingerichtet)")


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

    def aufraeumen_pfad(self) -> Path:
        return self.paths.aufraeumen

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
        return {"drivers": list(registry.driver_ids()), "zigbee": True, "updater": True}

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
        Konfiguration keine Antwort und software_soll liefe nie (Review Focus 4). Nur der Typname, Regel 6."""
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
            dateien.schreiben_json(self.paths.software_soll, software, modus=dateien.LESBAR)  # 0644: der Updater (root) liest

    def runtime_status(self) -> dict | None:
        return self.zustand.status

    def raum(self) -> dict | None:
        return self.zustand.raum

    def anzeigen(self, anzeige: Anzeige) -> None:
        self.letzte_anzeige = anzeige
        try:
            self._datei.update(anzeige.zustand)
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
