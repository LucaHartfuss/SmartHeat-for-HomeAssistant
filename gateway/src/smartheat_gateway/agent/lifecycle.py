"""Befehle mit Wirkung auf die Laufzeit (Spec SHG G2 6.3/6.4, Plan G2a Praezisierungen 2-5, 11): apply_config,
set_room_target, sign_off und das Aufraeumen nach dem Abmelden. Der Agent schreibt runtime_config.json und secrets/
(Geheimnisse zuerst, Review Focus 3) und meldet der Laufzeit per shg/cmd/reload; bestaetigt wird ueber shg/status bzw.
shg/raum. Ein Schreiber je Datei: den Laufzeit-Ordner loescht der Agent nur, wenn die Laufzeit abgemeldet ruht."""
import logging
import shutil
import uuid

from smartheat_gateway import topics
from smartheat_gateway.agent.commands import Done, Failed, InvalidPayload, Outcome, Waiting, register
from smartheat_gateway.agent.context import AgentContext
from smartheat_gateway.config import (
    add_transport_key,
    check_apply_config,
    is_configured,
    load_raw,
    parse,
    redact,
    split_secrets,
)
from smartheat_gateway.files import read_json, write_json
from smartheat_gateway.paths import Paths
from smartheat_gateway.target_store import is_valid_portal_target
from smartheat_runtime.options import ConfigError
from smartheat_runtime.status import STATUS_ABGEMELDET

logger = logging.getLogger(__name__)

APPLY_CONFIRM_SECONDS = 120
ROOM_TARGET_CONFIRM_SECONDS = 60
SIGN_OFF_WAIT_SECONDS = 24 * 3600
_CREDENTIAL_KEYS = ("mqtt_username", "mqtt_password", "tls_certificate", "cloudflared")


def merge_existing_secrets(paths: Paths, config: dict) -> dict:
    """Fehlende Geheimnisse = vorhandene behalten (Plan G2a Praezisierung 2); die einzige Stelle dieser Regel.
    Das MQTT-Passwort gehoert zum Benutzernamen, das Tunnel-Geheimnis zum Tunnel: ohne mqtt_username bzw. cloudflared
    (Abmelden) bleiben sie nicht erhalten. Aus einer abgemeldeten Einrichtung wird nie etwas uebernommen."""
    existing = load_raw(paths)
    merged = dict(config)
    if existing.get("abgemeldet") is True:
        return merged
    if "installation_token" not in merged and existing.get("installation_token"):
        merged["installation_token"] = existing["installation_token"]
    if "mqtt_password" not in merged and merged.get("mqtt_username") and existing.get("mqtt_password"):
        merged["mqtt_password"] = existing["mqtt_password"]
    cloudflared, old = merged.get("cloudflared"), existing.get("cloudflared")
    if (
        isinstance(cloudflared, dict) and "service_token_secret" not in cloudflared
        and isinstance(old, dict) and old.get("service_token_secret")
    ):
        merged["cloudflared"] = {**cloudflared, "service_token_secret": old["service_token_secret"]}
    return merged


def write_config(paths: Paths, config: dict) -> None:
    """Geheimnisse zuerst (fehlende = vorhandene behalten), dann die Konfiguration mit der neuen setup_id: startet die
    Laufzeit dazwischen, sieht sie die alte setup_id und startet bei der Wache neu (Review Focus 3)."""
    public, secrets = split_secrets(merge_existing_secrets(paths, config))
    write_json(paths.runtime_secrets, secrets, private=True)
    write_json(paths.runtime_config, public)


def remove_setup(paths: Paths, *, keep_new_credentials: bool = False) -> None:
    """Entfernt eine (abgemeldete) Einrichtung: zuerst den Laufzeit-Ordner (Praezisierung 3), dann Geheimnisse,
    Transport-Schluessel und Treiber-Tokens, die Konfiguration mit der Markierung `abgemeldet` ZULETZT; bricht es
    mittendrin ab (Stromausfall), findet der naechste Durchlauf die Markierung und raeumt den Rest weg.
    keep_new_credentials (apply_config): Transport-Schluessel und Treiber-Tokens stammen dann aus create_csr bzw.
    driver_login derselben neuen Einrichtung (der alte Schluessel ist seit sign_off weg) und bleiben."""
    shutil.rmtree(paths.runtime_dir, ignore_errors=True)
    paths.runtime_secrets.unlink(missing_ok=True)
    if not keep_new_credentials:
        paths.transport_key.unlink(missing_ok=True)
        shutil.rmtree(paths.driver_secrets_dir, ignore_errors=True)
    paths.runtime_config.unlink(missing_ok=True)


def _status_with(ctx: AgentContext, setup_id: str) -> dict | None:
    status = ctx.runtime_status
    return status if status is not None and status.get("setup_id") == setup_id else None


