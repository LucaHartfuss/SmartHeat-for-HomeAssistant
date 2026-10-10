"""Einstieg des Gateways (Spec 5b 5.3, Plan D2): ein Prozess fuer Geraetekern (smartheat_device, eigener Thread) und
Laufzeit (smartheat_runtime, Regel-Worker im Hauptthread). Agent-Container, HTTPS-Polling und Tunnel entfallen; Tick
und Telemetrie laufen ueber den Link des Geraets (mqtt_factory).

Neustart: eine laufzeitrelevante neue Konfiguration oder sign_off fordert ihn an (EV_RELOAD). Der Worker trennt zuerst
den Link sauber (MQTT-DISCONNECT; sonst meldet IoT Core DUPLICATE_CLIENTID, wenn der neue Prozess verbindet, und der
Dienst saehe nach drei Aenderungen eine Kopie, Spec 4.2) und endet dann mit Exit 0; Compose startet neu. SIGTERM
(docker stop) endet genauso; vor dem Exit schliesst der Geraete-Thread sein laufendes Ereignis ab (beenden), und jeder
Start reicht die Soll-Software der gespeicherten Konfiguration erneut an den Updater (software_soll_nachholen). Auch die Neustarts der Laufzeit selbst (abo.restart_process: Neupruefung im Ruhezustand
Konfigurationsfehler, Abo am Fristende wieder aktiv) nehmen diesen Weg statt os.execv, das den Link ohne DISCONNECT
abrisse. Nach dem Entfernen (Markierung aufraeumen.json) loescht der naechste Start den anlagengebundenen Zustand
(A4-07), bevor der Geraetekern startet - aber nur, solange die gespeicherte Konfiguration noch leer ist: kam vor dem
Neustart schon eine neue Einrichtung, bleibt ihre frische Treiber-Anmeldung. Die Konfigurationswache haelt nur noch das
Lebenszeichen fuer den Compose-Health-Check frisch (G2 7.2)."""
import contextlib
import logging
import os
import signal
import threading
import time
from pathlib import Path
from typing import NoReturn

from smartheat_core import wallclock
from smartheat_device.device import Device
from smartheat_device.documents import KONFIGURATION, DocumentStore
from smartheat_gateway import befehle, diagnose
from smartheat_gateway import geraet_host as geraet_host_module
from smartheat_gateway.bus import LocalBus, credentials_from_env
from smartheat_gateway.geraet_host import GatewayDeviceHost
from smartheat_gateway.host import GatewayHost
from smartheat_gateway.paths import Paths, from_env
from smartheat_gateway.version import GATEWAY_VERSION
from smartheat_gateway.zigbee import ZigbeeMirror
from smartheat_runtime import abo, app, dokument_config
from smartheat_runtime.runtime import EV_RECHECK, Runtime
from smartheat_runtime.status import STATUS_KONFIGURATIONSFEHLER
from smartheat_runtime.worker import Event

logger = logging.getLogger(__name__)

EV_RELOAD = geraet_host_module.EV_RELOAD
EV_CONFIG_WATCH = "shg_config_watch"
EV_RAUM = "shg_raum"
CONFIG_WATCH_SECONDS = 30
RAUM_SECONDS = 30
#: Ende des Prozesses: so lange darf der Geraete-Thread sein laufendes Ereignis noch abschliessen.
JOIN_SECONDS = 10.0


def _alive_file() -> Path:
    """Lebenszeichen fuer den Health-Check von Compose (G2 7.2): die Wache beruehrt es alle 30 s."""
    return Path(os.environ.get("SHG_ALIVE_FILE", "/tmp/shg-runtime-alive"))


def _touch_alive() -> None:
    with contextlib.suppress(OSError):  # ein nicht beschreibbares /tmp darf die Wache nicht beenden
        _alive_file().touch()


def aufraeumen(paths: Paths) -> None:
    """Markierung aufraeumen.json (geraet_host: Wechsel von eingerichtet auf leer) vor dem Start des Geraetekerns
    auswerten: noch nichts schreibt, und ein driver_login direkt nach hello kann nicht mit dem Loeschen kollidieren.
    Geloescht wird nur, wenn die gespeicherte Konfiguration noch nicht wieder eingerichtet ist; die Markierung geht in
    jedem Fall weg."""
    if not paths.aufraeumen.exists():
        return
    gespeichert = DocumentStore(paths.device_dir, host=GatewayDeviceHost.host,
                                software_version=GATEWAY_VERSION).inhalt(KONFIGURATION)
    if dokument_config.eingerichtet(gespeichert):
        logger.info("Aufraeumen entfaellt: das Gateway ist inzwischen neu eingerichtet")
    else:
        geraet_host_module.vergessen(paths)
    paths.aufraeumen.unlink(missing_ok=True)


def software_soll_nachholen(geraet: GatewayDeviceHost, device: Device) -> None:
    """Soll-Software der gespeicherten Konfiguration erneut an den Updater (idempotent), bevor der Geraete-Thread
    laeuft: ein Neustart (EV_RELOAD aus konfiguration_uebernommen) kann den Prozess beenden, bevor der Geraete-Thread
    software_soll.json schrieb, und der Server stellt eine bestaetigte Version nicht erneut zu (Review Focus 4). Nicht
    nach einer abgelehnten Konfiguration: deren Soll-Software steht schon dort (ohne Neustart geschrieben) und gilt."""
    konfiguration = device.konfiguration()
    if konfiguration is None or device.documents.ablehnung(KONFIGURATION) is not None:
        return
    software = konfiguration.get("software")
    try:
        geraet.software_soll(software if isinstance(software, dict) else None)
    except Exception as error:  # ein Schreibfehler darf den Start nicht verhindern; der naechste Start versucht es erneut
        logger.error("Soll-Software nicht nachgeholt (%s)", type(error).__name__)


