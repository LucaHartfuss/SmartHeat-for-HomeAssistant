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
import tempfile
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

# Statisch per YAML angelegter input_number-Helper, siehe _seed_static_input_number().
STATIC_INPUT_NUMBER_OBJECT_ID = "smartheat_test_static_helper"
STATIC_INPUT_NUMBER_ENTITY_ID = f"input_number.{STATIC_INPUT_NUMBER_OBJECT_ID}"
STATIC_INPUT_NUMBER_INITIAL = 20.0


@pytest.fixture(scope="module")
def real_ha():
    container_name = f"heizungsbruecke-real-ha-test-{uuid.uuid4().hex[:8]}"
    # Container per `create` statt `run` anlegen: so kann die Konfiguration (inkl.
    # des statischen input_number-Helpers, siehe _seed_static_input_number()) per
    # `docker cp` VOR dem ersten Start hineingelegt werden. Vermeidet einen
    # zusaetzlichen `docker restart`-Zyklus, der sich beim Entwickeln dieser
    # Fixture als unzuverlaessig erwiesen hat (siehe Kommentar dort).
    subprocess.run(
        ["docker", "create", "--rm", "--name", container_name, "-p", f"{HA_PORT}:8123", HA_IMAGE],
        check=True,
    )
    base_url = f"http://localhost:{HA_PORT}"
    try:
        _seed_static_input_number(container_name)
        subprocess.run(["docker", "start", container_name], check=True)
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


def _seed_static_input_number(container_name: str) -> None:
    """Legt einen input_number-Helper per YAML an, BEVOR der Container zum ersten Mal startet.

    HINTERGRUND (Ergebnis der Verifikation fuer Task 2, siehe auch der Docstring
    von HomeAssistantApi.create_input_number()): der im Task-Brief vorgesehene
    Entwurf fuer create_input_number() -- `POST /api/config/input_number/config/
    <object_id>` -- liefert gegen einen echten HA-Core-Container (2026.9.2) einen
    404. Quellcode-Pruefung im Container bestaetigt, dass es dafuer in dieser
    Version *keine* REST-Route mehr gibt: `homeassistant/components/config/
    __init__.py` registriert keine input_number-Sektion (die frueher existierende
    `config/input_number.py` ist entfernt), und `homeassistant/components/
    input_number/__init__.py` legt Helfer ausschliesslich ueber
    `DictStorageCollectionWebsocket` an -- also per Websocket-Kommando
    (`input_number/create`), nicht per REST.

    Um set_input_number_value() und entity_exists() trotzdem gegen einen echten,
    tatsaechlich existierenden input_number-Entity verifizieren zu koennen (statt
    diese Tests aufzugeben oder abzuschwaechen), wird hier -- NUR fuer diese
    Testfixture, nicht als Ersatz fuer create_input_number() selbst -- ein Helfer
    ganz regulaer per YAML angelegt.

    Ein erster Versuch hat configuration.yaml per `docker exec` NACH dem ersten
    Start erweitert und dann per `docker restart` neu geladen -- das erwies sich
    als unzuverlaessig (in wiederholten Laeufen kam die REST-API nach dem
    Neustart teils erst nach >150s wieder hoch, teils gar nicht, mit
    "RemoteDisconnected"-Fehlern; vermutlich ein Windows/Docker-Desktop-Problem
    beim erneuten Binden desselben Host-Ports). Stattdessen wird die komplette
    Konfiguration (inkl. des input_number-Blocks) VOR dem allerersten Start per
    `docker cp` in den noch nicht gestarteten Container kopiert -- dadurch gibt es
    nur einen einzigen, bereits erprobt zuverlaessigen Boot-Zyklus (wie in
    _wait_for_ha_ready() fuer Task 1 bereits verifiziert).

    Wichtig dabei: configuration.yaml verweist per `!include` auf automations.yaml/
    scripts.yaml/scenes.yaml. Diese existieren NICHT als leeres Skeleton im
    Docker-Image -- HA legt sie normalerweise beim allerersten Boot selbst an.
    Wird configuration.yaml vorab hineinkopiert, ohne dass diese drei Dateien
    existieren, scheitert das Parsen beim Erststart
    ("Unable to read file /config/automations.yaml") und HA faellt in den
    Recovery-Modus (dort laeuft zwar die Onboarding-API, aber ohne den
    input_number-Helper) -- deshalb werden hier alle vier Dateien vorab angelegt.
    Das wurde an einem echten Container gegenpruft.
    """
    static_input_number_yaml = (
        "# Loads default set of integrations. Do not remove.\n"
        "default_config:\n"
        "\n"
        "# Load frontend themes from the themes folder\n"
        "frontend:\n"
        "  themes: !include_dir_merge_named themes\n"
        "\n"
        "automation: !include automations.yaml\n"
        "script: !include scripts.yaml\n"
        "scene: !include scenes.yaml\n"
        "\n"
        "input_number:\n"
        f"  {STATIC_INPUT_NUMBER_OBJECT_ID}:\n"
        "    name: SmartHeat Test Static\n"
        "    min: 0\n"
        "    max: 35\n"
        "    step: 0.01\n"
        f"    initial: {STATIC_INPUT_NUMBER_INITIAL}\n"
    )
    files_to_seed = {
        "configuration.yaml": static_input_number_yaml,
        "automations.yaml": "[]\n",
        "scripts.yaml": "{}\n",
        "scenes.yaml": "[]\n",
    }
    with tempfile.TemporaryDirectory() as tmp_dir:
        for filename, content in files_to_seed.items():
            local_path = os.path.join(tmp_dir, filename)
            with open(local_path, "w", encoding="utf-8") as file:
                file.write(content)
            subprocess.run(
                ["docker", "cp", local_path, f"{container_name}:/config/{filename}"],
                check=True,
            )


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


