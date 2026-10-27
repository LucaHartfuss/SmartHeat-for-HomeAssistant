"""Einstieg des Gateways: ein Prozess fuer Geraetekern (smartheat_device, eigener Thread) und
Laufzeit (smartheat_runtime, Regel-Worker im Hauptthread). Tick
und Telemetrie laufen ueber den Link des Geraets (mqtt_factory).

Neustart und Ende teilen sich beide Hosts in smartheat_device.prozess: eine laufzeitrelevante neue Konfiguration oder
sign_off fordert den Neustart an (EV_RELOAD), ebenso die Neustarts der Laufzeit selbst (Rueckruf
prozess.neustart_der_laufzeit aus app.start: Neupruefung im Ruhezustand Konfigurationsfehler, Abo am Fristende wieder
aktiv, Neustart nach dem Zuruecksetzen). Der Worker endet mit prozess.Neustart, beenden trennt zuerst den Link sauber
(MQTT-DISCONNECT; sonst meldet IoT Core DUPLICATE_CLIENTID, wenn der neue Prozess verbindet, und der Dienst saehe nach
drei Aenderungen eine Kopie), laesst den Geraete-Thread sein laufendes Ereignis abschliessen, und main kehrt
zurueck: Exit 0, Compose startet neu.
Nach einem unerwarteten Fehler der Laufzeit trennt das Gateway ebenso, meldet sich ungesund (vor_der_pause), wartet
60 s und startet dann neu; ebenso nach einem Fehler in den Schritten davor (vorbereiten: Aufraeumen, Geraete-Host,
Geraetekern, Diagnoseserver). SIGTERM (docker stop) endet ueber beenden ohne Neustart. Jeder Start
reicht das gemerkte Software-Angebot erneut an den Updater (software_soll_nachholen).

Nach dem Entfernen (Markierung aufraeumen.json) setzt der naechste Start die Anlage erst zurueck wie sign_off (Laufzeit
im Modus abgemeldet mit der alten Konfiguration aus der Markierung, die Treiber-Anmeldung braucht es dafuer noch) und
startet danach neu; erst dieser Start loescht den anlagengebundenen Zustand, bevor
der Geraetekern startet - aber nur, solange die gespeicherte Konfiguration noch leer ist: kam vor dem Neustart schon
eine neue Einrichtung, bleibt ihre frische Treiber-Anmeldung. Die Konfigurationswache haelt nur noch das Lebenszeichen
fuer den Compose-Health-Check frisch."""
import contextlib
import logging
import os
import signal
import time
from collections.abc import Callable
from pathlib import Path

from smartheat_core import wallclock
from smartheat_device import prozess
from smartheat_device.device import Device
from smartheat_device.documents import KONFIGURATION, DocumentStore
from smartheat_gateway import befehle, diagnose
from smartheat_gateway import geraet_host as geraet_host_module
from smartheat_gateway.anmeldung import WERK_DATEI, Zugang
from smartheat_gateway.bus import BUS_PORT, LocalBus, credentials_from_env
from smartheat_gateway.geraet_host import GatewayDeviceHost
from smartheat_gateway.host import GatewayHost
from smartheat_gateway.paths import Paths, from_env
from smartheat_gateway.version import GATEWAY_VERSION
from smartheat_gateway.zigbee import ZigbeeMirror
from smartheat_runtime import app
from smartheat_runtime.aufraeumen import Markierung
from smartheat_runtime.runtime import Runtime
from smartheat_runtime.status import STATUS_STARTET
from smartheat_runtime.worker import Event

logger = logging.getLogger(__name__)

EV_CONFIG_WATCH = "shg_config_watch"
EV_RAUM = "shg_raum"
CONFIG_WATCH_SECONDS = 30
RAUM_SECONDS = 30
DIAG_PORT = 8080  # Port der Diagnoseseite im Container; die Compose-Datei veroeffentlicht ihn als 0.0.0.0:80
# Port nur fuer /healthz (Compose-Healthcheck im Container, Updater am Host); die Compose-Datei veroeffentlicht ihn nur
# als 127.0.0.1:8081. Eigener Server: Verbindungen aus dem LAN auf Port 80 nehmen ihm keine Plaetze weg (S-10).
HEALTH_PORT = 8081


def _alive_file() -> Path:
    """Lebenszeichen fuer den Health-Check von Compose: die Wache beruehrt es alle 30 s."""
    return Path(os.environ.get("SHG_ALIVE_FILE", "/tmp/shg-runtime-alive"))


