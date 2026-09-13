"""Integrationstest gegen einen echten Home-Assistant-Core-Docker-Container.

Anders als test_ha_api.py (das nur `requests` mockt) ruft dieser Test die REST-API
eines tatsaechlich laufenden HA-Core-Containers auf. Zweck: die in
docs/superpowers/specs/2026-09-13-heizungsbruecke-config-vereinfachung-design.md,
Abschnitt 3.4 als unsicher gekennzeichneten Annahmen ueber Home Assistants
Config-Entry-Flow- und Helper-Storage-APIs an echtem Verhalten verifizieren, bevor
darauf aufgebaut wird -- nicht danach.

Standardmaessig uebersprungen (braucht Docker, dauert ~30-90s Containerstart):
mit RUN_REAL_HA_TESTS=1 aktivieren, z.B.:
    RUN_REAL_HA_TESTS=1 python -m pytest tests/test_ha_api_real_ha_integration.py -v -s
"""
import os
import subprocess
import time
import uuid

import pytest
import requests

from heizungsbruecke.ha_api import HomeAssistantApi

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_REAL_HA_TESTS") != "1",
    reason="Startet einen echten HA-Core-Container per Docker; nur mit RUN_REAL_HA_TESTS=1 aktiv.",
)

HA_IMAGE = "ghcr.io/home-assistant/home-assistant:stable"
HA_PORT = 18213


@pytest.fixture(scope="module")
def real_ha():
    container_name = f"heizungsbruecke-real-ha-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", container_name, "-p", f"{HA_PORT}:8123", HA_IMAGE],
        check=True,
    )
    base_url = f"http://localhost:{HA_PORT}"
    try:
        _wait_for_ha_ready(base_url)
        token = _complete_onboarding(base_url)
        yield base_url, token
    finally:
        subprocess.run(["docker", "stop", container_name], check=False)


def _wait_for_ha_ready(base_url: str, timeout: float = 90.0) -> None:
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            response = requests.get(f"{base_url}/api/onboarding", timeout=2)
            if response.status_code == 200:
                return
        except requests.exceptions.RequestException as error:
            last_error = error
        time.sleep(2)
    raise TimeoutError(f"HA unter {base_url} wurde nicht rechtzeitig bereit: {last_error}")


def _complete_onboarding(base_url: str) -> str:
    """Fuehrt HAs Erstinbetriebnahme per REST durch und liefert einen Bearer-Token.

    EHRLICHER HINWEIS zum tatsaechlich beobachteten Verhalten (verifiziert gegen
    ghcr.io/home-assistant/home-assistant:stable, Core-Version 2026.9.2 -- der
    Entwurf dieser Funktion im Task-Brief war eine unverifizierte Annahme und musste
    an drei Stellen korrigiert werden:

    1. `POST /api/onboarding/core_config` nimmt in dieser Core-Version KEINEN
       Request-Body entgegen (`CoreConfigOnboardingView.post` in
       homeassistant/components/onboarding/views.py liest `request` gar nicht erst
       aus). Er markiert nur den Onboarding-Schritt als erledigt und stoesst im
       Hintergrund ein paar Default-Integrationen an (google_translate, met,
       radio_browser, shopping_list). Standort/Zeitzone/Waehrung etc. lassen sich
       in dieser Version NICHT ueber diesen REST-Endpunkt setzen -- das passiert
       ausschliesslich ueber den Websocket-Befehl `config/core/update`
       (homeassistant/components/config/core.py), der keine REST-Entsprechung hat.
       Deshalb bleibt zone.home nach dem Onboarding bei den Defaults
       latitude=0/longitude=0/radius=100 stehen, unabhaengig davon, was man an
       core_config schickt. Fuer Task 1 reicht das: der Zweck dieses Tests ist zu
       beweisen, dass get_state() mit api_prefix="/api" gegen eine echte,
       fertig onboardete HA-Instanz funktioniert -- nicht, das core_config-Verhalten
       im Detail zu spezifizieren. Falls eine spaetere Aufgabe echte Config-Werte
       (Standort etc.) braucht, muss sie ueber die Websocket-API gehen.
    2. Der `analytics`-Schritt (`POST /api/onboarding/analytics`, ebenfalls ohne
       Body) fehlte im Entwurf komplett. Ohne ihn bleibt der Onboarding-Datensatz
       dauerhaft auf `done: False` fuer diesen Schritt stehen (sichtbar in
       `GET /api/onboarding`), auch wenn alle anderen Schritte erledigt sind. Fuer
       eine als "fertig onboardet" gedachte Fixture fuer Task 2/3 wird er hier mit
       abgeschlossen.
    3. `client_id` beim Token-Request und Feldnamen/-typen bei den Requests
       (`auth_code`, `access_token`) entsprachen bereits dem Entwurf und mussten
       nicht angepasst werden.
    """
    client_id = f"{base_url}/"
    user_response = requests.post(
        f"{base_url}/api/onboarding/users",
        json={
            "client_id": client_id,
            "name": "Test",
            "username": "test",
            "password": "test-password-123",
            "language": "de",
        },
        timeout=10,
    )
    user_response.raise_for_status()
    auth_code = user_response.json()["auth_code"]

    token_response = requests.post(
        f"{base_url}/auth/token",
        data={"grant_type": "authorization_code", "code": auth_code, "client_id": client_id},
        timeout=10,
    )
    token_response.raise_for_status()
    access_token = token_response.json()["access_token"]

    headers = {"Authorization": f"Bearer {access_token}"}
    requests.post(
        f"{base_url}/api/onboarding/core_config",
        headers=headers,
        timeout=10,
    ).raise_for_status()
    requests.post(
        f"{base_url}/api/onboarding/integration",
        headers=headers,
        json={"client_id": client_id, "redirect_uri": client_id},
        timeout=10,
    ).raise_for_status()
    requests.post(
        f"{base_url}/api/onboarding/analytics",
        headers=headers,
        timeout=10,
    ).raise_for_status()

    return access_token


def test_get_state_reads_zone_home_radius_from_real_ha(real_ha):
    """Smoke-Test: die bestehende get_state()-Methode muss auch mit api_prefix='/api'
    gegen einen echten (nicht Supervisor-proxied) Container funktionieren.

    zone.home wird von HA immer automatisch angelegt (Teil von default_config,
    unabhaengig vom Onboarding-Fortschritt); sein radius-Attribut hat einen
    stabilen Default von 100 Metern, solange er nicht ueber die (REST-seitig nicht
    erreichbare) Config-Websocket-API geaendert wird -- siehe Kommentar in
    _complete_onboarding(). Das macht ihn zu einem deterministischen Wert, an dem
    sich verifizieren laesst, dass die authentifizierte REST-Anfrage gegen die
    echte, fertig onboardete Instanz tatsaechlich durchkommt.
    """
    base_url, token = real_ha
    api = HomeAssistantApi(base_url=base_url, token=token, api_prefix="/api")

    radius = api.get_state("zone.home::radius")

    assert radius == 100.0
