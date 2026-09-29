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
import json
import os
import subprocess
import time
import uuid

import pytest
import requests
import websocket

from heizungsbruecke.ha_api import HomeAssistantApi
from heizungsbruecke.helper_templates import outdoor_temperature_template, room_temperature_template

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_REAL_HA_TESTS") != "1",
    reason="Startet einen echten HA-Core-Container per Docker; nur mit RUN_REAL_HA_TESTS=1 aktiv.",
)

# CI pinnt bei Push-Laeufen die client1-Version, der taegliche Lauf nimmt stable (Spec 2.2).
HA_IMAGE = os.environ.get("HA_IMAGE", "ghcr.io/home-assistant/home-assistant:stable")
HA_PORT = 18213

# Docker-Aufruf, z.B. REAL_HA_DOCKER="flatpak-spawn --host docker" aus der VS-Code-Flatpak-Sandbox.
DOCKER = os.environ.get("REAL_HA_DOCKER", "docker").split()


@pytest.fixture(scope="module")
def real_ha():
    container_name = f"heizungsbruecke-real-ha-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        [*DOCKER, "run", "-d", "--rm", "--name", container_name, "-p", f"{HA_PORT}:8123", HA_IMAGE],
        check=True,
    )
    base_url = f"http://localhost:{HA_PORT}"
    try:
        _wait_for_ha_ready(base_url)
        token = _complete_onboarding(base_url)
        yield base_url, token
    finally:
        subprocess.run([*DOCKER, "stop", container_name], check=False)


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


def test_get_config_returns_time_zone_from_real_ha(real_ha):
    """B6: GET /api/config liefert die HA-Zeitzone, gegen die das Add-on beim Start
    seine Container-Zeitzone prueft."""
    base_url, token = real_ha
    api = HomeAssistantApi(base_url=base_url, token=token, api_prefix="/api")

    time_zone = api.get_config()["time_zone"]

    assert isinstance(time_zone, str)
    assert time_zone


def test_create_input_number_creates_a_real_working_helper(real_ha):
    """Verifiziert create_input_number() end-to-end gegen echte HA (Task 4: per Websocket).

    Der REST-Versuch aus Task 2 (`POST /api/config/input_number/config/<object_id>`)
    scheiterte mit 404 -- siehe Git-Historie und der Docstring von
    create_input_number() in ha_api.py fuer die Details. Seit Task 4 nutzt die
    Methode das Websocket-Kommando `input_number/create`
    (`homeassistant.helpers.collection.StorageCollectionWebsocket`), verifiziert
    hier per echtem Roundtrip: der Helfer entsteht tatsaechlich in HA und sein
    Zustand ist ueber die normale REST-States-API lesbar.

    Der zurueckgegebene entity_id-String wird nur auf das `input_number.`-Praefix
    geprueft, nicht auf einen exakten Wert: HA leitet den Objekt-Teil per
    `slugify(name)` her (siehe Docstring von create_input_number()).
    """
    base_url, token = real_ha
    api = HomeAssistantApi(base_url=base_url, token=token, api_prefix="/api")

    entity_id = api.create_input_number(
        name="Test Tagesmittel",
        minimum=0.0, maximum=35.0, step=0.01, initial=20.0,
    )

    assert entity_id.startswith("input_number.")
    assert api.get_state(entity_id) == 20.0


def test_set_input_number_value_updates_real_state(real_ha):
    """Nutzt einen per api.create_input_number() frisch angelegten Helfer als reale Entity
    (statt, wie vor Task 4, einen statisch per YAML in real_ha vorprovisionierten)."""
    base_url, token = real_ha
    api = HomeAssistantApi(base_url=base_url, token=token, api_prefix="/api")
    entity_id = api.create_input_number(
        name="Test Set Value",
        minimum=0.0, maximum=35.0, step=0.01, initial=18.0,
    )

    api.set_input_number_value(entity_id, 23.5)

    assert api.get_state(entity_id) == 23.5


def test_entity_exists_true_for_created_entity_false_for_unknown(real_ha):
    base_url, token = real_ha
    api = HomeAssistantApi(base_url=base_url, token=token, api_prefix="/api")
    entity_id = api.create_input_number(
        name="Test Entity Exists",
        minimum=0.0, maximum=35.0, step=0.01, initial=18.0,
    )

    assert api.entity_exists(entity_id) is True
    assert api.entity_exists("input_number.does_not_exist_at_all") is False


