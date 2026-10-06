"""Einstieg der Laufzeit (Spec SHG G2 3.5, Plan G2a Praezisierung 4): baut den GatewayHost, startet
smartheat_runtime.app und haengt die Gateway-Ereignisse an den Worker: shg/cmd/reload und eine eigene
Konfigurationswache (beide beenden den Prozess mit Exit 0 zwischen zwei Worker-Ereignissen, Compose startet neu) und
den Raum-Kanal. Im Ruhezustand "nicht eingerichtet" raeumt sie retained Status und Raum ab (ein Schreiber je Topic)."""
import logging
import os
import time
from pathlib import Path

from smartheat_core import wallclock
from smartheat_gateway import config as gateway_config
from smartheat_gateway import topics
from smartheat_gateway.bus import LocalBus
from smartheat_gateway.host import GatewayHost
from smartheat_gateway.paths import Paths, from_env
from smartheat_runtime import app
from smartheat_runtime.runtime import Runtime
from smartheat_runtime.worker import Event

logger = logging.getLogger(__name__)

EV_RELOAD = "shg_reload"
EV_CONFIG_WATCH = "shg_config_watch"
EV_RAUM = "shg_raum"
CONFIG_WATCH_SECONDS = 30
RAUM_SECONDS = 30


def _alive_file() -> Path:
    """Lebenszeichen fuer den Health-Check von Compose (G2 7.2): die Konfigurationswache beruehrt es alle 30 s."""
    return Path(os.environ.get("SHG_ALIVE_FILE", "/tmp/shg-runtime-alive"))


def _config_key(raw: dict) -> tuple:
    return raw.get("setup_id"), raw.get("abgemeldet") is True, gateway_config.is_configured(raw)


def start(paths: Paths, bus, clock=time.monotonic, *, driver_threads: bool = True):
    host = GatewayHost(paths, bus, clock=clock, driver_threads=driver_threads)
    result = app.start(host, clock)
    attach(result, host, bus, paths)
    return result, host


def attach(result, host: GatewayHost, bus, paths: Paths) -> None:
    worker = result.worker
    # Vergleichsstand = die Konfiguration, die der Host geladen hat (nicht erneut von der Platte: eine Aenderung
    # zwischen Laden und Anhaengen ginge sonst verloren).
    loaded_key = _config_key(host.raw)

    def _exit(event: Event) -> None:
        logger.info("Neue Konfiguration, Laufzeit startet neu")
        raise SystemExit(0)

    def _watch(event: Event) -> None:
        if _config_key(gateway_config.load_raw(paths)) != loaded_key:
            _exit(event)
        _alive_file().touch()
        worker.schedule(CONFIG_WATCH_SECONDS, Event(EV_CONFIG_WATCH))

    worker.register(EV_RELOAD, _exit)
    worker.register(EV_CONFIG_WATCH, _watch)
    _alive_file().touch()
    worker.schedule(CONFIG_WATCH_SECONDS, Event(EV_CONFIG_WATCH))
    bus.subscribe(topics.CMD_RELOAD, lambda topic, raw, retain: None if retain else worker.post(Event(EV_RELOAD)))
    if isinstance(result, Runtime) and host.raum is not None:
        raum = host.raum

        def _raum(event: Event) -> None:
            raum.publish()
            worker.schedule(RAUM_SECONDS, Event(EV_RAUM))

        worker.register(EV_RAUM, _raum)
        worker.schedule(0, Event(EV_RAUM))
    elif isinstance(result, app.IdleBridge) and result.reason == app.IDLE_NOT_CONFIGURED:
        bus.publish(topics.STATUS, None, retain=True)
        bus.publish(topics.RAUM, None, retain=True)


def main() -> None:  # pragma: no cover - Container-Einstieg
    logging.basicConfig(level=logging.INFO)
    bus = LocalBus(
        os.environ.get("SHG_BUS_HOST", "mosquitto"), int(os.environ.get("SHG_BUS_PORT", "1883")), "shg-runtime",
    )
    bus.start()
    result, _ = start(from_env(), bus)
    logger.info("Laufzeit gestartet (%s), Zeitzone %s", type(result).__name__, wallclock.now().tzinfo)
    result.worker.run()


if __name__ == "__main__":
    main()
