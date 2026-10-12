"""Einstieg des Add-ons ab 1.0.0 (Spec 5c 1.1/1.2): ein Prozess aus Geraetekern (smartheat_device, eigener Thread) und
Laufzeit (smartheat_runtime, Regel-Worker im Hauptthread), wie das Gateway. Optionen nur zum Starten (bootstrap_url,
log_level); Konfiguration und Bedienung kommen als Soll-Dokumente. Status-Ereignis (Schema 3), Discovery und
Uebernahme-Code haengt verdrahten() an den Geraete-Host; der Geraete-Thread meldet sie in jedem Takt (anzeigen), auch
im Ruhezustand "nicht eingerichtet", in dem die Laufzeit selbst nichts sendet.

Neustart (neue laufzeitrelevante Konfiguration, sign_off, Abo am Fristende wieder aktiv, Neupruefung im
Konfigurationsfehler): der Worker endet mit Neustart, dann trennt beenden() den Link sauber (MQTT-DISCONNECT; sonst
meldet IoT Core DUPLICATE_CLIENTID, wenn der neue Prozess verbindet) und wartet auf den Geraete-Thread, erst danach
os.execv. Anders als das Gateway (Compose startet nach Exit 0 neu) startet der Supervisor ein sauber beendetes Add-on
nicht zuverlaessig neu, deshalb exec. SIGTERM (Supervisor stoppt das Add-on) endet ueber beenden() ohne exec.

Nach dem Entfernen (aufraeumen.json) loescht der naechste Start den anlagengebundenen Zustand und die Hilfssensoren,
bevor der Geraetekern startet - aber nur, solange die gespeicherte Konfiguration noch leer ist."""
import json
import logging
import os
import shutil
import signal
import sys
import threading
import time
import urllib.parse

from heizungsbruecke import config, derived_sensors
from heizungsbruecke.discovery import Discovery
from heizungsbruecke.geraet_host import EV_RELOAD, HaDeviceHost
from heizungsbruecke.geraet_status import GeraetStatus
from heizungsbruecke.ha_api import HomeAssistantApi
from heizungsbruecke.host import HaHost
from heizungsbruecke.supervisor import SupervisorApi
from heizungsbruecke.version import ADDON_VERSION
from smartheat_device.device import DEVICE_DIR, Device
from smartheat_device.documents import KONFIGURATION, DocumentStore, read_json
from smartheat_device.hintergrund import Hintergrund
from smartheat_runtime import abo, app, dokument_config
from smartheat_runtime.runtime import EV_RECHECK, Runtime
from smartheat_runtime.status import STATUS_KONFIGURATIONSFEHLER

logger = logging.getLogger(__name__)

DEFAULT_BOOTSTRAP_URL = "https://accounts.hartfussha.org"
LOG_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}
#: Ende des Prozesses: so lange darf der Geraete-Thread sein laufendes Ereignis noch abschliessen.
JOIN_SECONDS = 10.0
#: Aufraeumen beim Start (startup: services, HA evtl. noch nicht bereit): so lange auf HA warten, um die Hilfssensoren zu
#: loeschen; danach startet der Geraetekern trotzdem, und der naechste Start versucht es erneut.
AUFRAEUMEN_HA_WARTEN_SECONDS = 120.0
AUFRAEUMEN_HA_TAKT_SECONDS = 5.0
#: Unerwarteter Fehler im Start: Pause vor dem Neustart (exec), damit ein dauerhafter Fehler keine schnelle Schleife wird.
FEHLER_PAUSE_SECONDS = 60.0


class Neustart(BaseException):
    """Aus dem Regel-Worker: Prozess nach sauberem Trennen neu starten. BaseException wie SystemExit beim Gateway: der
    Worker faengt in Handlern nur Exception ab, diese hier geht bis laufen() durch."""


