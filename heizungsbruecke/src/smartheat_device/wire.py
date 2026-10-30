"""Vertrag Geraet <-> Server: HTTPS-Bootstrap und MQTT. Woertliche Kopie des Vertragsteils von
heizungsserver/geraete/device_protocol.py (alles ab `SCHEMA = 1` bis einschliesslich `command_errors`; die Importe stehen je
Seite selbst); tools/contract_check.py im Dev-Root vergleicht beide mit tools/contracts/geraet_v1.json.
Aenderungen nur an allen drei Stellen zugleich. Die Server-Regeln unter dem Vertragsteil (Aushandeln, Ablauf,
Mindestversion je Host) gibt es nur auf dem Server; unter der Marke stehen hier statt dessen die Konstanten, die nur
das Geraet braucht (Nachweis der Einrichtung).

Versionsregeln: V1 wird auf beiden Seiten strikt geprueft; ein fehlendes oder
fremdes Feld und ein falscher Typ werden abgelehnt. Ein neues Feld Server -> Geraet gibt es deshalb erst nach einem
Geraete-Update, ein neues Feld Geraet -> Server zuerst auf dem Server (als *_OPTIONAL mit der Bedeutung bei Fehlen im
Kommentar). Ausnahme ist das Tick-Drahtformat (Snapshot- und Telemetrie-Leser des Servers, Antwort down/setpoints im
Geraet): es ignoriert unbekannte Felder. Unbekannte Topics ignorieren beide Seiten. Eine neue Schema-Nummer gibt es nur
fuer einen Bruch."""
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
    "device_id", "public_key", "claim_code_hash", "version", "host", "capabilities", "csr",
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
HELLO_OPTIONAL = ("device_state",)  # fehlt = Uebernahmestand des Geraets unbekannt, kein Abgleich (Spec 5c F1)
STATUS_FIELDS = ("schema", "zustand", "runtime_status", "raum", "ts")
STATUS_OPTIONAL = ("ursprungswerte",)  # fehlt = keine neuen Ursprungswerte (G-4: Hebel -> Wert vor dem ersten Schreiben)
ZUSTAENDE = (
    "startet", "nicht_uebernommen", "keine_verbindung", "wartet_auf_einrichtung", "regelt", "stoerung", "gesperrt",
    "update_noetig",
)
RAUM_FIELDS = ("ist", "soll", "soll_quelle", "ts")
NOTIFICATIONS_FIELDS = ("schema", "items")
NOTIFICATION_FIELDS = ("key", "kategorie", "kritisch", "text", "offen", "ts")
INVENTORY_FIELDS = ("schema", "command_id", "teil", "teile", "daten")
INVENTUR_FILTER_FIELDS = ("plattformen", "domaenen", "geraeteklassen", "dienste")
INVENTUR_HA_FIELDS = ("entities", "geraete", "dienste", "einheit_temperatur", "zeitzone")
INVENTUR_ENTITY_FIELDS = (
    "entity_id", "plattform", "unique_id", "name", "config_entry", "geraet", "einheit", "device_class", "state_class",
    "zustand", "attribute", "aktualisiert",
)
INVENTUR_ATTRIBUTE = (
    "temperature", "current_temperature", "temperature_unit", "min", "max", "step", "min_temp", "max_temp",
    "target_temp_step", "options", "hvac_modes",
)
INVENTUR_GERAET_FIELDS = ("id", "name", "modell", "config_entries")
# Gateway-Inventur `stunden: 0` (Spec Stufe 3 S5 5.1): rohe Features je Anlage, Features auf oberster Ebene (Stueckelung)
INVENTUR_FILTER_GATEWAY_FIELDS = ("treiber",)
INVENTUR_GATEWAY_FIELDS = ("treiber", "anlagen", "features")
INVENTUR_ANLAGE_FIELDS = ("kennung", "sicherheitswarnungen")
INVENTUR_FEATURE_FIELDS = ("anlage", "feature", "properties", "commands")
RESULT_FIELDS = ("schema", "ok", "result", "error")
RESULT_TARGETS = ("command_id", "dokument", "version")  # entweder command_id oder dokument und version
#: Pflicht je Ziel (Testoffensive T2): ein Befehls-Ergebnis traegt alle RESULT_FIELDS (result und error null je nach
#: ok); ein Dokument- oder Software-Ergebnis traegt nie result, error nur bei ok false.
RESULT_PFLICHT_DOKUMENT = ("schema", "ok")
RESULT_OPTIONAL_DOKUMENT = ("error",)
ERROR_FIELDS = ("grund", "details")
WISH_FIELDS = ("schema", "wish_id", "basis_version", "ts", "uhr_synchron", "werte")
WISH_OPTIONAL = ("roh", "herkunft", "durch")  # fehlt = nicht begrenzt bzw. unbekannt
WISH_WERTE = ("raum_soll",)
HERKUNFT = ("thermostat", "soll_entity", "smartheat_entity")
DURCH = ("nutzer", "automation")

