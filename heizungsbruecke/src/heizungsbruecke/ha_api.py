import json

import requests
import websocket

# Plattformen der Helfer, die derived_sensors per Config-Flow anlegt (und nur die darf
# delete_helper loeschen).
HELPER_PLATFORMS = ("statistics", "template")
# `state` in GET /api/config, sobald HA fertig gestartet ist (homeassistant.core.CoreState).
HA_STATE_RUNNING = "RUNNING"


class HomeAssistantApi:
    def __init__(self, base_url: str, token: str, api_prefix: str = "/core/api"):
        self._base_url = base_url
        self._api_prefix = api_prefix
        self._token = token
        self._headers = {"Authorization": f"Bearer {token}"}

    def websocket_url(self) -> str:
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

    @property
    def token(self) -> str:
        return self._token

    def _call_ws_command(self, command: dict) -> dict:
        """Oeffnet eine kurzlebige WebSocket-Verbindung, authentifiziert sich und
        fuehrt genau ein Kommando aus, dann schliesst die Verbindung wieder --
        kein Verbindungs-Pooling, kein Dauerbetrieb. Wird nur ein paar Mal beim
        Start aufgerufen (derived_sensors.ensure_all), nicht im Tick-Loop.
        """
        ws = websocket.create_connection(self.websocket_url(), timeout=10)
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

    def get_raw_state(self, entity_id: str) -> str:
        """Returns the entity's state string as-is (no float cast), for text sensors
        such as operating_mode. HA's failure states ("unavailable", "unknown", "") are
        raised as errors so callers omit the field instead of storing them as data;
        "::attr" references are not supported here and raise ValueError."""
        if "::" in entity_id:
            raise ValueError(f"get_raw_state unterstuetzt keine Attribut-Referenz: {entity_id!r}")
        real_entity_id = entity_id
        response = requests.get(
            f"{self._base_url}{self._api_prefix}/states/{real_entity_id}",
            headers=self._headers,
            timeout=10,
        )
        response.raise_for_status()
        state = response.json()["state"]
        if state in ("unavailable", "unknown", ""):
            raise ValueError(f"Entity {real_entity_id} hat keinen gueltigen Zustand: {state!r}")
        return state

    def get_config(self) -> dict:
        """GET /api/config -- u.a. `time_zone` der HA-Instanz (Startpruefung der
        Container-Zeitzone)."""
        response = requests.get(
            f"{self._base_url}{self._api_prefix}/config",
            headers=self._headers,
            timeout=10,
        )
        response.raise_for_status()
        return response.json()

    def set_number_value(self, entity_id: str, value: float) -> None:
        response = requests.post(
            f"{self._base_url}{self._api_prefix}/services/number/set_value",
            headers=self._headers,
            json={"entity_id": entity_id, "value": value},
            timeout=10,
        )
        response.raise_for_status()

    def set_climate_temperature(self, entity_id: str, value: float) -> None:
        """Wunschtemperatur einer Climate-Entity (TP11: Parallelverschiebung = Zonen-Sollwert).
        Bei mypyllant im Modus "Manuell" dauerhaft (set_manual_mode_setpoint)."""
        response = requests.post(
            f"{self._base_url}{self._api_prefix}/services/climate/set_temperature",
            headers=self._headers,
            json={"entity_id": entity_id, "temperature": value},
            timeout=10,
        )
        response.raise_for_status()

    def set_hvac_mode(self, entity_id: str, mode: str) -> None:
        response = requests.post(
            f"{self._base_url}{self._api_prefix}/services/climate/set_hvac_mode",
            headers=self._headers,
            json={"entity_id": entity_id, "hvac_mode": mode},
            timeout=10,
        )
        response.raise_for_status()

    def delete_input_number(self, entity_id: str) -> None:
        """Loescht einen input_number-Helfer (TP11: Tag-/Nachtmittel entfallen). Nur input_number.*
        mit dem Objekt-Teil-Praefix `smartheat_` (wie delete_helper() bei den Config-Entry-Helfern:
        nie ein fremder Helfer), per WS-Kommando input_number/delete mit dem Objekt-Teil der Entity-ID."""
        domain, _, object_id = entity_id.partition(".")
        if domain != "input_number" or not object_id:
            raise RuntimeError(f"'{entity_id}' ist kein input_number-Helfer, wird nicht geloescht")
        if not object_id.startswith("smartheat_"):
            raise RuntimeError(f"'{entity_id}' ist kein SmartHeat-Hilfssensor, wird nicht geloescht")
        self._call_ws_command({"type": "input_number/delete", "input_number_id": object_id})

    def create_template_sensor(self, name: str, template: str) -> str:
        """Legt einen Template-Sensor (Temperatur in °C) per Config-Entry-Flow `template` an:
        erst der Menue-Schritt `sensor`, dann das Formular mit Name, Template, Einheit,
        Device- und State-Class. Die Entity-ID entsteht aus slugify(name), wie bei den
        Statistik-Helfern. Gegen einen echten HA-Container verifiziert
        (tests/test_ha_api_real_ha_integration.py)."""
        fields = {
            "next_step_id": "sensor",
            "name": name,
            "state": template,
            "unit_of_measurement": "°C",
            "device_class": "temperature",
            "state_class": "measurement",
        }
        flow_response = self._start_config_flow("template")
        result = self._advance_config_flow(flow_response, fields)
        return self._find_entity_by_config_entry(result["result"]["entry_id"])

    def delete_helper(self, entity_id: str) -> None:
        """Loescht den Config-Entry eines per Config-Flow angelegten Helfers (Template- oder
        Statistik-Sensor). input_number-Helfer loescht delete_input_number.

        Nur Entities der Plattformen HELPER_PLATFORMS: die Entity-ID stammt aus
        derived_sensors.json. Ist die Datei kaputt, von einer anderen Installation oder die ID
        inzwischen an eine andere Entity vergeben, wuerde sonst der Config-Entry einer fremden
        Integration (z.B. mypyllant) geloescht -- nicht rueckgaengig zu machen. Dann wirft es,
        ohne etwas zu loeschen. Zusaetzlich wie bei delete_input_number nur Entities mit dem
        Objekt-Teil-Praefix `smartheat_` (eine fremde template-/statistics-Entity bleibt)."""
        object_id = entity_id.partition(".")[2]
        if not object_id.startswith("smartheat_"):
            raise RuntimeError(f"'{entity_id}' ist kein SmartHeat-Hilfssensor, wird nicht geloescht")
        entries = self._call_ws_command({"type": "config/entity_registry/list"})
        entry = next((entry for entry in entries if entry.get("entity_id") == entity_id), {})
        config_entry_id = entry.get("config_entry_id")
        if not config_entry_id:
            raise RuntimeError(f"Kein Config-Entry zu '{entity_id}' in der Entity-Registry gefunden")
        if entry.get("platform") not in HELPER_PLATFORMS:
            raise RuntimeError(
                f"'{entity_id}' ist kein SmartHeat-Hilfssensor (Plattform {entry.get('platform')!r}), "
                "wird nicht geloescht"
            )
        response = requests.delete(
            f"{self._base_url}{self._api_prefix}/config/config_entries/entry/{config_entry_id}",
            headers=self._headers,
            timeout=10,
        )
        response.raise_for_status()

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

        Formular-Schritt: `all_fields` wird auf die im `data_schema` genannten Feldnamen
        gefiltert. Gegen einen echten HA-Core-Container (2026.9.2) verifiziert: der
        Config-Entry-Flow laeuft per REST (`POST .../config/config_entries/flow` zum Start,
        `POST .../config/config_entries/flow/<flow_id>` je Schritt), Flows koennen aber
        mehrstufig sein, und jeder Schritt akzeptiert per Voluptuous-Schema ausschliesslich die
        in seinem eigenen `data_schema` genannten Feldnamen; zusaetzliche Felder fuehren zu 400
        ("not a valid option at ..."). Menue-Schritt (z.B. der erste Schritt des
        `template`-Flows): Auswahl ueber `all_fields["next_step_id"]`.
        """
        result = flow_response
        while result.get("type") in ("form", "menu"):
            if result["type"] == "menu":
                step_payload = {"next_step_id": all_fields["next_step_id"]}
            else:
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

        `GET .../config/entity_registry/list` hat in dieser HA-Version (2026.9.2) keine
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

    def fire_event(self, event_type: str, data: dict) -> None:
        """Feuert ein Ereignis auf dem HA-Event-Bus (POST /api/events/<typ>): Status-Kanal zur
        SmartHeat-Integration (Spec TP7 1.1). Wirft wie send_notification; abfangen ist Sache des
        Aufrufers."""
        response = requests.post(
            f"{self._base_url}{self._api_prefix}/events/{event_type}",
            headers=self._headers,
            json=data,
            timeout=10,
        )
        response.raise_for_status()

    def is_reachable(self) -> bool:
        """HA Core ist fertig gestartet (`GET /api/config` meldet `state` RUNNING); nur dann
        zaehlt das Retry-Budget des Starts. Antworten allein reicht nicht: der HTTP-Server laeuft
        schon frueh im Bootstrap, Integrationen wie mypyllant laden erst spaeter, und bis dahin
        fehlen ihre Entities (404). Wirft nie."""
        try:
            config = self.get_config()
            return isinstance(config, dict) and config.get("state") == HA_STATE_RUNNING
        except Exception:
            return False

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

    def create_persistent_notification(self, title: str, message: str, notification_id: str) -> None:
        """Legt eine Benachrichtigung in der HA-Oberflaeche an. Eine gleiche
        `notification_id` ersetzt die vorige, statt sie zu stapeln. Wirft wie
        send_notification; abfangen ist Sache des Aufrufers."""
        response = requests.post(
            f"{self._base_url}{self._api_prefix}/services/persistent_notification/create",
            headers=self._headers,
            json={"title": title, "message": message, "notification_id": notification_id},
            timeout=10,
        )
        response.raise_for_status()

    def dismiss_persistent_notification(self, notification_id: str) -> None:
        """Entfernt eine per create_persistent_notification angelegte Benachrichtigung. Wirft
        wie send_notification; abfangen ist Sache des Aufrufers."""
        response = requests.post(
            f"{self._base_url}{self._api_prefix}/services/persistent_notification/dismiss",
            headers=self._headers,
            json={"notification_id": notification_id},
            timeout=10,
        )
        response.raise_for_status()