def _optionen() -> dict | None:
    """Inhalt von options.json oder None, wenn sie unlesbar, kein JSON oder kein Objekt ist. Der Supervisor schreibt die
    Datei immer; fehlt sie trotzdem ganz, gilt sie als leer ({}: Produktions-Standard)."""
    try:
        raw = json.loads(config.OPTIONS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _url_fehler(value) -> str | None:
    """Grund, warum value keine Bootstrap-Adresse ist, sonst None. Wie SHG_DEVICE_API_URL beim Gateway: http oder https
    mit Host (Labor im LAN per http)."""
    if not isinstance(value, str):
        return "kein Text"
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return "nicht lesbar"
    if parts.scheme not in ("http", "https"):
        return "Schema nicht http oder https"
    if not parts.hostname:
        return "ohne Host"
    return None


def bootstrap_url() -> str | None:
    """Umgebung SMARTHEAT_BOOTSTRAP_URL (nur E2E/Tests) vor der Option bootstrap_url vor dem Produktions-Standard. Der
    Standard gilt nur, wenn beides fehlt (Schluessel fehlt oder null, options.json fehlt ganz): eine ungueltige Angabe
    und eine kaputte options.json fallen nie auf die Produktion zurueck (ein falsch eingestelltes Labor-Add-on meldete
    sich sonst dort an), sondern ergeben None. Ins Log kommt nur der Grund, nie die Adresse (sie kann Zugangsdaten
    tragen)."""
    override = os.environ.get("SMARTHEAT_BOOTSTRAP_URL")
    if override:
        quelle, value = "SMARTHEAT_BOOTSTRAP_URL", override
    else:
        optionen = _optionen()
        if optionen is None:
            logger.warning("options.json nicht lesbar oder kein JSON-Objekt: das Geraet meldet sich nicht an und "
                           "bleibt im Ruhezustand")
            return None
        quelle, value = "bootstrap_url", optionen.get("bootstrap_url")
        value = DEFAULT_BOOTSTRAP_URL if value is None else value  # nicht gesetzt (fehlt oder null)
    grund = _url_fehler(value)
    if grund is not None:
        logger.warning("%s ungueltig (%s): das Geraet meldet sich nicht an und bleibt im Ruhezustand", quelle, grund)
        return None
    assert isinstance(value, str)
    return value.rstrip("/")


def log_level() -> int:
    return LOG_LEVELS.get(str((_optionen() or {}).get("log_level", "info")), logging.INFO)


def _exec() -> None:
    """Ersetzt den Prozess durch einen frischen Start mit demselben Befehl (`python -m heizungsbruecke`); wie
    abo.restart_process, das start() fuer die Laufzeit auf den sauberen Neustart umleitet."""
    os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])


def _ha_bereit(ha_api) -> bool:
    frist = time.monotonic() + AUFRAEUMEN_HA_WARTEN_SECONDS
    while not ha_api.is_reachable():
        if time.monotonic() >= frist:
            return False
        time.sleep(AUFRAEUMEN_HA_TAKT_SECONDS)
    return True


def aufraeumen(ha_api) -> None:
    """Markierung aufraeumen.json (HaDeviceHost.konfiguration_uebernommen: Wechsel von eingerichtet auf leer) vor dem
    Start des Geraetekerns auswerten. Geloescht wird nur, wenn die gespeicherte Konfiguration noch nicht wieder
    eingerichtet ist: Sicherung, Notbetriebs- und Abo-Zustand, Laufzeit-Ordner und die Hilfssensoren in HA. Bleiben
    Hilfssensoren uebrig (HA nicht bereit oder Fehler), bleibt die Markierung fuer den naechsten Start."""
    if not config.AUFRAEUMEN_PATH.exists():
        return
    gespeichert = DocumentStore(config.DEVICE_DATA_DIR / DEVICE_DIR, host=HaDeviceHost.host,
                                software_version=ADDON_VERSION).inhalt(KONFIGURATION)
    if dokument_config.eingerichtet(gespeichert):
        logger.info("Aufraeumen entfaellt: das Add-on ist inzwischen neu eingerichtet")
        config.AUFRAEUMEN_PATH.unlink(missing_ok=True)
        return
    marker = read_json(config.AUFRAEUMEN_PATH)
    anlage = marker.get("anlage") if isinstance(marker, dict) else None
    fertig = True
    if isinstance(anlage, str) and anlage and config.DERIVED_SENSORS_PATH.exists():
        if _ha_bereit(ha_api):
            fertig = derived_sensors.entfernen(ha_api, anlage, config.DERIVED_SENSORS_PATH)
        else:
            fertig = False
            logger.warning("Home Assistant nicht bereit: Hilfssensoren werden beim naechsten Start entfernt")
    geloescht = False
    for path in (config.BACKUP_PATH, config.FAILSAFE_PATH, config.ENTITLEMENT_PATH):
        if path.exists():
            path.unlink(missing_ok=True)
            geloescht = True
    if config.RUNTIME_DIR.exists():
        shutil.rmtree(config.RUNTIME_DIR, ignore_errors=True)
        geloescht = True
    if geloescht:
        logger.info("Anlagengebundener Zustand geloescht (Add-on nicht mehr eingerichtet)")
    if fertig:
        config.AUFRAEUMEN_PATH.unlink(missing_ok=True)


