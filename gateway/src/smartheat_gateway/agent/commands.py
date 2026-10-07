"""Befehle des Agenten (Spec SHG G2 6.3, Vertrag G3 2.3, Plan G2a Praezisierung 10). Ein Handler liefert Done, Failed
oder Waiting; die Agent-Schleife prueft wartende Befehle weiter, ohne zu blockieren. Texte in Kundensprache, nie
Geheimnisse. Befehle mit Laufzeit-Wirkung (apply_config, set_room_target, sign_off) stehen in lifecycle.py.

Jeder gemeldete `grund` steht in der festen Liste des Befehls (wire.command_errors); ein anderer Grund wird als
`intern` gemeldet und protokolliert, damit der Server nie einen Grund ausserhalb des Vertrags sieht."""
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from smartheat_gateway.agent import identity as identity_module
from smartheat_gateway.agent import safety_warnings, wire
from smartheat_gateway.agent.context import AgentContext
from smartheat_gateway.drivers import registry
from smartheat_gateway.drivers.base import Driver, DriverError
from smartheat_gateway.files import read_json, write_json, write_text_private
from smartheat_gateway.quota import QuotaExhausted

logger = logging.getLogger(__name__)

INVENTORY_EVERY_SECONDS = 900
ZIGBEE_VALUE_FIELDS = ("temperature", "local_temperature", "occupied_heating_setpoint", "humidity")
QUOTA_TEXT = "Das Abfrage-Kontingent beim Hersteller ist erschöpft, später erneut versuchen."


@dataclass(frozen=True)
class Done:
    result: dict


@dataclass(frozen=True)
class Failed:
    grund: str
    text: str


@dataclass(frozen=True)
class Waiting:
    check: Callable[[], Done | Failed | None]
    deadline: float


Outcome = Done | Failed | Waiting
HANDLERS: dict[str, Callable[[AgentContext, dict], Outcome]] = {}


class InvalidPayload(ValueError):
    pass


def register(kind: str):
    def decorate(handler):
        HANDLERS[kind] = handler
        return handler

    return decorate


def _guarded[T](kind: str, call: Callable[[], T]) -> T | Failed:
    """Fuehrt `call` aus und uebersetzt Ausnahmen in Failed mit einem Grund aus der Liste des Befehls."""
    try:
        outcome = call()
    except InvalidPayload as error:
        failed = Failed("ungueltige_nutzlast", str(error))
    except DriverError as error:
        failed = Failed(error.grund, error.text)
    except QuotaExhausted:
        failed = Failed("kontingent_erschoepft", QUOTA_TEXT)
    except Exception:
        logger.exception("Befehl %s fehlgeschlagen", kind)
        return Failed("intern", "Interner Fehler im Gateway.")
    else:
        if not isinstance(outcome, Failed):
            return outcome
        failed = outcome
    if kind in wire.COMMANDS and failed.grund not in wire.command_errors(kind):
        logger.error("Befehl %s: Grund %s steht nicht in der Vertragsliste", kind, failed.grund)
        return Failed("intern", "Interner Fehler im Gateway.")
    return failed


def execute(ctx: AgentContext, kind: str, payload) -> Outcome:
    handler = HANDLERS.get(kind)
    if handler is None or not isinstance(payload, dict):
        return Failed("ungueltige_nutzlast", "Unbekannter Befehl.")
    outcome = _guarded(kind, lambda: handler(ctx, payload))
    if isinstance(outcome, Waiting):
        check = outcome.check
        return Waiting(lambda: _guarded(kind, check), outcome.deadline)
    return outcome


def _int(payload: dict, key: str, low: int, high: int) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise InvalidPayload(f"{key} muss eine ganze Zahl von {low} bis {high} sein.")
    return value


def _driver(ctx: AgentContext, payload: dict) -> Driver:
    driver_id = payload.get("driver_id")
    if driver_id not in registry.DRIVERS:
        raise DriverError("treiber_unbekannt", "Dieser Hersteller-Zugang wird vom Gateway nicht unterstützt.")
    return registry.create(driver_id, {}, ctx.paths, clock=ctx.clock, writer=False)


def _zigbee_not_ready(ctx: AgentContext) -> Failed | None:
    if ctx.mirror.online is not True:
        return Failed("zigbee_nicht_bereit", "Der Zigbee-Stick ist nicht bereit. Bitte prüfen, ob er eingesteckt ist.")
    return None


@register("zigbee_permit_join")
def _permit_join(ctx: AgentContext, payload: dict) -> Outcome:
    seconds = _int(payload, "seconds", 1, 254)
    not_ready = _zigbee_not_ready(ctx)
    if not_ready:
        return not_ready
    ctx.mirror.permit_join(seconds)
    return Done({})


@register("zigbee_devices")
def _devices(ctx: AgentContext, payload: dict) -> Outcome:
    not_ready = _zigbee_not_ready(ctx)
    if not_ready:
        return not_ready
    devices = []
    for device in ctx.mirror.devices():
        data = ctx.mirror.payload(device.ieee)
        seen = ctx.mirror.last_seen(device.ieee)
        last_seen = None if seen is None else datetime.fromtimestamp(
            ctx.wall() - (ctx.clock() - seen), UTC,
        ).isoformat()
        devices.append({
            "ieee": device.ieee, "model": device.model, "vendor": device.vendor, "art": device.art,
            "werte": {key: data[key] for key in ZIGBEE_VALUE_FIELDS if key in data},
            "batterie": data.get("battery", data.get("battery_low")),
            "faehigkeiten": sorted(device.faehigkeiten), "last_seen": last_seen,
        })
    return Done({"devices": devices})


