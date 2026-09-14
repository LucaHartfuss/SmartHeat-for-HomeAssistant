"""Flask-App fuer das Ingress-Panel: statische Wizard-Seite plus die JSON-Routen, die
sie gegen heizungsserver, ha_api und die Supervisor-Options-API aufruft. Jeder Schritt
scheitert laut auf Deutsch -- siehe Design-Spec, Abschnitt Fehlerbehandlung.
"""

import secrets

import requests
from flask import Flask, jsonify, request, session
from werkzeug.exceptions import HTTPException

from heizungsbruecke import profiles, supervisor_api


_ROLE_UNIT_EXPECTATIONS = {
    "entity_room_actual": "°C",
    "entity_room_target": "°C",
    "entity_outdoor_temp": "°C",
    "entity_offset_current": "°C",
    "entity_heat_limit": "°C",
}
_REQUIRED_ENTITY_ROLES = (*_ROLE_UNIT_EXPECTATIONS, "entity_curve_current")
# Roles allowed to reference a climate.* entity's flattened attribute (see wizard.js's
# CLIMATE_ATTRIBUTE_BY_ROLE) -- only entity_room_target since C2: entity_room_actual
# flows into derived_sensors.ensure_all()'s statistics config-flow, which rejects both
# climate-domain sources and the "::attribute" suffix.
_CLIMATE_ALLOWED_ROLES = {"entity_room_target"}