def test_create_statistics_sensor_config_flow_creates_a_loaded_config_entry(real_ha):
    """Verifiziert eigenstaendig NUR den Config-Entry-Flow-Teil von create_statistics_sensor().

    HINTERGRUND: der `statistics`-Integrationsflow verlangt eine reale, in der
    Domain "sensor" oder "binary_sensor" liegende Quell-Entity (per Selector im
    ersten Formular-Schritt serverseitig erzwungen -- ein input_number wird mit
    "Entity ... belongs to domain input_number, expected ['binary_sensor',
    'sensor']" abgelehnt). Ein per api.create_input_number() angelegter Helfer
    eignet sich dafuer also NICHT. Statt die gemeinsame real_ha-Fixture fuer
    diesen einen Test zu erweitern, wird hier sensor.sun_next_dawn verwendet --
    eine von HAs `sun`-Integration (Teil von `default_config`, siehe
    real_ha-Fixture) immer automatisch angelegte, damit garantiert vorhandene
    Sensor-Entity. Ihr nicht-numerischer Zustand (ISO-8601-Zeitstempel) ist fuer
    diesen Test unerheblich: geprueft wird hier nur, dass der Config-Entry-Flow
    selbst durchlaeuft und einen geladenen Config-Entry erzeugt -- nicht die
    korrekte Berechnung des Mittelwerts.

    Dieser Test verwendet absichtlich NICHT api.create_statistics_sensor()
    direkt, sondern ruft _start_config_flow()/_advance_config_flow() einzeln
    auf: so bleibt er unabhaengig von test_create_statistics_sensor_creates_a_real_working_helper()
    unten (das den vollen End-to-End-Pfad inkl. Entity-Lookup abdeckt) und
    verifiziert eigenstaendig nur den Flow-Teil.
    """
    base_url, token = real_ha
    api = HomeAssistantApi(base_url=base_url, token=token, api_prefix="/api")

    flow_response = api._start_config_flow("statistics")
    result = api._advance_config_flow(flow_response, {
        "name": "Test Mean",
        "entity_id": "sensor.sun_next_dawn",
        "state_characteristic": "mean",
        "max_age": {"hours": 12},
        "sampling_size": 255,
        "precision": 2,
    })

    assert result["type"] == "create_entry"
    entry_id = result["result"]["entry_id"]

    config_entries_response = requests.get(
        f"{base_url}/api/config/config_entries/entry",
        headers={"Authorization": f"Bearer {token}"},
        params={"domain": "statistics"},
        timeout=10,
    )
    config_entries_response.raise_for_status()
    matching_entries = [entry for entry in config_entries_response.json() if entry["entry_id"] == entry_id]

    assert len(matching_entries) == 1
    assert matching_entries[0]["state"] == "loaded"


def test_create_statistics_sensor_creates_a_real_working_helper(real_ha):
    """Verifiziert create_statistics_sensor() end-to-end gegen echte HA (Task 4: Entity-Lookup per Websocket).

    Analog zu test_create_input_number_creates_a_real_working_helper() oben, aber
    fuer eine andere API-Flaeche: der Config-Entry-Flow-Teil von
    create_statistics_sensor() funktionierte bereits per REST (siehe Test
    oberhalb) -- aber die anschliessende Aufloesung "Config-Entry-ID ->
    Entity-ID" ueber `GET /api/config/entity_registry/list` schlug mit 404 fehl
    (Quellcode-Pruefung: homeassistant/components/config/entity_registry.py
    registriert dafuer ausschliesslich Websocket-Kommandos, keine REST-Route).
    Seit Task 4 nutzt _find_entity_by_config_entry() dafuer das WS-Kommando
    `config/entity_registry/list` -- verifiziert hier per echtem Roundtrip: die
    Methode liefert jetzt tatsaechlich eine nutzbare, existierende Entity-ID.

    Nutzt sensor.sun_next_dusk (statt sun_next_dawn im Test oberhalb), damit
    beide Tests unabhaengige Config-Entries anlegen.
    """
    base_url, token = real_ha
    api = HomeAssistantApi(base_url=base_url, token=token, api_prefix="/api")

    entity_id = api.create_statistics_sensor(
        name="Test Mean 2", source_entity_id="sensor.sun_next_dusk", max_age_hours=12,
    )

    assert entity_id.startswith("sensor.")
    assert api.entity_exists(entity_id)


def _wait_for_state(api, entity_id: str, expected: str, timeout: float = 15.0) -> str | None:
    """Template-Sensoren rechnen asynchron nach einer Quellenaenderung neu."""
    deadline = time.time() + timeout
    state = None
    while time.time() < deadline:
        state = _raw(api, entity_id)
        if state == expected:
            return state
        time.sleep(0.5)
    return state


