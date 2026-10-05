"""Einstieg des Add-ons: baut den HA-Host und startet die Laufzeit (smartheat_runtime.app). Das Add-on beendet sich
nie absichtlich, siehe app.py."""
import logging
import os
import time
from typing import NoReturn

from heizungsbruecke import config
from heizungsbruecke.ha_api import HomeAssistantApi
from heizungsbruecke.host import HaHost
from smartheat_runtime import app
from smartheat_runtime.runtime import Runtime


def _start_bridge(options: dict, ha_api, clock=time.monotonic) -> Runtime | app.IdleBridge:
    return app.start(HaHost(options, ha_api), clock)


def _run_bridge(options: dict, ha_api) -> NoReturn:
    """Laeuft, bis der Prozess beendet wird. Voruebergehende Fehler (HA nicht bereit, Broker nicht erreichbar, Server
    schweigt) und Endzustaende beenden das Add-on nie."""
    _start_bridge(options, ha_api).worker.run()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    options = config.load_options_safe(config.OPTIONS_PATH)
    ha_api = HomeAssistantApi(base_url="http://supervisor", token=os.environ["SUPERVISOR_TOKEN"])
    _run_bridge(options, ha_api)


if __name__ == "__main__":
    main()
