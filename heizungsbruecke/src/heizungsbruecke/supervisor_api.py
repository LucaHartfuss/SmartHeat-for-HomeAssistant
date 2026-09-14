"""Duenner Client fuer die Supervisor-eigene Management-API (nicht die HA-Core-API,
die ha_api.py bedient -- anderer Pfad, kein `/core`-Praefix). Aktuell nur die eine
Operation, die der Wizard braucht: die eigenen Add-on-Optionen schreiben.
"""

import requests


def set_own_options(base_url: str, token: str, options: dict) -> None:
    response = requests.post(
        f"{base_url}/addons/self/options",
        headers={"Authorization": f"Bearer {token}"},
        json={"options": options},
        timeout=10,
    )
    response.raise_for_status()
