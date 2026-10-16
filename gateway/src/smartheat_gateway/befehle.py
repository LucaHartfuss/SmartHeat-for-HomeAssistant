"""Befehle des Gateways (Spec 5b 3.4; Handler aus dem Agenten bis 0.5.0) fuer das Befehlsregister des Geraetekerns
(smartheat_device.commands): ein Handler bekommt Kontext und Nutzlast und liefert Done, Failed oder Waiting; Einmal-
Ausfuehrung, Gruende-Pruefung und Versand macht das Register. Gateway-Befehle: zigbee_permit_join, zigbee_devices,
driver_login, inventory (stunden 0: Erkennung des Treibers, sonst Messreihe des Treibers aus der
Konfiguration; Ergebnis jeweils als up/inventory), diagnostics und
sign_off (Markierung, dann startet die Laufzeit als "abgemeldet" neu und setzt die Anlage zurueck). hinweis und
new_claim_code kann jedes Geraet (Geraetekern). Texte in Kundensprache, nie Geheimnisse; DriverError und
QuotaExhausted uebersetzt fehler_uebersetzen in Vertragsgruende.

Blockierende Treiber-Aufrufe (Cloud-HTTP: Anmeldung, Erkennung, Inventur-Probe; mit Token-Erneuerung und 401-Wiederholung
bis 80 s) laufen im Hintergrund (ein eigener Daemon-Thread je Kontext), nicht im Geraete-Thread: der Handler liefert
Waiting, dessen check im Geraete-Thread nur das Future liest. Sonst stuenden down/setpoints, Dokumente und hello still,
und die Laufzeit liefe nach zwei Ack-Timeouts in den Notbetrieb (bis 0.5.0 lief das im Agenten-Prozess)."""
import functools
import logging
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import UTC, datetime

from smartheat_core import dateien
from smartheat_device import wire
from smartheat_device.commands import CommandRegister, Done, Failed, InvalidPayload, Outcome, Waiting
from smartheat_device.hintergrund import (  # noqa: F401  (HINTERGRUND_NAME: Rueck-Import fuer tests/test_befehle.py)
    HINTERGRUND_NAME,
    Hintergrund,
)
from smartheat_gateway import config as gateway_config
from smartheat_gateway.drivers import registry
from smartheat_gateway.drivers.base import Driver, DriverError
from smartheat_gateway.paths import Paths
from smartheat_gateway.quota import QuotaExhausted
from smartheat_gateway.zigbee import ZigbeeMirror
from smartheat_runtime import dokument_config
from smartheat_runtime.dokument_config import ConfigError
from smartheat_runtime.status import STATUS_ABGEMELDET

logger = logging.getLogger(__name__)

INVENTORY_EVERY_SECONDS = 900
ERKENNUNG_SECONDS = 120
SIGN_OFF_WAIT_SECONDS = 24 * 3600
INVENTORY_ABORTED_TEXT = "Die Inventur wurde abgebrochen, weil sich die Einrichtung des Gateways geändert hat."
INVENTORY_NO_SAMPLES_TEXT = "Die Inventur konnte keine einzige Probe abrufen, weil die Anlage nicht erreichbar war."
ZIGBEE_VALUE_FIELDS = ("temperature", "local_temperature", "occupied_heating_setpoint", "humidity")
QUOTA_TEXT = "Das Abfrage-Kontingent beim Hersteller ist erschöpft, später erneut versuchen."
NOT_SET_UP_TEXT = "Das Gateway ist nicht eingerichtet."
DRIVER_UNKNOWN_TEXT = "Dieser Hersteller-Zugang wird vom Gateway nicht unterstützt."
ZIGBEE_TEXT = "Der Zigbee-Stick ist nicht bereit. Bitte prüfen, ob er eingesteckt ist."
DRIVER_TIMEOUT_TEXT = "Der Hersteller-Zugang hat nicht rechtzeitig geantwortet, später erneut versuchen."


@dataclass
class Kontext:
    paths: Paths
    mirror: ZigbeeMirror
    konfiguration: Callable[[], dict | None]
    runtime_status: Callable[[], dict | None]
    neustart: Callable[[], None]
    diagnostics: Callable[[], dict] = dict
    clock: Callable[[], float] = time.monotonic
    wall: Callable[[], float] = time.time
    hintergrund: Hintergrund = field(default_factory=Hintergrund)