def test_create_input_number_has_no_rest_equivalent_in_this_ha_version(real_ha):
    """Dokumentiert einen verifizierten Negativbefund, statt ihn zu verschweigen.

    Der im Task-Brief vorgesehene Entwurf fuer create_input_number() --
    `POST /api/config/input_number/config/<object_id>` -- liefert gegen den
    echten HA-Core-Container (2026.9.2) einen 404. Das ist keine falsche
    Payload/URL-Variante, die sich reparieren liesse: Quellcode-Pruefung im
    Container zeigt, dass diese REST-Route in dieser Version ueberhaupt nicht
    mehr registriert wird (siehe Docstring von create_input_number() in ha_api.py
    fuer die Details). Der Task-Brief instruiert fuer genau diesen Fall,
    zu stoppen und zurueckzumelden statt stillschweigend auf einen
    Websocket-Client umzusteigen -- das ist eine groessere Architektur-
    entscheidung mit eigener Brainstorming-Runde. Dieser Test haelt das
    verifizierte Verhalten fest, damit eine kuenftige HA-Version, die die Route
    wieder einfuehrt (oder ein Fix von create_input_number), hier sichtbar
    auffallen wuerde.
    """
    base_url, token = real_ha
    api = HomeAssistantApi(base_url=base_url, token=token, api_prefix="/api")

    with pytest.raises(requests.exceptions.HTTPError) as exc_info:
        api.create_input_number(
            object_id="smartheat_test_room_day_avg", name="Test Tagesmittel",
            minimum=0.0, maximum=35.0, step=0.01, initial=20.0,
        )

    assert exc_info.value.response.status_code == 404


def test_set_input_number_value_updates_real_state(real_ha):
    """Nutzt den YAML-provisionierten Helfer aus real_ha (STATIC_INPUT_NUMBER_ENTITY_ID),
    da api.create_input_number() -- siehe Test oben -- in dieser HA-Version nicht
    funktioniert.
    """
    base_url, token = real_ha
    api = HomeAssistantApi(base_url=base_url, token=token, api_prefix="/api")

    api.set_input_number_value(STATIC_INPUT_NUMBER_ENTITY_ID, 23.5)

    assert api.get_state(STATIC_INPUT_NUMBER_ENTITY_ID) == 23.5


def test_entity_exists_true_for_created_entity_false_for_unknown(real_ha):
    base_url, token = real_ha
    api = HomeAssistantApi(base_url=base_url, token=token, api_prefix="/api")

    assert api.entity_exists(STATIC_INPUT_NUMBER_ENTITY_ID) is True
    assert api.entity_exists("input_number.does_not_exist_at_all") is False