def create_app(
    heizungsserver_base_url: str,
    ha_api,
    supervisor_base_url: str,
    supervisor_token: str,
) -> Flask:
    app = Flask(__name__, static_folder="static", static_url_path="")
    app.secret_key = secrets.token_hex(32)
    # Flask's default cookie name ("session") at path "/" could collide with another
    # Ingress add-on's own Flask session cookie on the same shared HA origin.
    app.config["SESSION_COOKIE_NAME"] = "heizungsbruecke_session"
    app.config["HEIZUNGSSERVER_BASE_URL"] = heizungsserver_base_url
    app.config["HA_API"] = ha_api
    app.config["SUPERVISOR_BASE_URL"] = supervisor_base_url
    app.config["SUPERVISOR_TOKEN"] = supervisor_token

    @app.errorhandler(Exception)
    def _handle_unexpected_error(error):
        # Registering a handler for the bare Exception class also catches
        # HTTPException (Flask's _find_error_handler walks the MRO: NotFound ->
        # HTTPException -> Exception) -- without this check, every normal 404 (and
        # notably every browser's automatic GET /favicon.ico against the catch-all
        # static route, static_url_path="") would become a logged 500, training the
        # add-on's operator to ignore ERROR-level log lines. Let HTTPException through
        # to Flask's own default handling; only swallow genuine unexpected exceptions.
        if isinstance(error, HTTPException):
            return error
        # Backstop for anything the specific handlers below don't name explicitly --
        # nothing should ever hand the wizard a raw HTML traceback page.
        app.logger.exception("Unerwarteter Fehler in der Wizard-API")
        return jsonify(error="Unerwarteter Fehler"), 500

    @app.post("/api/login")
    def login():
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            body = {}
        email = body.get("email")
        password = body.get("password")
        if not email or not password:
            return jsonify(error="E-Mail und Passwort sind erforderlich"), 400

        try:
            response = requests.post(
                f"{app.config['HEIZUNGSSERVER_BASE_URL']}/auth/login",
                json={"email": email, "password": password},
                timeout=10,
            )
        except requests.RequestException:
            return jsonify(error="Abo-Service nicht erreichbar"), 502

        if response.status_code != 200:
            return jsonify(error="Ungueltige Zugangsdaten"), 401

        try:
            session["heizungsserver_token"] = response.json()["token"]
        except (ValueError, KeyError):
            return jsonify(error="Abo-Service nicht erreichbar"), 502
        return jsonify(ok=True), 200

    @app.get("/api/tenants")
    def tenants():
        token = session.get("heizungsserver_token")
        if not token:
            return jsonify(error="Nicht eingeloggt"), 401

        try:
            response = requests.get(
                f"{app.config['HEIZUNGSSERVER_BASE_URL']}/accounts/me/tenants",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
        except requests.RequestException:
            return jsonify(error="Abo-Service nicht erreichbar"), 502

        if response.status_code != 200:
            return jsonify(error="Sitzung abgelaufen, bitte erneut einloggen"), 401

        try:
            return jsonify(response.json()), 200
        except ValueError:
            return jsonify(
                error="Antwort des Abo-Service beim Abrufen der Anlagen war unvollstaendig"
            ), 502

    @app.get("/api/profiles")
    def profile_catalog():
        token = session.get("heizungsserver_token")
        if not token:
            return jsonify(error="Nicht eingeloggt"), 401

        return jsonify([
            {
                "hersteller": entry.hersteller,
                "erzeuger_typ": entry.erzeuger_typ,
                "verteilsystem": entry.verteilsystem,
                "profile_id": entry.profile_id,
                "verified": profiles.is_verified(entry.profile_id),
            }
            for entry in profiles.PROFILE_CATALOG
        ]), 200

    @app.get("/api/entities")
    def entities():
        token = session.get("heizungsserver_token")
        if not token:
            return jsonify(error="Nicht eingeloggt"), 401

        domain = request.args.get("domain")
        if not domain:
            return jsonify(error="domain-Parameter ist erforderlich"), 400

        try:
            states = app.config["HA_API"].list_states()
        except Exception:
            return jsonify(error="HA-Entities konnten nicht geladen werden"), 502

        matching = [
            {
                "entity_id": state["entity_id"],
                "friendly_name": state.get("attributes", {}).get("friendly_name", state["entity_id"]),
                "unit_of_measurement": state.get("attributes", {}).get("unit_of_measurement"),
            }
            for state in states
            if state["entity_id"].split(".", 1)[0] == domain
        ]
        return jsonify(matching), 200

    @app.post("/api/complete")
    def complete():
        token = session.get("heizungsserver_token")
        if not token:
            return jsonify(error="Nicht eingeloggt"), 401

        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            body = {}
        tenant_id = body.get("tenant_id")
        profile_id = body.get("profile_id")
        entities = body.get("entities", {})

        if not tenant_id or not profile_id:
            return jsonify(error="tenant_id und profile_id sind erforderlich"), 400

        if not isinstance(entities, dict):
            return jsonify(error="entities muss ein Objekt sein"), 400

        if not profiles.is_verified(profile_id):
            return jsonify(error=f"Profil '{profile_id}' hat noch keine verifizierten Standardwerte"), 400

        try:
            states = app.config["HA_API"].list_states()
        except Exception:
            return jsonify(error="HA-Entities konnten nicht geladen werden"), 502

        # Server-seitige Revalidierung (siehe Design-Spec, Abschnitt Fehlerbehandlung):
        # der Client darf die Einheit eines Entities nicht selbst behaupten duerfen,
        # sonst validiert der Server faktisch nur gegen die eigene Behauptung des
        # Clients. climate.*-Quellen melden hier typischerweise keine
        # unit_of_measurement (HA nutzt dafuer intern eine eigene temperature_unit,
        # die list_states() nicht ausliefert) -- daher der explizite Sonderfall unten,
        # und zwar nur fuer die eine Rolle, die climate.* ueberhaupt noch zulaesst
        # (entity_room_target, siehe C2/wizard.js's ROLE_DOMAINS).
        real_units = {
            state["entity_id"]: state.get("attributes", {}).get("unit_of_measurement")
            for state in states
        }

        for role in _REQUIRED_ENTITY_ROLES:
            entity = entities.get(role)
            if not entity or not entity.get("entity_id"):
                return jsonify(error=f"Feld '{role}' ist erforderlich"), 400

            base_entity_id = entity["entity_id"].split("::", 1)[0]
            if base_entity_id not in real_units:
                return jsonify(error=f"'{role}': Entity '{base_entity_id}' existiert nicht"), 400

            expected_unit = _ROLE_UNIT_EXPECTATIONS.get(role)
            if expected_unit is not None:
                is_climate_source = (
                    role in _CLIMATE_ALLOWED_ROLES and base_entity_id.split(".", 1)[0] == "climate"
                )
                actual_unit = "°C" if is_climate_source else real_units[base_entity_id]
                if actual_unit != expected_unit:
                    return jsonify(
                        error=f"'{role}': erwartete Einheit '{expected_unit}', gefunden "
                              f"'{actual_unit}'"
                    ), 400

        try:
            provision_response = requests.post(
                f"{app.config['HEIZUNGSSERVER_BASE_URL']}/tenants/{tenant_id}/provision",
                json={"profile_id": profile_id},
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
        except requests.RequestException:
            return jsonify(error="Abo-Service nicht erreichbar"), 502

        if provision_response.status_code != 200:
            return jsonify(error="Provisioning fehlgeschlagen"), provision_response.status_code

        try:
            provisioning = provision_response.json()
            mqtt_username = provisioning["username"]
            mqtt_password = provisioning["password"]
            mosquitto_passwd_command = provisioning["mosquitto_passwd_command"]
            acl_snippet = provisioning["acl_snippet"]
        except (ValueError, KeyError, TypeError):
            return jsonify(error="Antwort des Abo-Service beim Provisioning war unvollstaendig"), 502

        options = {
            "tenant_id": tenant_id,
            "profile": profile_id,
            # Only the validated required roles -- not entities.items() verbatim.
            # With schema: false, Supervisor accepts unvalidated extra keys; a stray
            # entities key (e.g. "poll_interval_seconds") would otherwise silently
            # corrupt an unrelated option's type on the next add-on start.
            **{role: entities[role]["entity_id"] for role in _REQUIRED_ENTITY_ROLES},
        }
        try:
            supervisor_api.set_own_options(
                app.config["SUPERVISOR_BASE_URL"], app.config["SUPERVISOR_TOKEN"], options,
            )
        except requests.RequestException:
            return jsonify(
                error="Speichern der Add-on-Optionen fehlgeschlagen",
                mqtt_username=mqtt_username,
                mqtt_password=mqtt_password,
                mosquitto_passwd_command=mosquitto_passwd_command,
                acl_snippet=acl_snippet,
            ), 502

        return jsonify(
            ok=True,
            message="Konfiguration gespeichert - Add-on bitte manuell neu starten",
            mqtt_username=mqtt_username,
            mqtt_password=mqtt_password,
            mosquitto_passwd_command=mosquitto_passwd_command,
            acl_snippet=acl_snippet,
        ), 200

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    return app
