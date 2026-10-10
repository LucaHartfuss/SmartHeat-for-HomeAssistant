"""Vertrag Geraet <-> Server (Spec 5b Abschnitte 1-3): HTTPS-Bootstrap und MQTT. Woertliche Kopie des Vertragsteils von
heizungsserver/device_protocol.py (alles ab `SCHEMA = 1` bis einschliesslich `command_errors`; die Importe stehen je
Seite selbst); tools/contract_check.py im Dev-Root vergleicht beide mit tools/contracts/geraet_v1.json (Plan 5b-W).
Aenderungen nur an allen drei Stellen zugleich. Die Server-Regeln unter dem Vertragsteil (Aushandeln, Ablauf,
Mindestversion je Host) gibt es nur auf dem Server.

Versionsregeln (Spec 2.1): beide Seiten ignorieren unbekannte Felder und Topics; neue Felder sind optional und haben
eine festgelegte Bedeutung bei Fehlen; eine neue Schema-Nummer gibt es nur fuer einen Bruch. Felder, deren Fehlen
etwas bedeutet, stehen in *_OPTIONAL mit der Bedeutung im Kommentar."""
import hashlib
import re
from dataclasses import dataclass

SCHEMA = 1
SCHEMATA = (1,)

# --- HTTPS-Bootstrap (Spec 1), signiert wie der Vertrag G3 ---
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
CSR_MAX_BYTES = 8192
UNAUTHENTICATED_ERROR = "nicht authentifiziert"
ROUTES: dict[str, tuple[str, str]] = {
    "register": ("POST", "/devices/register"),
    "certificate": ("POST", "/devices/{device_id}/certificate"),
}
HOSTS = ("gateway", "ha")
REGISTER_FIELDS = (
    "device_id", "public_key", "enc_public_key", "claim_code_hash", "version", "host", "capabilities", "csr",
)
REGISTER_OPTIONAL = ("werks_nachweis",)  # fehlt = kein Nachweis (Pruefung: eigene Spec vor der Freischaltung)
CERTIFICATE_FIELDS = ("csr",)
CAPABILITY_FIELDS = ("drivers", "zigbee")
CAPABILITY_OPTIONAL = ("updater", "programm")  # fehlt = false
BOOTSTRAP_RESPONSE = ("device_state", "certificate", "mqtt_endpoint", "mqtt_port", "mqtt_ca")
BOOTSTRAP_OPTIONAL = ("mqtt_alpn",)  # fehlt = kein Rueckfall auf Port 443 (AN-3)
DEVICE_STATES = ("nicht_uebernommen", "uebernommen", "gesperrt")

# --- MQTT (Spec 2) ---
TOPIC_PREFIX = "smartheat"
QOS = 1
MAX_MESSAGE_BYTES = 120 * 1024  # AN-10: 120 KB zugestellt, 130 KB abgelehnt
SESSION_EXPIRY_SECONDS = 3600  # AN-9: Hoechstwert von IoT Core
DEVICE_SUBSCRIPTION = "down/#"  # AN-6: die Policy erlaubt nur diesen Filter
UP_TOPICS = (
    "up/hello", "up/snapshot", "up/status", "up/notifications", "up/inventory", "up/result", "up/wish", "telemetry",
)
DOWN_TOPICS = ("down/config", "down/operation", "down/command", "down/setpoints")

HELLO_FIELDS = (
    "schema", "schemata", "konfiguration_version", "bedienung_version", "software_version", "host", "faehigkeiten",
    "boot_id", "uhr_synchron",
)
STATUS_FIELDS = ("schema", "zustand", "runtime_status", "raum", "ts")
ZUSTAENDE = (
    "startet", "nicht_uebernommen", "keine_verbindung", "wartet_auf_einrichtung", "regelt", "stoerung", "gesperrt",
    "update_noetig",
)
RAUM_FIELDS = ("ist", "soll", "soll_quelle", "ts")
NOTIFICATIONS_FIELDS = ("schema", "items")
NOTIFICATION_FIELDS = ("key", "kategorie", "kritisch", "text", "offen", "ts")
INVENTORY_FIELDS = ("schema", "command_id", "teil", "teile", "daten")
RESULT_FIELDS = ("schema", "ok", "result", "error")
RESULT_TARGETS = ("command_id", "dokument", "version")  # entweder command_id oder dokument und version
ERROR_FIELDS = ("grund", "text")
WISH_FIELDS = ("schema", "wish_id", "basis_version", "ts", "uhr_synchron", "werte")
WISH_OPTIONAL = ("roh", "herkunft", "durch")  # fehlt = nicht begrenzt bzw. unbekannt
WISH_WERTE = ("raum_soll",)
HERKUNFT = ("thermostat", "soll_entity", "smartheat_entity")
DURCH = ("nutzer", "automation")
SNAPSHOT_EXTRA = ("raum_soll_wirksam",)  # zusaetzlich zum Tick Schema 4 (Server generic/messages.py)
TELEMETRY_EXTRA = ("nachgeliefert",)  # fehlt = live gemessen (A4-21)

