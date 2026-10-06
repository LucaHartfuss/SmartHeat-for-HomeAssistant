"""Vertrag Geraet <-> Server (Spec SHG G3 Abschnitt 2, Plan G2a Praezisierungen 2, 5, 8). Gegenstueck auf dem Server:
heizungsserver/devices_wire.py (G3). tools/contract_check.py (Check 44) vergleicht beide mit
tools/contracts/shg_device_v1.json im Dev-Root. Aenderungen nur an allen drei Stellen zugleich."""
import hashlib
import sys
from dataclasses import dataclass

CONTRACT_VERSION = 1
SIGNATURE_SCHEME = "SHG1"
SIGNATURE_ENCODING = "base64url_unpadded"
PUBLIC_KEY_ENCODING = "base64_der_spki"
HEADER_DEVICE = "X-SHG-Device"
HEADER_TIMESTAMP = "X-SHG-Timestamp"
HEADER_SIGNATURE = "X-SHG-Signature"
MAX_CLOCK_SKEW_SECONDS = 300
MAX_BODY_BYTES = 65536
DEVICE_ID_PREFIX = "shg-"
DEVICE_ID_HASH_CHARS = 16
CLAIM_CODE_CHARS = 12
UNAUTHENTICATED_ERROR = "nicht authentifiziert"

ROUTES: dict[str, tuple[str, str]] = {
    "register": ("POST", "/devices/register"),
    "commands": ("GET", "/devices/{device_id}/commands"),
    "result": ("POST", "/devices/{device_id}/commands/{command_id}/result"),
    "status": ("POST", "/devices/{device_id}/status"),
    "notifications": ("POST", "/devices/{device_id}/notifications"),
    "desired": ("GET", "/devices/{device_id}/desired"),
    "update_result": ("POST", "/devices/{device_id}/update_result"),
}
REQUEST_FIELDS: dict[str, tuple[str, ...]] = {
    "register": ("device_id", "public_key", "enc_public_key", "claim_code_hash", "version", "capabilities"),
    "result": ("ok", "result", "error"),
    "status": ("agent_state", "runtime_status", "raum", "version", "capabilities", "ts"),
    "notifications": ("items",),
    "notification_item": ("key", "kategorie", "kritisch", "text", "offen", "ts"),
    "update_result": ("version", "result", "reason"),
}
RESPONSE_FIELDS: dict[str, tuple[str, ...]] = {
    "register": ("device_state", "poll_after"),
    "commands": ("device_state", "poll_after", "commands"),
    "command": ("command_id", "kind", "payload", "expires_at"),
    "desired": ("version", "manifest_url", "manifest_sha256", "signature"),
}
ERROR_FIELDS = ("grund", "text")
CAPABILITY_FIELDS = ("drivers", "zigbee")
RAUM_FIELDS = ("ist", "soll", "soll_quelle", "ts")
DEVICE_STATES = ("nicht_uebernommen", "uebernommen", "gesperrt")
AGENT_STATES = ("startet", "nicht_uebernommen", "keine_verbindung", "wartet_auf_einrichtung", "regelt", "stoerung")
ROOM_TARGET_MIN, ROOM_TARGET_MAX, ROOM_TARGET_STEP = 15.0, 25.0, 0.5
ZIGBEE_DEVICE_FIELDS = ("ieee", "model", "vendor", "art", "werte", "batterie", "faehigkeiten", "last_seen")
ZIGBEE_ARTEN = ("fuehler", "thermostat", "sonstig")
PROBE_FIELDS = ("driver_id", "kandidaten")
PROBE_CANDIDATE_FIELDS = (
    "kandidat_id", "anzeige", "parameter", "erzeuger_typ", "lever_set", "ablehnung", "hebel", "signale",
    "sicherheitswarnungen", "details",
)
PROBE_LEVER_FIELDS = ("wert", "min", "max", "schritt")
COMMON_ERRORS = ("ungueltige_nutzlast", "intern")
_DRIVER_ERRORS = ("treiber_unbekannt", "nicht_angemeldet", "kontingent_erschoepft", "anlage_nicht_erreichbar")


@dataclass(frozen=True)
class Command:
    payload: tuple[str, ...]
    result: tuple[str, ...]
    expires_seconds: int | None  # None = Regel in expires_rule
    errors: tuple[str, ...] = ()
    expires_rule: str | None = None