DOKUMENTE = ("konfiguration", "bedienung")
DOKUMENT_SOFTWARE = "software"  # nur in up/result: Ergebnis eines Updates, version = Software-Version (Text)
DOKUMENT_TOPICS = {"konfiguration": "down/config", "bedienung": "down/operation"}
KONFIGURATION_FIELDS = (
    "schema", "version", "anlage", "profil", "hebelsatz", "verteilsystem", "bindung", "raumfuehler", "tagestick", "abo",
    "mindest_software", "software", "bindungsparameter",
)
KONFIGURATION_OPTIONAL = ("ursprungswerte",)  # fehlt = keine Ursprungswerte auf dem Server
# Spec Stufe 3 S4b 4.1: Herstellerwissen der Bindung (G-7). Pflicht fuer eine eingerichtete Konfiguration, null in einer
# leeren -- bewusste Ausnahme von der Versionsregel ohne neue Schema-Nummer: Geraete bis 1.0.0 ignorieren das Feld.
BINDUNGSPARAMETER_FIELDS = (
    "schritte", "toleranzen", "wartezeit_s", "schreibgruppen", "zurueck", "zurueck_optional", "nur_lesen", "zaehler",
    "schreibschutz", "schemata", "energie",
)
SCHREIBSCHUTZ_FIELDS = ("tag", "lebensdauer_hinweis", "durchsetzen_tag")  # null = kein Tagesbudget bzw. kein Hinweis
ZAEHLER = ("total", "daily")  # Zaehlerart der Energiekanaele: durchlaufende Summe oder Tageszaehler
ENERGIE_EINHEITEN = ("Wh", "kWh", "m³")  # erwartete Einheit je Energiekanal (Einrichtung); das Geraet sendet kWh bzw. m³
BINDUNG_GATEWAY_FIELDS = ("treiber", "thermostat")
BINDUNG_HA_FIELDS = ("kreis", "rollen", "batterien", "benachrichtigung", "hinweise_aus")
BINDUNG_HA_OPTIONAL = ("abfrage_s", "einheiten")  # fehlt = Standardwert des Hebelsatzes bzw. keine Energiekanaele
KREIS_FIELDS = ("plattform", "anlage", "nummer")
KREIS_OPTIONAL = ("config_entry",)  # fehlt = Kreis ohne Config-Entry (dann kein Schreibziel, ha_pruefung)
HA_ROLLEN = (
    "room_target", "curve", "room_setpoint", "level", "heat_limit", "min_flow", "mode_select", "setpoint_comfort",
    "setpoint_setback", "outdoor_temp", "flow_temperature", "flow_setpoint", "return_temperature", "operating_mode",
    "system_water_pressure", "efficiency_ratio", "generator_hours", "generator_starts", "generator_state",
    "energy_electrical_heating", "energy_electrical_dhw", "energy_primary_heating", "energy_primary_dhw",
    "energy_thermal_heating", "energy_thermal_dhw", "energy_electrical_total",
)
HA_HINWEISE = ("raumfuehler", "batterie", "manueller_eingriff", "quellwechsel", "schreibbudget", "schreibzaehler")
TREIBER_FIELDS = ("id", "parameter")
SOFTWARE_FIELDS = ("version", "manifest_url", "manifest_sha256", "signature")
ABO = ("aktiv", "inaktiv")
BEDIENUNG_FIELDS = ("schema", "version", "modus", "raum_soll", "quelle", "ts")
MODI = ("manuell",)
QUELLEN = ("portal", "geraet", "auftrag")
RAUM_SOLL_MIN, RAUM_SOLL_MAX, RAUM_SOLL_STEP = 15.0, 25.0, 0.5
URSPRUNG_MAX_HEBEL, URSPRUNG_HEBEL_MAX_CHARS = 16, 64  # Ursprungswerte: hoechstens so viele Hebel, Namen so lang
URSPRUNG_WERT_MAX = 1e12  # groesster Betrag einer Zahl in ursprungswerte
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
COMMON_ERRORS = ("ungueltige_nutzlast", "intern", "nicht_unterstuetzt")
_DRIVER_ERRORS = ("treiber_unbekannt", "nicht_angemeldet", "kontingent_erschoepft", "anlage_nicht_erreichbar")