DOKUMENTE = ("konfiguration", "bedienung")
DOKUMENT_SOFTWARE = "software"  # nur in up/result: Ergebnis eines Updates, version = Software-Version (Text)
DOKUMENT_TOPICS = {"konfiguration": "down/config", "bedienung": "down/operation"}
KONFIGURATION_FIELDS = (
    "schema", "version", "anlage", "profil", "hebelsatz", "verteilsystem", "bindung", "raumfuehler", "tagestick", "abo",
    "mindest_software", "software",
)
KONFIGURATION_OPTIONAL = ("intervalle",)  # fehlt = Standardwerte des Geraets
BINDUNG_GATEWAY_FIELDS = ("treiber", "thermostat")
TREIBER_FIELDS = ("id", "parameter")
SOFTWARE_FIELDS = ("version", "manifest_url", "manifest_sha256", "signature")
INTERVALLE_FIELDS = ("pruefung_s", "telemetrie_s")
ABO = ("aktiv", "inaktiv")
BEDIENUNG_FIELDS = ("schema", "version", "modus", "raum_soll", "quelle", "ts")
MODI = ("manuell",)
QUELLEN = ("portal", "geraet", "auftrag")
RAUM_SOLL_MIN, RAUM_SOLL_MAX, RAUM_SOLL_STEP = 15.0, 25.0, 0.5
DOKUMENT_ERRORS = (
    "schema_unbekannt", "ungueltig", "hebelsatz_eingerastet", "sicherheitswerte_fehlen", "rolle_unzulaessig",
    "doppelt_belegt", "treiber_parameter", "update_noetig", "ausserhalb_bereich",
)
SOFTWARE_ERRORS = (
    "manifest_ungueltig", "signatur_ungueltig", "updater_zu_alt", "version_zu_alt", "start_fehlgeschlagen",
    "ungesund", "server_unerreichbar",
)
HINWEIS_STUFEN = ("info", "warnung", "kritisch")
MELDUNGEN = ("wunsch_begrenzt", "soll_quelle_aus", "geraet_doppelt", "update_noetig", "gesperrt")

COMMAND_FIELDS = ("schema", "command_id", "kind", "payload", "expires_at")
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
    "inventory": Command(("stunden",), (), None, (*_DRIVER_ERRORS, "nicht_eingerichtet", "keine_bestaetigung"),
                         "stunden*3600+3600"),
    "sign_off": Command((), ("zurueckgesetzt", "werte"), 24 * 3600, ("nicht_eingerichtet", "keine_bestaetigung")),
    "new_claim_code": Command((), (), 300, ("bereits_uebernommen",)),
    "diagnostics": Command((), (), 300),
    "hinweis": Command(("key", "stufe", "text"), (), 24 * 3600),
}
DRIVER_LOGIN_PHASES: dict[str, tuple[str, ...]] = {
    "begin": ("phase", "driver_id", "client_id", "redirect_uri", "scope"),
    "finish": ("phase", "driver_id", "code", "redirect_uri"),
    "password": ("phase", "driver_id", "ciphertext"),
}
_SEGMENT_RE = re.compile(r"[A-Za-z0-9_-]+")


def canonical_string(method: str, path: str, timestamp: int, body: bytes) -> bytes:
    """Signierte Zeichenkette des Bootstraps (wie G3 2.1); `path` inklusive Query."""
    return f"{SIGNATURE_SCHEME}\n{method}\n{path}\n{timestamp}\n{hashlib.sha256(body).hexdigest()}".encode()


def claim_code_hash(device_id: str, code: str) -> str:
    return hashlib.sha256(f"{device_id}:{code}".encode()).hexdigest()


def topic(segment: str, name: str) -> str:
    """smartheat/<geraete_id>/<name>; das Segment darf keine Platzhalter und keinen Schraegstrich enthalten."""
    if not _SEGMENT_RE.fullmatch(segment or ""):
        raise ValueError("ungueltiges Topic-Segment")
    return f"{TOPIC_PREFIX}/{segment}/{name}"


def parse_topic(value: str) -> tuple[str, str] | None:
    """(Segment, Name) unter smartheat/; None fuer fremde Formen. Unbekannte Namen ignoriert der Aufrufer."""
    prefix, _, rest = value.partition("/")
    segment, _, name = rest.partition("/")
    if prefix != TOPIC_PREFIX or not _SEGMENT_RE.fullmatch(segment) or not name:
        return None
    return segment, name


def command_errors(kind: str) -> tuple[str, ...]:
    return COMMANDS[kind].errors + COMMON_ERRORS