def _touch_alive() -> None:
    with contextlib.suppress(OSError):  # ein nicht beschreibbares /tmp darf die Wache nicht beenden
        _alive_file().touch()


def aufraeumen(paths: Paths) -> Markierung | None:
    """Markierung aufraeumen.json vor dem Start des Geraetekerns (prozess.aufraeumen_beim_start): noch nichts schreibt,
    und ein driver_login direkt nach hello kann nicht mit dem Loeschen kollidieren. Geloescht wird mit vergessen.
    Rueckgabe: die Markierung, wenn das Zuruecksetzen aussteht (_vorbereiten reicht sie an
    GatewayDeviceHost.start_markierung; die Laufzeit liest sie nicht erneut)."""
    return prozess.aufraeumen_beim_start(
        paths.aufraeumen,
        lambda: DocumentStore(paths.device_dir, host=GatewayDeviceHost.host,
                              software_version=GATEWAY_VERSION, rasten=False).inhalt(KONFIGURATION),
        lambda: _vergessen(paths), wer="das Gateway")


def _vergessen(paths: Paths) -> bool:
    return geraet_host_module.vergessen(paths)  # wirft nie; False laesst die Markierung fuer den naechsten Start


def software_soll_nachholen(geraet: GatewayDeviceHost, device: Device) -> None:
    """Das gemerkte Software-Angebot (DocumentStore.software_angebot) erneut an den Updater (idempotent),
    bevor der Geraete-Thread laeuft: ein Neustart (EV_RELOAD aus konfiguration_uebernommen) oder ein verlorener
    Schreibvorgang kann software_soll.json fehlen lassen, und der Server stellt eine bestaetigte oder abgelehnte Version
    nicht erneut zu. Gemerkt ist das zuletzt gueltige Angebot, auch aus einer abgelehnten
    Konfiguration; ein zurueckgezogenes (software null) geht als null weiter (der Updater liest es als "kein Angebot"),
    damit keine zurueckgezogene Version aus einer alten software_soll.json installiert wird. Ohne gemerktes Angebot
    (dokumente.json ohne software_angebot) wird der Updater nicht gerufen."""
    angebot = device.documents.software_angebot()
    if angebot is None:
        return
    try:
        geraet.software_soll(angebot.software)
    except Exception as error:  # ein Schreibfehler darf den Start nicht verhindern; der naechste versucht es erneut
        logger.error("Soll-Software nicht nachgeholt (%s)", type(error).__name__)


def start(paths: Paths, bus, mirror: ZigbeeMirror, geraet: GatewayDeviceHost, device: Device, clock=time.monotonic,
          *, driver_threads: bool = True) -> tuple[Runtime | app.IdleBridge, GatewayHost]:
    host = GatewayHost(paths, bus, mirror, geraet, clock=clock, driver_threads=driver_threads)
    result = app.start(host, clock, mqtt_factory=device.laufzeit_kanal, neustart=prozess.neustart_der_laufzeit)
    attach(result, host, geraet)
    return result, host


def attach(result: Runtime | app.IdleBridge, host: GatewayHost, geraet: GatewayDeviceHost) -> None:
    """Haengt die Gateway-Ereignisse (Lebenszeichen, Raum) an den Worker; EV_RELOAD geht ueber
    prozess.reload_anhaengen, die Neustarts der Laufzeit selbst ueber den Rueckruf aus start."""
    worker = result.worker

    def _watch(event: Event) -> None:
        worker.schedule(CONFIG_WATCH_SECONDS, Event(EV_CONFIG_WATCH))  # zuerst: die Wache darf nie abreissen
        _touch_alive()

    prozess.reload_anhaengen(result)
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


def vor_der_pause(geraet: GatewayDeviceHost | None) -> None:
    """Rueckruf vor_pause fuer prozess.laufen und prozess.starten: nach
    einem unerwarteten Fehler meldet sich das Gateway fuer die 60-s-Pause ungesund, damit der Updater ein
    Update, dessen Laufzeit gleich nach dem Start wirft, zurueckrollt. Der Prozess bleibt in der Pause oben: das
    Lebenszeichen waere frisch (Compose-Health-Check "healthy"; im Start nur ohne tmpfs auf /tmp aus dem vorigen Lauf
    im selben Container, Compose legt /tmp als tmpfs an), und /healthz hinge nur an server_ok aus dem letzten Takt des
    Geraete-Threads. Deshalb geht das Lebenszeichen weg (der Health-Check scheitert), und /healthz meldet die Laufzeit
    als "startet" (health_ok des Updaters lehnt das ab); der Neustart beginnt ohnehin so. Ohne Geraete-Host (Fehler
    davor, geraet None) laeuft auch der Diagnoseserver noch nicht: /healthz antwortet nicht, und health_ok lehnt ebenso
    ab."""
    with contextlib.suppress(OSError):  # ein nicht beschreibbares /tmp darf Pause und Neustart nicht verhindern
        _alive_file().unlink(missing_ok=True)
    if geraet is not None:
        geraet.zustand.status = {"status": STATUS_STARTET}