@dataclass(frozen=True)
class Command:
    payload: tuple[str, ...]
    result: tuple[str, ...]
    expires_seconds: int | None  # None = Regel in expires_rule
    errors: tuple[str, ...] = ()
    expires_rule: str | None = None
    payload_optional: tuple[str, ...] = ()  # fehlt in der Nutzlast = Bedeutung je Befehl (inventory: ohne Filter)


COMMANDS: dict[str, Command] = {
    "zigbee_permit_join": Command(("seconds",), (), 60, ("zigbee_nicht_bereit",)),
    "zigbee_devices": Command((), ("devices",), 60, ("zigbee_nicht_bereit",)),
    "driver_login": Command(
        ("phase", "driver_id", "client_id", "redirect_uri", "scope", "code", "ciphertext"),
        ("code_challenge", "code_challenge_method"), 120,
        ("treiber_unbekannt", "login_nicht_noetig", "login_fehlgeschlagen", "anlage_nicht_erreichbar"),
    ),
    "inventory": Command(("stunden",), (), None,
                         (*_DRIVER_ERRORS, "nicht_eingerichtet", "keine_bestaetigung", "ha_nicht_erreichbar"),
                         "stunden*3600+3600", ("filter",)),
    "sign_off": Command((), ("zurueckgesetzt", "werte"), 24 * 3600, ("nicht_eingerichtet", "keine_bestaetigung")),
    "new_claim_code": Command((), (), 300, ("bereits_uebernommen",)),
    "claim_state": Command(("device_state",), (), 24 * 3600),
    "diagnostics": Command((), (), 300),
    "hinweis": Command(("key", "stufe", "text"), (), 24 * 3600),
}
DRIVER_LOGIN_PHASES: dict[str, tuple[str, ...]] = {
    "begin": ("phase", "driver_id", "client_id", "redirect_uri", "scope"),
    "finish": ("phase", "driver_id", "code", "redirect_uri"),
    "password": ("phase", "driver_id", "ciphertext"),
}
# Spec Stufe 3 S5 6.1 (G-6): Fehler tragen Code und details, keine Texte. details nur mit diesen Schluesseln: `feld`
# aus PRUEF_FELDER, `regel` aus PRUEF_REGELN, die uebrigen endliche Zahlen; freie Texte gibt es darin nicht.
DETAIL_FIELDS = ("feld", "regel", "min", "max", "wert", "anzahl")
PRUEF_REGELN = ("fehlt", "ungueltig", "anzahl", "bereich", "format", "doppelt", "unbekannt", "nicht_erlaubt")
#: Pruefbare Felder: die Namen aus den Feldlisten dieses Vertrags (abgeleitet: ein neues Feld ist ohne zweite Liste
#: pruefbar).
PRUEF_FELDER = tuple(dict.fromkeys((
    *KONFIGURATION_FIELDS, *KONFIGURATION_OPTIONAL, *BINDUNGSPARAMETER_FIELDS, *SCHREIBSCHUTZ_FIELDS,
    *BINDUNG_GATEWAY_FIELDS, *TREIBER_FIELDS, *BINDUNG_HA_FIELDS, *BINDUNG_HA_OPTIONAL, *KREIS_FIELDS, *KREIS_OPTIONAL,
    *HA_ROLLEN,
    *BEDIENUNG_FIELDS, *COMMAND_FIELDS, *INVENTUR_FILTER_FIELDS, *INVENTUR_FILTER_GATEWAY_FIELDS,
    *(feld for command in COMMANDS.values() for feld in (*command.payload, *command.payload_optional)),
)))
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


# --- Server-Regeln (nicht im Vertrag) ---
# Nur im Geraet (der Server hat die Gegenstuecke in einrichtung/geraetecode.py): der Nachweis fuer den Geraetecode der
# Einrichtung. Das Geraet signiert mit canonical_string nur diese festen Werte; ein anderer Pfad oder Koerper steht
# nirgends zur Wahl.
NACHWEIS_METHOD = "POST"
NACHWEIS_PATH = "/einrichtung/v1/geraetecode"
NACHWEIS_BODY = b"{}"