@register("driver_login")
def _login(ctx: AgentContext, payload: dict) -> Outcome:
    phase = payload.get("phase")
    if phase not in wire.DRIVER_LOGIN_PHASES:
        raise InvalidPayload("phase muss begin, finish oder password sein.")
    driver = _driver(ctx, payload)
    if type(driver).login_kind is None:
        raise DriverError("login_nicht_noetig", "Für diesen Zugang ist keine Anmeldung nötig.")
    if phase == "begin":
        return Done(driver.login_begin(payload))
    driver.login_finish(payload)
    return Done({})


@register("driver_probe")
def _probe(ctx: AgentContext, payload: dict) -> Outcome:
    result = _driver(ctx, payload).probe()
    for candidate in result["kandidaten"]:
        candidate["sicherheitswarnungen"] = safety_warnings.compute(candidate)
    return Done(result)


@register("driver_inventory")
def _inventory(ctx: AgentContext, payload: dict) -> Outcome:
    hours = _int(payload, "stunden", 1, 48)
    driver = _driver(ctx, payload)
    series = _series(ctx, driver.driver_id, hours)
    deadline_in = min(max(series["started"] + hours * 3600 - ctx.wall(), 0), hours * 3600)
    ready_at = ctx.clock() + deadline_in

    def check() -> Done | Failed | None:
        _adopt_saved(ctx, series)  # zwei laufende Befehle derselben Reihe teilen sie ueber die Datei
        now = ctx.wall()
        if now >= series["next_at"] and now < series["started"] + hours * 3600:
            series["samples"].append(driver.inventory_sample())
            series["next_at"] = now + INVENTORY_EVERY_SECONDS
            write_json(ctx.paths.inventory_samples, series, private=True)
        if ctx.clock() < ready_at:
            return None
        return Done(driver.inventory(hours, series["samples"]))

    return Waiting(check, ready_at + 3600)


def _series(ctx: AgentContext, driver_id: str, hours: int) -> dict:
    """Reihe fortsetzen (Neustart, erneute Zustellung: nichts wird doppelt abgerufen) oder neu beginnen: anderer
    Treiber, andere Dauer, ein abgelaufenes Fenster oder ein Beginn in der Zukunft (Uhrsprung zurueck) beginnen neu."""
    saved = read_json(ctx.paths.inventory_samples)
    now = ctx.wall()
    if (
        isinstance(saved, dict) and saved.get("driver_id") == driver_id and saved.get("hours") == hours
        and isinstance(saved.get("samples"), list) and _is_number(saved.get("started"))
        and _is_number(saved.get("next_at")) and saved["started"] <= now < saved["started"] + hours * 3600
    ):
        return saved
    series = {"driver_id": driver_id, "started": now, "hours": hours, "samples": [], "next_at": now}
    write_json(ctx.paths.inventory_samples, series, private=True)
    return series


def _adopt_saved(ctx: AgentContext, series: dict) -> None:
    """Neuere Proben derselben Reihe (gleicher Treiber, gleiche Dauer, gleicher Beginn) aus der Datei uebernehmen:
    ein zweiter Befehl mit anderer ID darf einen 15-Minuten-Zeitpunkt nicht ein zweites Mal abrufen."""
    saved = read_json(ctx.paths.inventory_samples)
    if (
        isinstance(saved, dict) and isinstance(saved.get("samples"), list) and _is_number(saved.get("next_at"))
        and all(saved.get(key) == series[key] for key in ("driver_id", "hours", "started"))
    ):
        series.update(samples=saved["samples"], next_at=saved["next_at"])


def _is_number(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


@register("create_csr")
def _create_csr(ctx: AgentContext, payload: dict) -> Outcome:
    tenant_id = payload.get("tenant_id")
    if not isinstance(tenant_id, str) or not tenant_id:
        raise InvalidPayload("tenant_id fehlt.")
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (  # zuerst bauen und signieren: scheitert es, bleibt der vorhandene Schluessel unangetastet
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, tenant_id)]))
        .sign(key, hashes.SHA256())
    )
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    write_text_private(ctx.paths.transport_key, pem.decode())
    return Done({"csr": csr.public_bytes(serialization.Encoding.PEM).decode()})


@register("new_claim_code")
def _new_claim_code(ctx: AgentContext, payload: dict) -> Outcome:
    if ctx.device_state != "nicht_uebernommen":
        return Failed(
            "bereits_uebernommen", "Das Gerät ist übernommen; ein neuer Code ist erst nach dem Entfernen möglich.",
        )
    ctx.identity = identity_module.replace_claim_code(ctx.paths, ctx.identity)
    ctx.register_requested = True
    return Done({})


@register("diagnostics")
def _diagnostics(ctx: AgentContext, payload: dict) -> Outcome:
    return Done(ctx.diagnostics())