COMMANDS: dict[str, Command] = {
    "zigbee_permit_join": Command(("seconds",), (), 60, ("zigbee_nicht_bereit",)),
    "zigbee_devices": Command((), ("devices",), 60, ("zigbee_nicht_bereit",)),
    "driver_login": Command(
        ("phase", "driver_id", "client_id", "redirect_uri", "scope", "code", "ciphertext"),
        ("code_challenge", "code_challenge_method"), 120,
        ("treiber_unbekannt", "login_nicht_noetig", "login_fehlgeschlagen", "anlage_nicht_erreichbar"),
    ),
    "driver_probe": Command(("driver_id",), PROBE_FIELDS, 120, _DRIVER_ERRORS),
    "driver_inventory": Command(
        ("driver_id", "stunden"), (), None, (*_DRIVER_ERRORS, "keine_bestaetigung"), "stunden*3600+3600",
    ),
    "create_csr": Command(("tenant_id",), ("csr",), 120),
    "apply_config": Command(
        ("setup_id", "config"), ("setup_id",), 300, ("konfiguration_ungueltig", "keine_bestaetigung"),
    ),
    "set_room_target": Command(
        ("value",), ("value",), 120, ("ausserhalb_bereich", "nicht_eingerichtet", "keine_bestaetigung"),
    ),
    "sign_off": Command((), ("zurueckgesetzt", "werte"), 24 * 3600, ("nicht_eingerichtet", "keine_bestaetigung")),
    "new_claim_code": Command((), (), 300, ("bereits_uebernommen",)),
    "diagnostics": Command((), (), 300),
}
DRIVER_LOGIN_PHASES: dict[str, tuple[str, ...]] = {
    "begin": ("phase", "driver_id", "client_id", "redirect_uri", "scope"),
    "finish": ("phase", "driver_id", "code", "redirect_uri"),
    "password": ("phase", "driver_id", "ciphertext"),
}
APPLY_CONFIG_VERSION = 1
APPLY_CONFIG_REQUIRED = (
    "config_version", "tenant_id", "setup_id", "lever_set", "verteilsystem", "transport", "accounts_api_base_url",
    "daily_trigger_time", "driver", "room_sensors", "room_target_start", "abgemeldet",
)
APPLY_CONFIG_OPTIONAL = (
    "mqtt_username", "mqtt_password", "tls_certificate", "installation_token",
    "local_check_interval_seconds", "telemetry_interval_seconds", "poll_interval_seconds", "notify_hints_off",
    "thermostat", "cloudflared",
)
APPLY_CONFIG_SECRETS = ("mqtt_password", "installation_token", "cloudflared.service_token_secret")
APPLY_CONFIG_DRIVER_FIELDS = ("id", "parameter")
APPLY_CONFIG_CLOUDFLARED_FIELDS = ("hostname", "service_token_id", "service_token_secret")


def canonical_string(method: str, path: str, timestamp: int, body: bytes) -> bytes:
    """Signierte Zeichenkette (G3 2.1); `path` inklusive Query."""
    return f"{SIGNATURE_SCHEME}\n{method}\n{path}\n{timestamp}\n{hashlib.sha256(body).hexdigest()}".encode()


def claim_code_hash(device_id: str, code: str) -> str:
    return hashlib.sha256(f"{device_id}:{code}".encode()).hexdigest()


def command_errors(kind: str) -> tuple[str, ...]:
    return COMMANDS[kind].errors + COMMON_ERRORS


def contract_from(ns) -> dict:
    """Vertrag als JSON-faehiges Objekt aus einem Namensraum (Modul oder Kopie mit gleichen Attributen)."""
    def lists(value):
        return [lists(item) for item in value] if isinstance(value, (tuple, list)) else value

    return {
        "version": ns.CONTRACT_VERSION,
        "signature": {
            "scheme": ns.SIGNATURE_SCHEME, "encoding": ns.SIGNATURE_ENCODING,
            "public_key_encoding": ns.PUBLIC_KEY_ENCODING,
            "headers": [ns.HEADER_DEVICE, ns.HEADER_TIMESTAMP, ns.HEADER_SIGNATURE],
            "max_clock_skew_seconds": ns.MAX_CLOCK_SKEW_SECONDS, "max_body_bytes": ns.MAX_BODY_BYTES,
            "unauthenticated_error": ns.UNAUTHENTICATED_ERROR,
        },
        "device_id": {"prefix": ns.DEVICE_ID_PREFIX, "hash_chars": ns.DEVICE_ID_HASH_CHARS},
        "claim_code_chars": ns.CLAIM_CODE_CHARS,
        "routes": {name: lists(route) for name, route in ns.ROUTES.items()},
        "request_fields": {name: lists(fields) for name, fields in ns.REQUEST_FIELDS.items()},
        "response_fields": {name: lists(fields) for name, fields in ns.RESPONSE_FIELDS.items()},
        "error_fields": lists(ns.ERROR_FIELDS),
        "capability_fields": lists(ns.CAPABILITY_FIELDS),
        "raum_fields": lists(ns.RAUM_FIELDS),
        "device_states": lists(ns.DEVICE_STATES),
        "agent_states": lists(ns.AGENT_STATES),
        "room_target": {"min": ns.ROOM_TARGET_MIN, "max": ns.ROOM_TARGET_MAX, "step": ns.ROOM_TARGET_STEP},
        "zigbee_device_fields": lists(ns.ZIGBEE_DEVICE_FIELDS),
        "zigbee_arten": lists(ns.ZIGBEE_ARTEN),
        "probe": {
            "fields": lists(ns.PROBE_FIELDS), "candidate_fields": lists(ns.PROBE_CANDIDATE_FIELDS),
            "lever_fields": lists(ns.PROBE_LEVER_FIELDS),
        },
        "common_errors": lists(ns.COMMON_ERRORS),
        "commands": {
            kind: {
                "payload": lists(command.payload), "result": lists(command.result),
                "expires_seconds": command.expires_seconds, "expires_rule": command.expires_rule,
                "errors": lists(command.errors),
            }
            for kind, command in ns.COMMANDS.items()
        },
        "driver_login_phases": {phase: lists(fields) for phase, fields in ns.DRIVER_LOGIN_PHASES.items()},
        "apply_config": {
            "config_version": ns.APPLY_CONFIG_VERSION, "required": lists(ns.APPLY_CONFIG_REQUIRED),
            "optional": lists(ns.APPLY_CONFIG_OPTIONAL), "secrets": lists(ns.APPLY_CONFIG_SECRETS),
            "driver_fields": lists(ns.APPLY_CONFIG_DRIVER_FIELDS),
            "cloudflared_fields": lists(ns.APPLY_CONFIG_CLOUDFLARED_FIELDS),
        },
    }


def contract() -> dict:
    return contract_from(sys.modules[__name__])
