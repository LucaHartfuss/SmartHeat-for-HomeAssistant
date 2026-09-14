"""Flask-App fuer das Ingress-Panel: statische Wizard-Seite plus die JSON-Routen, die
sie gegen heizungsserver, ha_api und die Supervisor-Options-API aufruft. Jeder Schritt
scheitert laut auf Deutsch -- siehe Design-Spec, Abschnitt Fehlerbehandlung.
"""

import secrets

import requests
from flask import Flask, jsonify, request, session


def create_app(
    heizungsserver_base_url: str,
    ha_api,
    supervisor_base_url: str,
    supervisor_token: str,
) -> Flask:
    app = Flask(__name__, static_folder="static", static_url_path="")
    app.secret_key = secrets.token_hex(32)
    app.config["HEIZUNGSSERVER_BASE_URL"] = heizungsserver_base_url
    app.config["HA_API"] = ha_api
    app.config["SUPERVISOR_BASE_URL"] = supervisor_base_url
    app.config["SUPERVISOR_TOKEN"] = supervisor_token

    @app.post("/api/login")
    def login():
        body = request.get_json(silent=True) or {}
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

        session["heizungsserver_token"] = response.json()["token"]
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

        return jsonify(response.json()), 200

    return app