def beenden(device, thread: threading.Thread | None, timeout: float = JOIN_SECONDS) -> None:
    """Ende des Prozesses (Neustart, SIGTERM): erst den Geraetekern stoppen (sauberes Trennen), dann den Geraete-Thread
    sein laufendes Ereignis abschliessen lassen, z. B. software_soll nach konfiguration_uebernommen."""
    device.stop()
    if thread is not None:
        thread.join(timeout)
        if thread.is_alive():
            logger.warning("Geraete-Thread nach %.0f s nicht beendet, Prozess endet trotzdem", timeout)


def start(paths: Paths, bus, mirror: ZigbeeMirror, geraet: GatewayDeviceHost, device: Device, clock=time.monotonic,
          *, driver_threads: bool = True) -> tuple[Runtime | app.IdleBridge, GatewayHost]:
    host = GatewayHost(paths, bus, mirror, geraet, clock=clock, driver_threads=driver_threads)
    result = app.start(host, clock, mqtt_factory=device.laufzeit_kanal)
    attach(result, host, geraet, device)
    return result, host


def _neustart(device: Device, anlass: str) -> NoReturn:
    logger.info("%s, Laufzeit startet neu", anlass)
    device.stop()  # zuerst sauber trennen (MQTT-DISCONNECT); main() wartet danach auf den Geraete-Thread (beenden)
    raise SystemExit(0)


def attach(result: Runtime | app.IdleBridge, host: GatewayHost, geraet: GatewayDeviceHost, device: Device) -> None:
    """Haengt die Gateway-Ereignisse an den Worker und leitet die Neustarts der Laufzeit (abo.restart_process, auch im
    Ruhezustand Konfigurationsfehler ueber EV_RECHECK) auf den Neustart mit sauberem Trennen um. Im Ruhezustand
    abgemeldet bzw. abo_beendet bleibt EV_RECHECK das erneute Zuruecksetzen."""
    worker = result.worker

    def _reload(event: Event) -> None:
        _neustart(device, "Neue Konfiguration oder Abmelden")

    def _recheck(event: Event) -> None:
        _neustart(device, "Neupruefung nach Konfigurationsfehler")

    def _laufzeit_neustart() -> None:
        _neustart(device, "Neustart der Laufzeit (Abo wieder aktiv)")

    def _watch(event: Event) -> None:
        worker.schedule(CONFIG_WATCH_SECONDS, Event(EV_CONFIG_WATCH))  # zuerst: die Wache darf nie abreissen
        _touch_alive()

    abo.restart_process = _laufzeit_neustart  # statt os.execv: das risse den Link ohne DISCONNECT ab
    worker.register(EV_RELOAD, _reload)
    worker.register(EV_CONFIG_WATCH, _watch)
    worker.schedule(CONFIG_WATCH_SECONDS, Event(EV_CONFIG_WATCH))
    _touch_alive()
    if isinstance(result, Runtime) and host.raum is not None:
        raum = host.raum

        def _raum(event: Event) -> None:
            raum.publish()
            worker.schedule(RAUM_SECONDS, Event(EV_RAUM))

        worker.register(EV_RAUM, _raum)
        worker.schedule(0, Event(EV_RAUM))
    elif isinstance(result, app.IdleBridge):
        geraet.zustand.raum = None  # Ruhezustand: kein Raum der vorigen Einrichtung
        if result.reason == app.IDLE_NOT_CONFIGURED:
            geraet.zustand.status = None
        elif result.reason == STATUS_KONFIGURATIONSFEHLER:
            # bewusst zusaetzlich zur Umleitung von abo.restart_process: haengt nicht an diesem globalen Eingriff
            worker.register(EV_RECHECK, _recheck)
    geraet.worker_binden(worker)


def _terminate(signum, frame) -> None:
    raise SystemExit(0)


def main() -> None:  # pragma: no cover - Container-Einstieg
    logging.basicConfig(level=logging.INFO)
    paths = from_env()
    bus = LocalBus(os.environ.get("SHG_BUS_HOST", "mosquitto"), int(os.environ.get("SHG_BUS_PORT", "1883")),
                   "shg-runtime", credentials=credentials_from_env())
    mirror = ZigbeeMirror(bus, time.monotonic)
    mirror.start()
    bus.start()
    aufraeumen(paths)  # vor dem Geraetekern: kein Rennen mit driver_login oder inventory nach hello
    geraet = GatewayDeviceHost(paths, mirror, host_dir=Path(os.environ.get("SHG_HOST_DIR", "/host")))
    device = Device(geraet, paths.data, os.environ["SHG_DEVICE_API_URL"], befehl_fehler=befehle.fehler_uebersetzen)
    geraet.verbinden(device)
    portal = os.environ.get("SHG_PORTAL_BASE_URL", "")
    diagnose.start_server(
        int(os.environ.get("SHG_DIAG_PORT", "8080")), lambda: diagnose.seite(geraet, device),
        lambda: diagnose.qr_url(portal, device), diagnose.parse_hostnames(os.environ.get("SHG_DIAG_HOSTNAMES", "")),
    )
    software_soll_nachholen(geraet, device)  # vor dem Geraete-Thread: kein Rennen mit einer neuen Konfiguration
    signal.signal(signal.SIGTERM, _terminate)  # vor dem Geraete-Thread: ein SIGTERM endet immer ueber beenden
    thread: threading.Thread | None = None
    try:
        thread = device.start()
        result, _ = start(paths, bus, mirror, geraet, device)
        logger.info("Laufzeit gestartet (%s), Zeitzone %s", type(result).__name__, wallclock.now().tzinfo)
        result.worker.run()
    finally:
        beenden(device, thread)


if __name__ == "__main__":
    main()
