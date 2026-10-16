"""Einstieg des Add-ons ab 1.0.0 (Spec 5c 1.1/1.2): ein Prozess aus Geraetekern (smartheat_device, eigener Thread) und
Laufzeit (smartheat_runtime, Regel-Worker im Hauptthread), wie das Gateway. Optionen nur zum Starten (bootstrap_url,
log_level); Konfiguration und Bedienung kommen als Soll-Dokumente. Status-Ereignis (Schema 3), Discovery und
Uebernahme-Code haengt verdrahten() an den Geraete-Host; der Geraete-Thread meldet sie in jedem Takt (anzeigen), auch
im Ruhezustand "nicht eingerichtet", in dem die Laufzeit selbst nichts sendet.

Neustart, Ende und Aufraeumen beim Start teilen sich beide Hosts in smartheat_device.prozess (neue laufzeitrelevante
Konfiguration, sign_off, Abo am Fristende wieder aktiv, Neupruefung im Konfigurationsfehler: der Worker endet mit
prozess.Neustart, beenden trennt den Link sauber, erst danach os.execv). Anders als das Gateway (Compose startet nach
Exit 0 neu) startet der Supervisor ein sauber beendetes Add-on nicht zuverlaessig neu, deshalb exec. SIGTERM
(Supervisor stoppt das Add-on) endet ueber beenden ohne exec.

Nach dem Entfernen (aufraeumen.json) setzt der naechste Start die Anlage erst zurueck wie sign_off (Laufzeit im Modus
abgemeldet mit der alten Konfiguration aus der Markierung) und startet danach neu; erst dieser Start loescht den
anlagengebundenen Zustand und die Hilfssensoren, bevor der Geraetekern startet - aber nur, solange die gespeicherte
Konfiguration noch leer ist (Spec Stufe 3 S3 Abschnitt 4)."""
import json
import logging
import os
import shutil
import signal
import sys
import time
import urllib.parse

from heizungsbruecke import config, derived_sensors
from heizungsbruecke.discovery import Discovery
from heizungsbruecke.geraet_host import HaDeviceHost
from heizungsbruecke.geraet_status import GeraetStatus
from heizungsbruecke.ha_api import HomeAssistantApi
from heizungsbruecke.host import HaHost
from heizungsbruecke.supervisor import SupervisorApi
from heizungsbruecke.version import ADDON_VERSION
from smartheat_device import prozess
from smartheat_device.device import DEVICE_DIR, Device
from smartheat_device.documents import KONFIGURATION, DocumentStore
from smartheat_runtime import app
from smartheat_runtime import aufraeumen as markierung
from smartheat_runtime.runtime import Runtime

logger = logging.getLogger(__name__)

DEFAULT_BOOTSTRAP_URL = "https://accounts.hartfussha.org"
LOG_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}
#: Aufraeumen beim Start (startup: services, HA evtl. noch nicht bereit): so lange auf HA warten, um die Hilfssensoren zu
#: loeschen; danach startet der Geraetekern trotzdem, und der naechste Start versucht es erneut.
AUFRAEUMEN_HA_WARTEN_SECONDS = 120.0
AUFRAEUMEN_HA_TAKT_SECONDS = 5.0


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


def _url_fehler(value, *, http_erlaubt: bool = False) -> str | None:
    """Grund, warum value keine Bootstrap-Adresse ist, sonst None. Option nur https mit Host (wie das Gateway-Image,
    5c F/Q4); http nur ueber SMARTHEAT_BOOTSTRAP_URL (E2E/Tests, im Add-on nicht setzbar)."""
    if not isinstance(value, str):
        return "kein Text"
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return "nicht lesbar"
    if parts.scheme not in (("http", "https") if http_erlaubt else ("https",)):
        return "Schema nicht http oder https" if http_erlaubt else "Schema nicht https"
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
    grund = _url_fehler(value, http_erlaubt=quelle == "SMARTHEAT_BOOTSTRAP_URL")
    if grund is not None:
        logger.warning("%s ungueltig (%s): das Geraet meldet sich nicht an und bleibt im Ruhezustand", quelle, grund)
        return None
    assert isinstance(value, str)
    return value.rstrip("/")


def log_level() -> int:
    return LOG_LEVELS.get(str((_optionen() or {}).get("log_level", "info")), logging.INFO)


def _exec() -> None:
    """Ersetzt den Prozess durch einen frischen Start mit demselben Befehl (`python -m heizungsbruecke`); der
    Neustart des Hosts fuer prozess.laufen."""
    os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])


def _ha_bereit(ha_api) -> bool:
    frist = time.monotonic() + AUFRAEUMEN_HA_WARTEN_SECONDS
    while not ha_api.is_reachable():
        if time.monotonic() >= frist:
            return False
        time.sleep(AUFRAEUMEN_HA_TAKT_SECONDS)
    return True


def aufraeumen(ha_api) -> None:
    """Markierung aufraeumen.json vor dem Start des Geraetekerns (prozess.aufraeumen_beim_start); geloescht werden
    Sicherung, Notbetriebs- und Abo-Zustand, Laufzeit-Ordner und die Hilfssensoren in HA. Bleiben Hilfssensoren uebrig
    (HA nicht bereit oder Fehler), bleibt die Markierung fuer den naechsten Start."""
    prozess.aufraeumen_beim_start(
        config.AUFRAEUMEN_PATH,
        lambda: DocumentStore(config.DEVICE_DATA_DIR / DEVICE_DIR, host=HaDeviceHost.host,
                              software_version=ADDON_VERSION).inhalt(KONFIGURATION),
        lambda: _anlage_loeschen(ha_api), wer="das Add-on")


def _anlage_loeschen(ha_api) -> bool:
    gelesen = markierung.lesen(config.AUFRAEUMEN_PATH)
    anlage = gelesen.anlage if gelesen is not None else None
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
    return fertig


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
    """Laufzeit auf dem Link des Geraets starten (mqtt_factory); alle Neustarts gehen ueber prozess.Neustart."""
    geraet.status_sink = status
    host = HaHost(geraet, ha_api, clock=clock)
    status_holder.host = host
    result = app.start(host, clock, mqtt_factory=device.laufzeit_kanal)
    prozess.neustarts_umleiten(result)
    geraet.worker_binden(result.worker, soll_eingang=isinstance(result, Runtime))  # Ruhezustand: kein Soll-Eingang
    return result, host


def laufen(geraet: HaDeviceHost, device, ha_api, status: GeraetStatus, clock=time.monotonic) -> None:
    """prozess.laufen mit exec als Neustart (Moduldocstring)."""
    prozess.laufen(device, geraet.hintergrund, lambda: start(geraet, device, ha_api, status, clock)[0], _exec)


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