@register("apply_config")
def apply_config(ctx: AgentContext, payload: dict) -> Outcome:
    setup_id, config = payload.get("setup_id"), payload.get("config")
    if not isinstance(setup_id, str) or not isinstance(config, dict) or config.get("setup_id") != setup_id:
        raise InvalidPayload("setup_id fehlt oder passt nicht zur Konfiguration.")
    problem = check_apply_config(config)
    if problem:
        return Failed("konfiguration_ungueltig", f"Die Konfiguration ist ungültig ({problem}).")
    candidate = merge_existing_secrets(ctx.paths, config)
    add_transport_key(ctx.paths, candidate)  # Schluessel aus create_csr (nur iot_core), nur fuer die Pruefung
    try:
        parse(candidate, ctx.paths)
    except Exception as error:  # jeder Fehlertext geschwaerzt, nie ungeschwaerzt in logger.exception (Regel 6)
        text = redact(str(error), candidate)
        if not isinstance(error, ConfigError):
            logger.error("apply_config: Pruefung gescheitert (%s): %s", type(error).__name__, text)
        return Failed("konfiguration_ungueltig", f"Die Konfiguration ist ungültig: {text}")
    if load_raw(ctx.paths).get("abgemeldet") is True:
        # Abgemeldet (auch mit gescheitertem Zuruecksetzen): neu einrichten wie frisch installiert (Praezisierung 3).
        remove_setup(ctx.paths, keep_new_credentials=True)
    write_config(ctx.paths, config)
    ctx.bus.publish(topics.CMD_RELOAD, {"setup_id": setup_id})

    def check() -> Done | Failed | None:
        if _status_with(ctx, setup_id) is not None:
            return Done({"setup_id": setup_id})
        return None

    return Waiting(check, ctx.clock() + APPLY_CONFIRM_SECONDS)


@register("set_room_target")
def set_room_target(ctx: AgentContext, payload: dict) -> Outcome:
    value = payload.get("value")
    if not is_valid_portal_target(value):  # Regel 4; die Laufzeit prueft erneut (TargetStore.apply_portal)
        return Failed(
            "ausserhalb_bereich", "Die Wunschtemperatur muss zwischen 15 und 25 °C in Schritten von 0,5 K liegen.",
        )
    if not is_configured(load_raw(ctx.paths)):
        return Failed("nicht_eingerichtet", "Das Gateway ist noch nicht eingerichtet.")
    ctx.bus.publish(topics.CMD_ROOM_TARGET, {"value": value, "source": "portal", "ts": ctx.wall()})

    def check() -> Done | Failed | None:
        # Bestaetigt, sobald shg/raum das Soll zeigt; ein schon aktiver Wert ist damit sofort bestaetigt.
        raum = ctx.raum
        if raum is not None and raum.get("soll") == value:
            return Done({"value": value})
        return None

    return Waiting(check, ctx.clock() + ROOM_TARGET_CONFIRM_SECONDS)


def _restore_values(ctx: AgentContext) -> dict:
    backup = read_json(ctx.paths.backup)
    point = backup.get("restore_point") if isinstance(backup, dict) else None
    return dict(point) if isinstance(point, dict) else {}


@register("sign_off")
def sign_off(ctx: AgentContext, payload: dict) -> Outcome:
    raw = load_raw(ctx.paths)
    if not raw.get("tenant_id"):
        return Failed("nicht_eingerichtet", "Das Gateway ist nicht eingerichtet.")
    setup_id = raw.get("setup_id")
    # Erneut zugestellt (Agent-Neustart) und schon abgemeldet: dieselbe setup_id, nichts neu schreiben.
    if raw.get("abgemeldet") is not True or not isinstance(setup_id, str):
        setup_id = f"abmelden-{uuid.uuid4().hex[:12]}"
        config = {k: v for k, v in raw.items() if k not in (*_CREDENTIAL_KEYS, "tls_private_key")}
        config.update(setup_id=setup_id, abgemeldet=True)
        write_config(ctx.paths, config)
        ctx.bus.publish(topics.CMD_RELOAD, {"setup_id": setup_id})
    # Immer, auch beim erneuten Zustellen: ein Abbruch zwischen write_config und hier liesse ihn sonst liegen.
    ctx.paths.transport_key.unlink(missing_ok=True)

    def check() -> Done | Failed | None:
        status = _status_with(ctx, setup_id)
        if status is None or status.get("status") != STATUS_ABGEMELDET:
            return None
        return Done({"zurueckgesetzt": status.get("grund") is None, "werte": _restore_values(ctx)})

    return Waiting(check, ctx.clock() + SIGN_OFF_WAIT_SECONDS)


def cleanup_after_sign_off(ctx: AgentContext) -> bool:
    """Abschnitt 6.4 Punkt 3: erst wenn die Laufzeit abgemeldet OHNE Ruecksetzfehler ruht, die Einrichtung entfernen
    (remove_setup, Konfiguration zuletzt); Geraet, Zigbee und Kontingent bleiben. True = aufgeraeumt."""
    raw = load_raw(ctx.paths)
    setup_id = raw.get("setup_id")
    if raw.get("abgemeldet") is not True or not isinstance(setup_id, str):
        return False
    status = _status_with(ctx, setup_id)
    if status is None or status.get("status") != STATUS_ABGEMELDET or status.get("grund") is not None:
        return False
    remove_setup(ctx.paths)
    ctx.bus.publish(topics.CMD_RELOAD, {"setup_id": None})
    logger.info("Abmelden abgeschlossen, Gateway wieder frei (gleicher Uebernahme-Code)")
    return True