Handler = Callable[[Kontext, dict], Outcome]
HANDLERS: dict[str, Handler] = {}


def _handler(kind: str):
    assert kind in wire.COMMANDS, kind

    def decorate(function: Handler) -> Handler:
        HANDLERS[kind] = function
        return function

    return decorate


def eintragen(register: CommandRegister, ctx: Kontext) -> None:
    for kind, function in HANDLERS.items():
        # sign_off ist idempotent (die Markierung setzt den Neustart nur einmal): nach dem Neustart laeuft es erneut,
        # damit das Ergebnis kommt, sobald der Status "abgemeldet" zeigt (D1: wiederholbar).
        register.register(kind, functools.partial(function, ctx), wiederholbar=kind == "sign_off")


def fehler_uebersetzen(error: Exception) -> Failed | None:
    """Fuer CommandRegister(uebersetzen=...): Treiber-Fehler tragen ihren Vertragsgrund."""
    if isinstance(error, DriverError):
        return Failed(error.grund, error.text)
    if isinstance(error, QuotaExhausted):
        return Failed("kontingent_erschoepft", QUOTA_TEXT)
    return None


def _int(payload: dict, key: str, low: int, high: int) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise InvalidPayload(f"{key} muss eine ganze Zahl von {low} bis {high} sein.")
    return value


def _driver(ctx: Kontext, driver_id, parameter: dict | None = None) -> Driver:
    if driver_id not in registry.DRIVERS:
        raise DriverError("treiber_unbekannt", DRIVER_UNKNOWN_TEXT)
    return registry.create(driver_id, parameter or {}, ctx.paths, clock=ctx.clock, writer=False)


def _im_hintergrund(ctx: Kontext, kind: str, call: Callable[[], Done | Failed],
                    frist_s: float | None = None) -> Outcome:
    """Blockierender Aufruf im Hintergrund; check() im Geraete-Thread liest nur das Future. Eine Ausnahme des Aufrufs
    wirft check() erneut, das Register uebersetzt sie wie bisher (fehler_uebersetzen). Antwortet der Treiber bis zur
    Frist des Befehls (wire, expires; frist_s, wenn die Frist von der Nutzlast abhaengt) nicht, endet der Befehl mit
    anlage_nicht_erreichbar. Ist der Aufruf sofort fertig, kommt das Ergebnis direkt."""
    future = ctx.hintergrund.submit(call)
    expires = frist_s if frist_s is not None else wire.COMMANDS[kind].expires_seconds
    assert expires is not None, kind
    frist = ctx.clock() + expires

    def check() -> Done | Failed | None:
        if not future.done():
            if ctx.clock() >= frist:
                future.cancel()
                return Failed("anlage_nicht_erreichbar", DRIVER_TIMEOUT_TEXT)
            return None
        return future.result()

    sofort = check()
    return sofort if sofort is not None else Waiting(check, frist)


def _zigbee_not_ready(ctx: Kontext) -> Failed | None:
    if ctx.mirror.online is not True:
        return Failed("zigbee_nicht_bereit", ZIGBEE_TEXT)
    return None


@_handler("zigbee_permit_join")
def zigbee_permit_join(ctx: Kontext, payload: dict) -> Outcome:
    seconds = _int(payload, "seconds", 1, 254)
    not_ready = _zigbee_not_ready(ctx)
    if not_ready:
        return not_ready
    ctx.mirror.permit_join(seconds)
    return Done({})


@_handler("zigbee_devices")
def zigbee_devices(ctx: Kontext, payload: dict) -> Outcome:
    not_ready = _zigbee_not_ready(ctx)
    if not_ready:
        return not_ready
    devices = []
    for device in ctx.mirror.devices():
        data = ctx.mirror.payload(device.ieee)
        seen = ctx.mirror.last_seen(device.ieee)
        last_seen = None if seen is None else datetime.fromtimestamp(ctx.wall() - (ctx.clock() - seen), UTC).isoformat()
        devices.append({
            "ieee": device.ieee, "model": device.model, "vendor": device.vendor, "art": device.art,
            "werte": {key: data[key] for key in ZIGBEE_VALUE_FIELDS if key in data},
            "batterie": data.get("battery", data.get("battery_low")),
            "faehigkeiten": sorted(device.faehigkeiten), "last_seen": last_seen,
        })
    return Done({"devices": devices})