class _Laufzeit:
    """Der HaHost des laufenden Starts: der Geraete-Thread liest daraus den wirksamen Raum-Soll fuer das
    Status-Ereignis (GeraetStatus)."""

    host: HaHost | None = None


status_holder = _Laufzeit()


def _raum_soll() -> float | None:
    host = status_holder.host
    store = host.store if host is not None else None
    return store.value if store is not None else None


def verdrahten(geraet: HaDeviceHost, device, ha_api, supervisor) -> GeraetStatus:
    """Geraete-Host an den Geraetekern haengen: Status-Ereignis Schema 3 (laufzeit_merken legt das Laufzeit-Ereignis
    fuer up/status und sign_off ab), Supervisor-Discovery und der jeweils aktuelle Uebernahme-Code."""
    geraet.verbinden(device)
    status = GeraetStatus(ha_api, device.identity.device_id, _raum_soll,
                          laufzeit_merken=lambda event: setattr(geraet, "runtime_status_event", event))
    geraet.status_sink = status
    geraet.discovery = Discovery(supervisor, config.DEVICE_DATA_DIR / DEVICE_DIR / "discovery.json")
    geraet.claim_code = lambda: device.identity.claim_code
    return status


def start(geraet: HaDeviceHost, device, ha_api, status: GeraetStatus,
          clock=time.monotonic) -> tuple[Runtime | app.IdleBridge, HaHost]:
    """Laufzeit auf dem Link des Geraets starten (mqtt_factory) und ihre Neustarts auf den sauberen Neustart umleiten:
    EV_RELOAD (Geraete-Host), abo.restart_process (Abo wieder aktiv; Neupruefung im Konfigurationsfehler) und - nicht
    nur ueber diesen globalen Eingriff - EV_RECHECK im Konfigurationsfehler."""
    geraet.status_sink = status

    def _neustart(anlass: str) -> None:
        logger.info("%s, Add-on startet neu", anlass)
        raise Neustart()

    abo.restart_process = lambda: _neustart("Neustart der Laufzeit (Abo wieder aktiv oder Neupruefung)")
    host = HaHost(geraet, ha_api, clock=clock)
    status_holder.host = host
    result = app.start(host, clock, mqtt_factory=device.laufzeit_kanal)
    worker = result.worker
    worker.register(EV_RELOAD, lambda event: _neustart("Neue Konfiguration oder Abmelden"))
    if isinstance(result, app.IdleBridge) and result.reason == STATUS_KONFIGURATIONSFEHLER:
        worker.register(EV_RECHECK, lambda event: _neustart("Neupruefung nach Konfigurationsfehler"))
    geraet.worker_binden(worker, soll_eingang=isinstance(result, Runtime))  # Ruhezustand: kein Soll-Eingang
    return result, host


def _schritt(name: str, aufruf) -> None:
    try:
        aufruf()
    except Exception as error:  # das Ende darf nie scheitern: sonst entfiele das exec nach einem Neustart
        logger.warning("Beenden: %s gescheitert (%s)", name, type(error).__name__)


