import requests


class HomeAssistantApi:
    def __init__(self, base_url: str, token: str, api_prefix: str = "/core/api"):
        self._base_url = base_url
        self._api_prefix = api_prefix
        self._headers = {"Authorization": f"Bearer {token}"}

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
        """Legt einen input_number-Helper an -- NACH VERIFIKATION GEGEN ECHTE HA NICHT FUNKTIONSFAEHIG.

        Diese Methode entspricht dem im Task-Brief vorgesehenen Entwurf, wurde
        aber gegen einen echten HA-Core-Container (2026.9.2, siehe
        tests/test_ha_api_real_ha_integration.py) verifiziert und schlaegt dort
        mit 404 fehl: `POST .../config/input_number/config/<object_id>` existiert
        in dieser HA-Version nicht mehr als REST-Route. Quellcode-Pruefung im
        Container bestaetigt: `homeassistant/components/config/__init__.py`
        registriert keine input_number-Sektion mehr (frueher
        `config/input_number.py`, mittlerweile entfernt), und
        `homeassistant/components/input_number/__init__.py` bietet Helfer-Erzeugung
        ausschliesslich ueber `DictStorageCollectionWebsocket`, also per
        Websocket-Kommando (`input_number/create`), nicht per REST.

        Ein Wechsel auf einen Websocket-Client ist eine groessere Architektur-
        entscheidung (eigene Brainstorming-Runde noetig, siehe Design-Dokument
        Abschnitt 3.4) und wurde hier bewusst NICHT vorgenommen. Diese Methode
        bleibt als dokumentierter Platzhalter stehen, bis diese Entscheidung
        getroffen ist; siehe task-2-report.md fuer Details.
        """
        response = requests.post(
            f"{self._base_url}{self._api_prefix}/config/input_number/config/{object_id}",
            headers=self._headers,
            json={"name": name, "min": minimum, "max": maximum, "step": step, "initial": initial},
            timeout=10,
        )
        response.raise_for_status()
        return f"input_number.{object_id}"

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
           (_find_entity_by_config_entry(), `GET .../config/entity_registry/
           list`) liefert dagegen einen 404 und ist NICHT reparierbar: Quellcode-
           Pruefung im Container (homeassistant/components/config/
           entity_registry.py) zeigt, dass diese Datei ausschliesslich
           `websocket_api.async_register_command(...)` registriert -- keine
           einzige `HomeAssistantView`-Klasse, also ueberhaupt keine REST-Route
           fuer die Entity-Registry in dieser HA-Version. Das ist ein zu
           create_input_number() analoger, aber eigenstaendiger architektonischer
           Befund (andere API-Flaeche: Entity-Registry statt Helper-Storage) --
           siehe task-3-report.md. Diese Methode bleibt deshalb, wie
           create_input_number(), ein dokumentierter, verifiziert an dieser
           letzten Stelle nicht funktionsfaehiger Aufruf: der Config-Entry-Flow
           laeuft durch und legt einen echten, geladenen Config-Entry samt
           Entity in HA an (verifiziert per `GET .../config/config_entries/
           entry?domain=statistics`), aber die Rueckgabe der resultierenden
           Entity-ID scheitert per REST.
        """
        fields = {
            "name": name,
            "entity_id": source_entity_id,
            "state_characteristic": "mean",
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
        """Loest eine Config-Entry-ID zu ihrer Entity-ID auf -- NACH VERIFIKATION NICHT FUNKTIONSFAEHIG.

        Siehe create_statistics_sensor()-Docstring, Punkt 2: `GET .../config/
        entity_registry/list` liefert gegen einen echten HA-Core-Container
        (2026.9.2) einen 404. Quellcode-Pruefung bestaetigt: keine REST-Route
        fuer die Entity-Registry in dieser Version, nur Websocket-Kommandos.
        Ein Wechsel auf einen Websocket-Client (oder eine heuristische
        REST-Ersatzloesung, z.B. per States-Diff) ist eine groessere
        Architekturentscheidung und wurde hier bewusst NICHT vorgenommen --
        siehe task-3-report.md.
        """
        response = requests.get(
            f"{self._base_url}{self._api_prefix}/config/entity_registry/list",
            headers=self._headers,
            timeout=10,
        )
        response.raise_for_status()
        for entry in response.json():
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
