"""Einstieg des Gateways (Spec 5b 5.3, Plan D2): ein Prozess fuer Geraetekern (smartheat_device, eigener Thread) und
Laufzeit (smartheat_runtime, Regel-Worker im Hauptthread). Agent-Container, HTTPS-Polling und Tunnel entfallen; Tick
und Telemetrie laufen ueber den Link des Geraets (mqtt_factory).

Neustart und Ende teilen sich beide Hosts in smartheat_device.prozess: eine laufzeitrelevante neue Konfiguration oder
sign_off fordert den Neustart an (EV_RELOAD), ebenso die Neustarts der Laufzeit selbst (Rueckruf
prozess.neustart_der_laufzeit aus app.start: Neupruefung im Ruhezustand Konfigurationsfehler, Abo am Fristende wieder
aktiv, Neustart nach dem Zuruecksetzen). Der Worker endet mit prozess.Neustart, beenden trennt zuerst den Link sauber
(MQTT-DISCONNECT; sonst meldet IoT Core DUPLICATE_CLIENTID, wenn der neue Prozess verbindet, und der Dienst saehe nach
drei Aenderungen eine Kopie, Spec 4.2), laesst den Geraete-Thread sein laufendes Ereignis abschliessen, und main kehrt
zurueck: Exit 0, Compose startet neu.
Nach einem unerwarteten Fehler der Laufzeit trennt das Gateway ebenso, wartet 60 s und startet dann neu. SIGTERM
(docker stop) endet ueber beenden ohne Neustart. Jeder Start reicht die Soll-Software der gespeicherten Konfiguration
erneut an den Updater (software_soll_nachholen).

Nach dem Entfernen (Markierung aufraeumen.json) setzt der naechste Start die Anlage erst zurueck wie sign_off (Laufzeit
im Modus abgemeldet mit der alten Konfiguration aus der Markierung, die Treiber-Anmeldung braucht es dafuer noch) und
startet danach neu; erst dieser Start loescht den anlagengebundenen Zustand (A4-07, Spec Stufe 3 S3 Abschnitt 4), bevor
der Geraetekern startet - aber nur, solange die gespeicherte Konfiguration noch leer ist: kam vor dem Neustart schon
eine neue Einrichtung, bleibt ihre frische Treiber-Anmeldung. Die Konfigurationswache haelt nur noch das Lebenszeichen
fuer den Compose-Health-Check frisch (G2 7.2)."""
import contextlib
import logging
import os
import signal
import time
from pathlib import Path

from smartheat_core import wallclock
from smartheat_device import prozess
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
from smartheat_runtime import app
from smartheat_runtime.runtime import Runtime
from smartheat_runtime.worker import Event

logger = logging.getLogger(__name__)

EV_CONFIG_WATCH = "shg_config_watch"
EV_RAUM = "shg_raum"
CONFIG_WATCH_SECONDS = 30
RAUM_SECONDS = 30


def _alive_file() -> Path:
    """Lebenszeichen fuer den Health-Check von Compose (G2 7.2): die Wache beruehrt es alle 30 s."""
    return Path(os.environ.get("SHG_ALIVE_FILE", "/tmp/shg-runtime-alive"))


def _touch_alive() -> None:
    with contextlib.suppress(OSError):  # ein nicht beschreibbares /tmp darf die Wache nicht beenden
        _alive_file().touch()


def aufraeumen(paths: Paths) -> None:
    """Markierung aufraeumen.json vor dem Start des Geraetekerns (prozess.aufraeumen_beim_start): noch nichts schreibt,
    und ein driver_login direkt nach hello kann nicht mit dem Loeschen kollidieren. Geloescht wird mit vergessen."""
    prozess.aufraeumen_beim_start(
        paths.aufraeumen,
        lambda: DocumentStore(paths.device_dir, host=GatewayDeviceHost.host,
                              software_version=GATEWAY_VERSION).inhalt(KONFIGURATION),
        lambda: _vergessen(paths), wer="das Gateway")


def _vergessen(paths: Paths) -> bool:
    geraet_host_module.vergessen(paths)  # wirft nie
    return True


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


def start(paths: Paths, bus, mirror: ZigbeeMirror, geraet: GatewayDeviceHost, device: Device, clock=time.monotonic,
          *, driver_threads: bool = True) -> tuple[Runtime | app.IdleBridge, GatewayHost]:
    host = GatewayHost(paths, bus, mirror, geraet, clock=clock, driver_threads=driver_threads)
    result = app.start(host, clock, mqtt_factory=device.laufzeit_kanal, neustart=prozess.neustart_der_laufzeit)
    attach(result, host, geraet)
    return result, host


def attach(result: Runtime | app.IdleBridge, host: GatewayHost, geraet: GatewayDeviceHost) -> None:
    """Haengt die Gateway-Ereignisse (Lebenszeichen, Raum) an den Worker; EV_RELOAD geht ueber
    prozess.neustarts_umleiten, die Neustarts der Laufzeit selbst ueber den Rueckruf aus start."""
    worker = result.worker

    def _watch(event: Event) -> None:
        worker.schedule(CONFIG_WATCH_SECONDS, Event(EV_CONFIG_WATCH))  # zuerst: die Wache darf nie abreissen
        _touch_alive()

    prozess.neustarts_umleiten(result)
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
    geraet.worker_binden(worker, soll_eingang=isinstance(result, Runtime))  # Ruhezustand: kein Soll-Eingang


def _starten(paths: Paths, bus, mirror: ZigbeeMirror, geraet: GatewayDeviceHost, device: Device):
    result, _ = start(paths, bus, mirror, geraet, device)
    logger.info("Zeitzone %s", wallclock.now().tzinfo)
    return result


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
    prozess.laufen(device, geraet.hintergrund, lambda: _starten(paths, bus, mirror, geraet, device), lambda: None)


if __name__ == "__main__":
    main()