def _warten(thread: threading.Thread) -> None:
    thread.join(JOIN_SECONDS)
    if thread.is_alive():
        logger.warning("Geraete-Thread nach %.0f s nicht beendet, Prozess endet trotzdem", JOIN_SECONDS)


def beenden(device, thread: threading.Thread | None, hintergrund: Hintergrund | None) -> None:
    """Ende des Prozesses (Neustart, SIGTERM): erst den Geraetekern stoppen (sauberes Trennen) und wartende HA-Aufrufe
    der Inventur abbrechen, dann den Geraete-Thread sein laufendes Ereignis abschliessen lassen. Danach noch einmal
    stop(): ein letzter Takt nach dem ersten stop() koennte einen neuen Link geoeffnet haben (wie beim Gateway). Wirft
    nie: jeder Schritt einzeln, ins Log nur die Fehlerart."""
    _schritt("Geraetekern stoppen", device.stop)
    if hintergrund is not None:
        _schritt("Hintergrund stoppen", hintergrund.stop)
    if thread is not None:
        _schritt("Geraete-Thread abwarten", lambda: _warten(thread))
    _schritt("Geraetekern stoppen", device.stop)


def laufen(geraet: HaDeviceHost, device, ha_api, status: GeraetStatus, clock=time.monotonic) -> None:
    """Geraete-Thread und Laufzeit bis zum Ende des Prozesses; ein Neustart startet den Prozess erst nach beenden()
    neu (exec), SIGTERM (SystemExit) endet nach beenden() ohne exec. Ein unerwarteter Fehler (Start der Laufzeit, Worker)
    beendet den Prozess nie still: Fehlerart ins Log, sauber trennen, FEHLER_PAUSE_SECONDS warten, dann derselbe
    Neustart (das Add-on beendet sich nie von selbst; die Pause verhindert eine schnelle Schleife)."""
    thread: threading.Thread | None = None
    neu = fehler = False
    try:
        thread = device.start()
        result, _ = start(geraet, device, ha_api, status, clock)
        logger.info("Laufzeit gestartet (%s)", type(result).__name__)
        result.worker.run()
    except Neustart:
        neu = True
    except Exception as error:
        logger.error("Unerwarteter Fehler der Laufzeit (%s), Neustart in %.0f s", type(error).__name__,
                     FEHLER_PAUSE_SECONDS)
        logger.debug("Fehler der Laufzeit", exc_info=True)
        neu = fehler = True
    finally:
        beenden(device, thread, geraet.hintergrund)
    if fehler:
        time.sleep(FEHLER_PAUSE_SECONDS)
    if neu:
        logger.info("Neustart nach sauberem Trennen")
        _exec()


def _ruhen() -> None:
    """Ruhezustand ohne Geraetekern (ungueltige bootstrap_url): bis SIGTERM nichts tun, nie beenden."""
    while True:
        time.sleep(3600)


def _terminate(signum, frame) -> None:
    raise SystemExit(0)


def main() -> None:  # pragma: no cover - Add-on-Einstieg
    logging.basicConfig(level=log_level())
    token = os.environ["SUPERVISOR_TOKEN"]
    ha_api = HomeAssistantApi(base_url="http://supervisor", token=token)
    supervisor = SupervisorApi(token=token)
    signal.signal(signal.SIGTERM, _terminate)  # vor dem Geraete-Thread: ein SIGTERM endet immer ueber beenden
    url = bootstrap_url()
    if url is None:  # nie auf die Produktion ausweichen: ohne Geraetekern ruhen, bis die Option korrigiert ist
        _ruhen()
        return
    aufraeumen(ha_api)  # vor dem Geraetekern: noch schreibt nichts in den anlagengebundenen Zustand
    geraet = HaDeviceHost(ha_api, supervisor)
    device = Device(geraet, config.DEVICE_DATA_DIR, url)
    status = verdrahten(geraet, device, ha_api, supervisor)
    laufen(geraet, device, ha_api, status)


if __name__ == "__main__":
    main()