def _raw(api, entity_id: str) -> str | None:
    response = requests.get(
        f"{api._base_url}{api._api_prefix}/states/{entity_id}", headers=api._headers, timeout=10,
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()["state"]


def _set_state(api, entity_id: str, state: str, attributes: dict) -> None:
    """Setzt eine Test-Fixture-Entity per rohem REST-POST.

    HomeAssistantApi kennt seit Task 7 (B4, Wegfall der Status-Entity) kein set_state()
    mehr -- das Add-on selbst braucht die Methode nicht mehr. Diese Tests hier simulieren
    aber weiterhin Sensor-/Wetter-Quellen fuer die Template-Sensor-Tests unten, dafuer
    reicht ein lokaler Roundtrip auf denselben Endpunkt, den get_state()/create_*
    ohnehin verwenden.
    """
    response = requests.post(
        f"{api._base_url}{api._api_prefix}/states/{entity_id}",
        headers=api._headers,
        json={"state": state, "attributes": attributes},
        timeout=10,
    )
    response.raise_for_status()


def test_room_template_sensor_averages_valid_sources_and_is_unknown_without(real_ha):
    """Spec TP6 3.2/6: Mittelwert mit einem toten und einem unplausiblen Fuehler; alle tot ->
    unknown (nicht "None" als Text, nicht 0)."""
    base_url, token = real_ha
    api = HomeAssistantApi(base_url=base_url, token=token, api_prefix="/api")
    _set_state(api, "sensor.tp6_a", "20.0", {"unit_of_measurement": "°C"})
    _set_state(api, "sensor.tp6_b", "unavailable", {})
    _set_state(api, "sensor.tp6_c", "0.0", {"unit_of_measurement": "°C"})
    _set_state(api, "climate.tp6_d", "heat", {"current_temperature": 21.0, "temperature": 22.0})

    entity_id = api.create_template_sensor(
        name="SmartHeat realtest Raumtemperatur",
        template=room_temperature_template(
            ["sensor.tp6_a", "sensor.tp6_b", "sensor.tp6_c", "climate.tp6_d::current_temperature"],
        ),
    )

    assert entity_id == "sensor.smartheat_realtest_raumtemperatur"
    assert _wait_for_state(api, entity_id, "20.5") == "20.5"

    _set_state(api, "sensor.tp6_a", "unknown", {})
    _set_state(api, "climate.tp6_d", "heat", {"current_temperature": None, "temperature": 22.0})
    assert _wait_for_state(api, entity_id, "unknown") == "unknown"


def test_outdoor_template_sensor_reads_weather_temperature(real_ha):
    base_url, token = real_ha
    api = HomeAssistantApi(base_url=base_url, token=token, api_prefix="/api")
    _set_state(api, "weather.tp6_home", "sunny", {"temperature": 3.2, "temperature_unit": "°C"})

    entity_id = api.create_template_sensor(
        name="SmartHeat realtest Außentemperatur", template=outdoor_temperature_template("weather.tp6_home"),
    )

    assert _wait_for_state(api, entity_id, "3.2") == "3.2"


def test_deleted_and_recreated_helpers_keep_their_entity_id(real_ha):
    """Spec TP6 3.3: ein Quellwechsel loescht den Helfer und legt ihn neu an. Die Entity-ID muss
    gleich bleiben (kein _2), sonst zeigen Dashboards und Recorder-Historie ins Leere."""
    base_url, token = real_ha
    api = HomeAssistantApi(base_url=base_url, token=token, api_prefix="/api")
    _set_state(api, "sensor.tp6_src", "20.0", {"unit_of_measurement": "°C"})

    template_id = api.create_template_sensor(name="SmartHeat recreate Raumtemperatur", template="{{ 20 }}")
    api.delete_helper(template_id)
    assert api.create_template_sensor(name="SmartHeat recreate Raumtemperatur", template="{{ 21 }}") == template_id

    stats_id = api.create_statistics_sensor(name="SmartHeat recreate DART", source_entity_id="sensor.tp6_src", max_age_hours=24)
    api.delete_helper(stats_id)
    assert api.create_statistics_sensor(
        name="SmartHeat recreate DART", source_entity_id=template_id, max_age_hours=24,
    ) == stats_id


def test_fire_event_reaches_the_event_bus(real_ha):
    """Spec TP7 1.1: das Status-Event kommt ueber POST /api/events am Bus an (die Integration hoert
    dort mit hass.bus.async_listen)."""
    base_url, token = real_ha
    api = HomeAssistantApi(base_url=base_url, token=token, api_prefix="/api")
    ws = websocket.create_connection(api.websocket_url(), timeout=10)
    try:
        ws.recv()
        ws.send(json.dumps({"type": "auth", "access_token": token}))
        assert json.loads(ws.recv())["type"] == "auth_ok"
        ws.send(json.dumps({"id": 1, "type": "subscribe_events", "event_type": "smartheat_status"}))
        assert json.loads(ws.recv())["success"] is True

        api.fire_event("smartheat_status", {"schema": 1, "tenant_id": "realtest", "status": "regelt"})

        message = json.loads(ws.recv())
    finally:
        ws.close()

    assert message["type"] == "event"
    assert message["event"]["event_type"] == "smartheat_status"
    assert message["event"]["data"] == {"schema": 1, "tenant_id": "realtest", "status": "regelt"}


def test_is_reachable_once_real_ha_reports_running(real_ha):
    """Review I2: is_reachable() verlangt `state == "RUNNING"` in GET /api/config. Prueft gegen
    echtes HA, dass das Feld so heisst und ein fertig gestartetes HA RUNNING meldet (sonst
    wartete das Add-on ewig)."""
    base_url, token = real_ha
    api = HomeAssistantApi(base_url=base_url, token=token, api_prefix="/api")

    deadline = time.time() + 60
    while api.get_config().get("state") != "RUNNING" and time.time() < deadline:
        time.sleep(1)

    assert api.get_config()["state"] == "RUNNING"
    assert api.is_reachable() is True
    assert HomeAssistantApi(base_url=base_url, token="falsch", api_prefix="/api").is_reachable() is False