@_handler("driver_login")
def driver_login(ctx: Kontext, payload: dict) -> Outcome:
    phase = payload.get("phase")
    if phase not in wire.DRIVER_LOGIN_PHASES:
        raise InvalidPayload("phase muss begin, finish oder password sein.")
    driver = _driver(ctx, payload.get("driver_id"))
    if type(driver).login_kind is None:
        raise DriverError("login_nicht_noetig", "Für diesen Zugang ist keine Anmeldung nötig.")

    def anmelden() -> Done:
        if phase == "begin":
            return Done(driver.login_begin(payload))
        driver.login_finish(payload)
        return Done({})

    return _im_hintergrund(ctx, "driver_login", anmelden)


def forget_inventory(paths: Paths) -> None:
    """Proben gehoeren zur Anlage der Einrichtung; nach Abmelden oder Aufraeumen nie in einer neuen Inventur. Wirft nie."""
    for path in (paths.inventur, paths.inventur.with_name(paths.inventur.name + ".tmp")):
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            logger.error("Inventur-Proben nicht gelöscht (%s)", type(error).__name__)


def _erkennung(ctx: Kontext, payload: dict) -> Outcome:
    """Inventur stunden 0 (Spec Stufe 3 S5 5.1): Erkennung des Treibers aus dem Filter, vor und nach der Einrichtung;
    die Daten gehen als up/inventory vor dem Ergebnis."""
    filter_ = payload.get("filter")
    treiber = filter_.get("treiber") if isinstance(filter_, dict) and set(filter_) == set(
        wire.INVENTUR_FILTER_GATEWAY_FIELDS) else None
    if not isinstance(treiber, str):
        raise InvalidPayload("Die Erkennung braucht filter.treiber.")
    driver = _driver(ctx, treiber)
    return _im_hintergrund(ctx, "inventory", lambda: Done({}, inventur=driver.erkennung()), frist_s=ERKENNUNG_SECONDS)


@_handler("inventory")
def inventory(ctx: Kontext, payload: dict) -> Outcome:
    hours = _int(payload, "stunden", 0, 48)
    if hours == 0:
        return _erkennung(ctx, payload)
    konfiguration = ctx.konfiguration()
    if not dokument_config.eingerichtet(konfiguration):
        return Failed("nicht_eingerichtet", NOT_SET_UP_TEXT)
    try:
        spec = gateway_config.bindung(konfiguration).driver_spec()
    except ConfigError:
        raise DriverError("treiber_unbekannt", DRIVER_UNKNOWN_TEXT) from None
    driver = _driver(ctx, spec["id"], spec["parameter"])
    series = _series(ctx, driver.driver_id, hours)
    deadline_in = min(max(series["started"] + hours * 3600 - ctx.wall(), 0), hours * 3600)
    ready_at = ctx.clock() + deadline_in
    last_failure = Failed("anlage_nicht_erreichbar", INVENTORY_NO_SAMPLES_TEXT)  # nur fuer eine Reihe ohne Probe
    laufend: Future | None = None  # Probe im Hintergrund

    def check() -> Done | Failed | None:
        nonlocal last_failure, laufend
        if not _adopt_saved(ctx, series):  # Reihe weg oder ersetzt (Abmelden, Aufraeumen): nichts mehr abrufen
            if laufend is not None:
                laufend.cancel()
            return Failed("anlage_nicht_erreichbar", INVENTORY_ABORTED_TEXT)
        now = ctx.wall()
        if laufend is None and series["next_at"] <= now < series["started"] + hours * 3600:
            # Zeitpunkt zuerst in der Datei belegen: ein zweiter wartender Befehl derselben Reihe ruft ihn nicht ab.
            series["next_at"] = now + INVENTORY_EVERY_SECONDS
            dateien.schreiben_json(ctx.paths.inventur, series, modus=dateien.PRIVAT)
            laufend = ctx.hintergrund.submit(driver.inventory_sample)
        if laufend is not None:
            if not laufend.done():
                return None  # auch am Fensterende: die letzte Probe gehoert noch dazu
            probe, laufend = laufend, None
            try:
                series["samples"].append(probe.result())
            except (DriverError, QuotaExhausted) as error:
                # Eine einzelne Probe (5xx, Zeitueberschreitung, 429) beendet die Reihe nicht; nur der Typname ins Log.
                logger.warning("Inventur-Probe uebersprungen (%s)", type(error).__name__)
                last_failure = (Failed(error.grund, error.text) if isinstance(error, DriverError)
                                else Failed("kontingent_erschoepft", QUOTA_TEXT))
            dateien.schreiben_json(ctx.paths.inventur, series, modus=dateien.PRIVAT)
        if ctx.clock() < ready_at:
            return None
        if not series["samples"]:
            return last_failure
        return Done({}, inventur=driver.inventory(hours, series["samples"]))

    return Waiting(check, ready_at + 3600)