def laufen(paths: Paths, bus, mirror: ZigbeeMirror, geraet: GatewayDeviceHost, device: Device) -> None:
    """prozess.laufen mit Exit 0 als Neustart (Compose startet neu) und vor_der_pause (Moduldocstring)."""
    prozess.laufen(device, geraet.hintergrund, lambda: _starten(paths, bus, mirror, geraet, device), lambda: None,
                   vor_pause=lambda: vor_der_pause(geraet))


def _terminate(signum, frame) -> None:
    raise SystemExit(0)


def _vorbereiten(paths: Paths, mirror: ZigbeeMirror,
                 angelegt: Callable[[GatewayDeviceHost], None]) -> tuple[GatewayDeviceHost, Device]:
    """Die Host-Schritte vor laufen; angelegt bekommt den Geraete-Host, sobald es ihn gibt (fuer vor_der_pause)."""
    ausstehend = aufraeumen(paths)  # vor dem Geraetekern: kein Rennen mit driver_login oder inventory nach hello
    host_dir = Path(os.environ.get("SHG_HOST_DIR", "/host"))
    geraet = GatewayDeviceHost(paths, mirror, host_dir=host_dir)
    geraet.start_markierung = ausstehend
    angelegt(geraet)
    device = Device(geraet, paths.data, os.environ["SHG_DEVICE_API_URL"], befehl_fehler=befehle.fehler_uebersetzen)
    geraet.verbinden(device)
    hostnames = diagnose.parse_hostnames(os.environ.get("SHG_DIAG_HOSTNAMES", ""))
    # Die Seite auf Port 80 verlangt die Anmeldung mit dem Etikett-Passwort aus werk.json (Kopie des Hosts, GW 8)
    diagnose.start_server(DIAG_PORT, lambda: diagnose.seite(geraet, device), hostnames,
                          zugang=Zugang(host_dir / WERK_DATEI))
    # Dieselben erlaubten Host-Namen: der Docker-Test des Updaters fragt den Dienst `runtime` im Test-Netz.
    diagnose.start_server(HEALTH_PORT, lambda: diagnose.seite(geraet, device), hostnames, nur_health=True)
    software_soll_nachholen(geraet, device)  # vor dem Geraete-Thread: kein Rennen mit einer neuen Konfiguration
    return geraet, device


def vorbereiten(paths: Paths, mirror: ZigbeeMirror) -> tuple[GatewayDeviceHost, Device] | None:
    """prozess.starten mit _vorbereiten, Exit 0 als Neustart und vor_der_pause: ein Fehler vor laufen (Aufraeumen,
    DocumentStore, Geraete-Host, Diagnoseport) endet ungesund in der 60-s-Pause, dann None."""
    angelegt: list[GatewayDeviceHost] = []
    return prozess.starten(lambda: _vorbereiten(paths, mirror, angelegt.append), lambda: None,
                           vor_pause=lambda: vor_der_pause(angelegt[0] if angelegt else None))


def main() -> None:  # pragma: no cover - Container-Einstieg
    logging.basicConfig(level=logging.INFO)
    signal.signal(signal.SIGTERM, _terminate)  # vor dem Start: ein SIGTERM endet immer ueber SystemExit bzw. beenden
    # Bus, Spiegel und Zugangsdaten bewusst vor prozess.starten: ein Fehler hier endet ohne Pause (Compose-Backoff;
    # noch kein Geraetekern, IoT Core unberuehrt)
    paths = from_env()
    bus = LocalBus(os.environ.get("SHG_BUS_HOST", "mosquitto"), BUS_PORT,
                   "shg-runtime", credentials=credentials_from_env())
    mirror = ZigbeeMirror(bus, time.monotonic)
    mirror.start()
    bus.start()
    teile = vorbereiten(paths, mirror)
    if teile is None:  # Startfehler: ungesund, Pause gelaufen; Exit 0, Compose startet neu
        return
    geraet, device = teile
    laufen(paths, bus, mirror, geraet, device)


if __name__ == "__main__":
    main()
