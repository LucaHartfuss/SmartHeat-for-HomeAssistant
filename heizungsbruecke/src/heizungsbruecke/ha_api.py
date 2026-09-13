import json

import requests
import websocket


class HomeAssistantApi:
    def __init__(self, base_url: str, token: str, api_prefix: str = "/core/api"):
        self._base_url = base_url
        self._api_prefix = api_prefix
        self._token = token
        self._headers = {"Authorization": f"Bearer {token}"}

    def _websocket_url(self) -> str:
        """Baut die Websocket-URL aus `_base_url`/`_api_prefix`.

        Der Task-Brief-Entwurf hierfuer (`_api_prefix.rsplit("/api", 1)[0] +
        "/websocket"`, also ein zu `/api` GESCHWISTER-Pfad) war eine falsche
        Annahme: gegen einen echten HA-Core-Container (2026.9.2, api_prefix=
        "/api") lieferte das `ws://.../websocket` einen 404-Handshake-Fehler.
        Quellcode-Pruefung bestaetigt: `homeassistant/components/websocket_api/
        const.py` definiert `URL = "/api/websocket"` -- die Websocket-Route
        liegt also UNTER demselben `/api`-Praefix wie die REST-Routen, nicht
        daneben. Deshalb einfach `_api_prefix + "/websocket"` anhaengen (fuer
        api_prefix="/api" ergibt das korrekt "/api/websocket").
        """
        ws_base = self._base_url.replace("https://", "wss://").replace("http://", "ws://")
        return f"{ws_base}{self._api_prefix}/websocket"

    def _call_ws_command(self, command: dict) -> dict:
        """Oeffnet eine kurzlebige WebSocket-Verbindung, authentifiziert sich und
        fuehrt genau ein Kommando aus, dann schliesst die Verbindung wieder --
        kein Verbindungs-Pooling, kein Dauerbetrieb. Wird nur ein paar Mal beim
        Start aufgerufen (derived_sensors.ensure_all), nicht im Tick-Loop.
        """
        ws = websocket.create_connection(self._websocket_url(), timeout=10)
        try:
            auth_required = json.loads(ws.recv())
            if auth_required.get("type") != "auth_required":
                raise RuntimeError(f"Unerwartete erste WS-Nachricht: {auth_required}")

            ws.send(json.dumps({"type": "auth", "access_token": self._token}))
            auth_result = json.loads(ws.recv())
            if auth_result.get("type") != "auth_ok":
                raise RuntimeError(f"WS-Authentifizierung fehlgeschlagen: {auth_result}")

            ws.send(json.dumps({"id": 1, **command}))
            result = json.loads(ws.recv())
            if not result.get("success"):
                raise RuntimeError(f"WS-Kommando fehlgeschlagen: {result.get('error')}")
            return result["result"]
        finally:
            ws.close()

    def get_state(self, entity_id: str) -> float:
        real_entity_id, _, attribute = entity_id.partition("::")

        response = requests.get(
            f"{self._base_url}{self._api_prefix}/states/{real_entity_id}",
            headers=self._headers,
            timeout=10,
        )
        response.raise_for_status()
        body = response.json()

        if attribute:
            return float(body["attributes"][attribute])
        return float(body["state"])

    def set_number_value(self, entity_id: str, value: float) -> None:
        response = requests.post(
            f"{self._base_url}{self._api_prefix}/services/number/set_value",
            headers=self._headers,
            json={"entity_id": entity_id, "value": value},
            timeout=10,
        )
        response.raise_for_status()

    def create_input_number(
        self, object_id: str, name: str, minimum: float, maximum: float, step: float, initial: float
    ) -> str:
        """Legt einen input_number-Helper per Websocket an und liefert seine Entity-ID.

        Ersetzt seit Task 4 den REST-Versuch aus Task 2, der an einem echten
        HA-Core-Container (2026.9.2) mit 404 scheiterte: `POST .../config/
        input_number/config/<object_id>` existiert in dieser HA-Version nicht
        mehr als REST-Route (Quellcode-Pruefung: `homeassistant/components/
        input_number/__init__.py` registriert Helfer-Erzeugung ausschliesslich
        ueber `collection.DictStorageCollectionWebsocket(storage_collection,
        "input_number", "input_number", STORAGE_FIELDS, STORAGE_FIELDS)`, was
        u.a. das Websocket-Kommando `input_number/create` registriert -- siehe
        `homeassistant.helpers.collection.StorageCollectionWebsocket.async_setup`).
        Verifiziert per echtem WS-Roundtrip gegen denselben Container (siehe
        tests/test_ha_api_real_ha_integration.py).

        WICHTIG, per Quellcode bestaetigt: `object_id` wird von HA NICHT als
        Entity-ID uebernommen. Das Create-Schema (`STORAGE_FIELDS` in
        input_number/__init__.py) akzeptiert nur `name`/`min`/`max`/`initial`/
        `step`/`icon`/`unit_of_measurement`/`mode` -- kein `id`-Feld; die
        `BASE_COMMAND_MESSAGE_SCHEMA` des Websocket-Kommandos ist strikt
        (`vol.Schema` ohne `extra=ALLOW_EXTRA`), ein zusaetzliches `id`/
        `object_id`-Feld wuerde also ohnehin mit "extra keys not allowed"
        abgelehnt. Der tatsaechliche Objekt-Teil der Entity-ID ist
        `IDManager.generate_id(name)`, also ein `slugify(name)` mit
        Kollisions-Suffix (`_2`, `_3`, ...) bei Namenskonflikten -- unabhaengig
        vom hier uebergebenen `object_id`. Der Parameter bleibt aus
        Signaturkompatibilitaet (siehe Task-Interface) erhalten, wird aber
        aktuell nicht verwendet; Aufrufer duerfen sich nicht auf
        `input_number.<object_id>` als Ergebnis verlassen, sondern muessen den
        zurueckgegebenen String verwenden.
        """
        result = self._call_ws_command({
            "type": "input_number/create",
            "name": name,
            "min": minimum,
            "max": maximum,
            "step": step,
            "initial": initial,
        })
        return f"input_number.{result['id']}"

    def set_input_number_value(self, entity_id: str, value: float) -> None:
        response = requests.post(
            f"{self._base_url}{self._api_prefix}/services/input_number/set_value",
            headers=self._headers,
            json={"entity_id": entity_id, "value": value},
            timeout=10,
        )
        response.raise_for_status()

    def create_statistics_sensor(self, name: str, source_entity_id: str, max_age_hours: float) -> str:
        """Legt einen `statistics`-Sensor (gleitender Mittelwert) per Config-Entry-Flow an.

        `state_characteristic="average_step"` (zeitgewichteter Mittelwert -- gewichtet
        jeden Messwert mit der Dauer bis zum naechsten Update) statt des naheliegenderen
        `"mean"` (einfacher arithmetischer Mittelwert der Samples), plus
        `keep_last_sample=True`: auf dem Live-Pi von Kunde 1 bereits vorhandene,
        offenbar mit der aktuellen Heizkurve kalibrierte DAT/DART-Helfer (angelegt
        2026-09-08, per `.storage/core.config_entries` verifiziert) nutzen exakt diese
        beiden Werte. Ein Wechsel auf `"mean"` wuerde bei unregelmaessig aktualisierenden
        Temperatursensoren einen anderen DAT/DART-Wert liefern und damit lautlos die
        Eingabedaten der proprietaeren Heizkurve verschieben -- deshalb hier bewusst an
        die live-kalibrierten Werte angeglichen statt einer Neu-Definition.

        ZWEI GETRENNTE VERIFIKATIONSERGEBNISSE gegen einen echten HA-Core-
        Container (2026.9.2, siehe tests/test_ha_api_real_ha_integration.py):

        1. Der Config-Entry-Flow selbst (`POST .../config/config_entries/flow`
           zum Start, `POST .../config/config_entries/flow/<flow_id>` zum
           Abschluss) FUNKTIONIERT per REST und wurde -- anders als die alten
           Helper-Storage-Views (siehe create_input_number()) -- nicht entfernt.
           Anders als im urspruenglichen Task-Brief angenommen ist der
           `statistics`-Flow aber KEIN einstufiger Formular-Flow, sondern
           DREISTUFIG ("user": name+entity_id -> "state_characteristic":
           state_characteristic -> "options": sampling_size/max_age/precision/
           etc.). Jeder Schritt akzeptiert per Voluptuous-Schema ausschliesslich
           die in seinem eigenen `data_schema` genannten Feldnamen; zusaetzliche
           Felder fuehren zu 400 ("not a valid option at ..."). Das war eine
           falsche Annahme im Brief (Payload-Form), keine fehlende Route --
           siehe _advance_config_flow().
        2. Die Aufloesung von Config-Entry-ID zu Entity-ID
           (_find_entity_by_config_entry()) lief per REST (`GET .../config/
           entity_registry/list`) ebenfalls auf einen 404 -- Quellcode-Pruefung
           im Container (homeassistant/components/config/entity_registry.py)
           zeigte, dass diese Datei ausschliesslich `websocket_api.
           async_register_command(...)` registriert, keine einzige
           `HomeAssistantView`-Klasse. Seit Task 4 nutzt
           _find_entity_by_config_entry() dafuer das Websocket-Kommando
           `config/entity_registry/list` (registriert in derselben Datei,
           siehe dortiger Docstring) -- verifiziert per echtem WS-Roundtrip
           gegen denselben Container.
        """
        fields = {
            "name": name,
            "entity_id": source_entity_id,
            "state_characteristic": "average_step",
            "keep_last_sample": True,
            "max_age": {"hours": max_age_hours},
            "sampling_size": 255,
            "precision": 2,
        }
        flow_response = self._start_config_flow("statistics")
        result = self._advance_config_flow(flow_response, fields)
        return self._find_entity_by_config_entry(result["result"]["entry_id"])

    def _start_config_flow(self, handler: str) -> dict:
        """Startet einen Config-Entry-Flow und liefert die volle erste Formular-Antwort.

        Liefert bewusst das gesamte JSON (nicht nur `flow_id` wie im
        urspruenglichen Brief-Entwurf): die erste Antwort enthaelt bereits das
        `data_schema` des ersten Schritts, das _advance_config_flow() braucht,
        um zu wissen, welche Felder dieser Schritt ueberhaupt akzeptiert (siehe
        dortiger Docstring zum verifizierten Mehrstufigkeits-Befund).
        """
        response = requests.post(
            f"{self._base_url}{self._api_prefix}/config/config_entries/flow",
            headers=self._headers,
            json={"handler": handler, "show_advanced_options": False},
            timeout=10,
        )
        response.raise_for_status()
        return response.json()

    def _advance_config_flow(self, flow_response: dict, all_fields: dict) -> dict:
        """Durchlaeuft einen mehrstufigen Config-Entry-Flow bis zum Abschluss.

        Filtert bei jedem Formular-Schritt `all_fields` auf die im jeweils
        zurueckgegebenen `data_schema` genannten Feldnamen und schickt nur diese
        Teilmenge -- siehe create_statistics_sensor()-Docstring, Punkt 1, fuer
        die Verifikation, dass der `statistics`-Flow genau das braucht.
        """
        result = flow_response
        while result.get("type") == "form":
            allowed_field_names = {field["name"] for field in result["data_schema"]}
            step_payload = {key: value for key, value in all_fields.items() if key in allowed_field_names}
            result = self._finish_config_flow(result["flow_id"], step_payload)
        return result

    def _finish_config_flow(self, flow_id: str, data: dict) -> dict:
        response = requests.post(
            f"{self._base_url}{self._api_prefix}/config/config_entries/flow/{flow_id}",
            headers=self._headers,
            json=data,
            timeout=10,
        )
        response.raise_for_status()
        return response.json()

    def _find_entity_by_config_entry(self, config_entry_id: str) -> str:
        """Loest eine Config-Entry-ID per Websocket zu ihrer Entity-ID auf.

        Siehe create_statistics_sensor()-Docstring, Punkt 2: `GET .../config/
        entity_registry/list` hat in dieser HA-Version (2026.9.2) keine
        REST-Entsprechung (Quellcode-Pruefung: homeassistant/components/config/
        entity_registry.py registriert nur Websocket-Kommandos). Das WS-Kommando
        `config/entity_registry/list` (registriert per
        `@websocket_api.websocket_command({"type": "config/entity_registry/list"})`
        in derselben Datei) liefert -- anders als bei den meisten anderen
        WS-Kommandos, die ihr Ergebnis in ein `{"entity_categories": ...,
        "entities": [...]}`-Dict packen (siehe `.../list_for_display`) -- eine
        BARE LISTE von Registry-Eintraegen (`entry.partial_json_repr`, siehe
        `homeassistant/helpers/entity_registry.py`, `as_partial_dict`); jeder
        Eintrag enthaelt u.a. `entity_id` und `config_entry_id`. Verifiziert per
        echtem WS-Roundtrip gegen einen echten HA-Core-Container.
        """
        entries = self._call_ws_command({"type": "config/entity_registry/list"})
        for entry in entries:
            if entry.get("config_entry_id") == config_entry_id:
                return entry["entity_id"]
        raise RuntimeError(f"Keine Entity fuer Config-Entry '{config_entry_id}' in der Entity-Registry gefunden")

    def entity_exists(self, entity_id: str) -> bool:
        response = requests.get(
            f"{self._base_url}{self._api_prefix}/states/{entity_id}",
            headers=self._headers,
            timeout=10,
        )
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return True

    def send_notification(self, notify_service: str, message: str) -> None:
        """Calls a Home Assistant notify service, e.g. `notify.mobile_app_lucas_iphone`.

        Raises like every other method here; catching is the caller's job, since a
        failed push notification must never take down the path that triggered it.
        """
        domain, _, service = notify_service.partition(".")

        response = requests.post(
            f"{self._base_url}{self._api_prefix}/services/{domain}/{service}",
            headers=self._headers,
            json={"message": message},
            timeout=10,
        )
        response.raise_for_status()