def _is_number(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _series(ctx: Kontext, driver_id: str, hours: int) -> dict:
    """Reihe fortsetzen (Neustart, erneute Zustellung: nichts doppelt abgerufen) oder neu beginnen: anderer Treiber,
    andere Dauer, abgelaufenes Fenster oder Beginn in der Zukunft (Uhrsprung zurueck)."""
    saved = dateien.lesen_json(ctx.paths.inventur)
    now = ctx.wall()
    if (
        isinstance(saved, dict) and saved.get("driver_id") == driver_id and saved.get("hours") == hours
        and isinstance(saved.get("samples"), list) and _is_number(saved.get("started"))
        and _is_number(saved.get("next_at")) and saved["started"] <= now < saved["started"] + hours * 3600
    ):
        return saved
    series = {"driver_id": driver_id, "started": now, "hours": hours, "samples": [], "next_at": now}
    dateien.schreiben_json(ctx.paths.inventur, series, modus=dateien.PRIVAT)
    return series


def _adopt_saved(ctx: Kontext, series: dict) -> bool:
    """Neuere Proben derselben Reihe aus der Datei uebernehmen; fehlt sie oder gehoert sie zu einer anderen Reihe, ist
    die Einrichtung weg: False, der wartende Befehl legt sie nicht neu an und liefert keine alten Proben."""
    saved = dateien.lesen_json(ctx.paths.inventur)
    if not (isinstance(saved, dict) and isinstance(saved.get("samples"), list) and _is_number(saved.get("next_at"))
            and all(saved.get(key) == series[key] for key in ("driver_id", "hours", "started"))):
        return False
    series.update(samples=saved["samples"], next_at=saved["next_at"])
    return True


@_handler("diagnostics")
def diagnostics(ctx: Kontext, payload: dict) -> Outcome:
    return Done(ctx.diagnostics())


def _restore_values(ctx: Kontext) -> dict:
    backup = dateien.lesen_json(ctx.paths.backup)
    point = backup.get("restore_point") if isinstance(backup, dict) else None
    return dict(point) if isinstance(point, dict) else {}


@_handler("sign_off")
def sign_off(ctx: Kontext, payload: dict) -> Outcome:
    """Abmelden (Spec TP7 3.3, 5b 4.5): Markierung fuer die Anlage, dann startet die Laufzeit als "abgemeldet" neu und
    setzt die Anlage zurueck (LeverPipeline.restore_and_clear): die Hebel aus restore_originals auf ihre Ursprungswerte,
    die uebrigen auf die zuletzt gelernten Werte (Wiederherstellungspunkt). Erneut zugestellt (nach dem Neustart):
    dieselbe Markierung, kein zweiter Neustart. Fertig, sobald der Status "abgemeldet" fuer diese Anlage zeigt; werte
    ist der Wiederherstellungspunkt danach."""
    konfiguration = ctx.konfiguration()
    if not dokument_config.eingerichtet(konfiguration):
        return Failed("nicht_eingerichtet", NOT_SET_UP_TEXT)
    assert konfiguration is not None
    anlage = konfiguration["anlage"]
    if gateway_config.abmelden(ctx.paths, anlage):
        ctx.neustart()

    def check() -> Done | Failed | None:
        status = ctx.runtime_status()
        if not status or status.get("status") != STATUS_ABGEMELDET or status.get("tenant_id") != anlage:
            return None
        return Done({"zurueckgesetzt": status.get("grund") is None, "werte": _restore_values(ctx)})

    return Waiting(check, ctx.clock() + SIGN_OFF_WAIT_SECONDS)
