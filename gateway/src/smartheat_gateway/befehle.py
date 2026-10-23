"""Befehle des Gateways (Spec 5b 3.4; Handler aus dem Agenten bis 0.5.0) fuer das Befehlsregister des Geraetekerns
(smartheat_device.commands): ein Handler bekommt Kontext und Nutzlast und liefert Done, Failed oder Waiting; Einmal-
Ausfuehrung, Gruende-Pruefung und Versand macht das Register. Gateway-Befehle: zigbee_permit_join, zigbee_devices,
driver_login, inventory (stunden 0: Erkennung des Treibers, sonst Messreihe des Treibers aus der Konfiguration;
Ergebnis jeweils als up/inventory), diagnostics und sign_off (Markierung, dann startet die Laufzeit als "abgemeldet"
neu und setzt die Anlage zurueck). hinweis und new_claim_code kann jedes Geraet (Geraetekern). Fehler als Grund und
details, nie Texte oder Geheimnisse (Spec Stufe 3 S5 6.2); DriverError und QuotaExhausted uebersetzt
fehler_uebersetzen in Vertragsgruende.

Blockierende Treiber-Aufrufe (Cloud-HTTP: Anmeldung, Erkennung, Inventur-Probe; mit Token-Erneuerung und 401-Wiederholung
bis 80 s) laufen im Hintergrund (ein eigener Daemon-Thread je Kontext), nicht im Geraete-Thread: der Handler liefert
Waiting, dessen check im Geraete-Thread nur das Future liest. Sonst stuenden down/setpoints, Dokumente und hello still,
und die Laufzeit liefe nach zwei Ack-Timeouts in den Notbetrieb (bis 0.5.0 lief das im Agenten-Prozess)."""
import functools
import json
import logging
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import UTC, datetime

from smartheat_core import dateien
from smartheat_core.zahlen import ist_endliche_zahl
from smartheat_device import fehler, wire
from smartheat_device.commands import CommandRegister, Done, Failed, InvalidPayload, Outcome, Waiting
from smartheat_device.geraet_host_basis import SIGN_OFF_WAIT_SECONDS, abmeldung_abwarten
from smartheat_device.hintergrund import (  # noqa: F401  (HINTERGRUND_NAME: Rueck-Import fuer tests/test_befehle.py)
    HINTERGRUND_NAME,
    Hintergrund,
)
from smartheat_device.nachrichten import Vertragsfehler, pruefen
from smartheat_gateway import config as gateway_config
from smartheat_gateway.drivers import registry
from smartheat_gateway.drivers.base import Driver, DriverError
from smartheat_gateway.paths import Paths
from smartheat_gateway.quota import QuotaExhausted
from smartheat_gateway.zigbee import ZigbeeMirror
from smartheat_runtime import dokument_config
from smartheat_runtime.dokument_config import ConfigError

logger = logging.getLogger(__name__)

INVENTORY_EVERY_SECONDS = 900
ERKENNUNG_SECONDS = 120
ZIGBEE_VALUE_FIELDS = ("temperature", "local_temperature", "occupied_heating_setpoint", "humidity")
MAX_WERT_EBENEN = 32  # tiefer verschachtelte Werte der Zigbee-Geraeteliste fallen weg (Testoffensive T5', Task 4)


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
#: Arten, die nach einem Neustart bei erneuter Zustellung noch einmal laufen (CommandRegister.register, wiederholbar)
WIEDERHOLBAR = frozenset({"sign_off", "inventory"})


def _handler(kind: str):
    assert kind in wire.COMMANDS, kind

    def decorate(function: Handler) -> Handler:
        HANDLERS[kind] = function
        return function

    return decorate


def eintragen(register: CommandRegister, ctx: Kontext) -> None:
    for kind, function in HANDLERS.items():
        # sign_off ist idempotent (die Markierung setzt den Neustart nur einmal): nach dem Neustart laeuft es erneut,
        # damit das Ergebnis kommt, sobald der Status "abgemeldet" zeigt (D1: wiederholbar). inventory liest nur und
        # setzt die Messreihe aus der Datei fort: fiel ein Neustart in die Erkennung, laeuft sie bei der erneuten
        # Zustellung noch einmal, statt mit "intern" zu enden (L4-02). Grenze: hoechstens eine Cloud-Abfrage je
        # Neustart, solange der Server den Befehl zustellt (bis expires_at); eine Neustart-Schleife wiederholte sie.
        register.register(kind, functools.partial(function, ctx), wiederholbar=kind in WIEDERHOLBAR)


def fehler_uebersetzen(error: Exception) -> Failed | None:
    """Fuer CommandRegister(uebersetzen=...): Treiber-Fehler tragen ihren Vertragsgrund."""
    if isinstance(error, DriverError):
        return Failed(error.grund, error.details)
    if isinstance(error, QuotaExhausted):
        return Failed("kontingent_erschoepft")
    return None


def _int(payload: dict, key: str, low: int, high: int) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise InvalidPayload(key, "bereich", min=low, max=high)
    return value


def _driver(ctx: Kontext, driver_id, parameter: dict | None = None) -> Driver:
    if driver_id not in registry.DRIVERS:
        raise DriverError("treiber_unbekannt")
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
                return Failed("anlage_nicht_erreichbar")
            return None
        return future.result()

    sofort = check()
    return sofort if sofort is not None else Waiting(check, frist)


def _zigbee_not_ready(ctx: Kontext) -> Failed | None:
    if ctx.mirror.online is not True:
        return Failed("zigbee_nicht_bereit")
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
        devices.append(_zigbee_geraet({
            "ieee": device.ieee, "model": device.model, "vendor": device.vendor, "art": device.art,
            "werte": {key: data[key] for key in ZIGBEE_VALUE_FIELDS if key in data and _sendbar(data[key])},
            "batterie": _batterie(data),
            "faehigkeiten": sorted(device.faehigkeiten), "last_seen": last_seen,
        }))
    return Done({"devices": devices})


def _tiefer_als(wert, grenze: int) -> bool:
    """Ist der Wert mehr als `grenze` Ebenen aus Listen und Objekten tief verschachtelt? Iterativ, ohne Rekursion: der Wert
    kommt als JSON vom Bus, und json.loads liest bis rund 9998 Ebenen."""
    offen = [(wert, 0)]
    while offen:
        eintrag, ebene = offen.pop()
        if isinstance(eintrag, dict):
            kinder = list(eintrag.values())
        elif isinstance(eintrag, list | tuple):
            kinder = list(eintrag)
        else:
            continue
        if ebene + 1 > grenze:
            return True
        offen.extend((kind, ebene + 1) for kind in kinder)
    return False


def _sendbar(wert) -> bool:
    """Kann der Wert in die Antwort an den Server? Der Link sendet mit allow_nan=False: NaN und Unendlich (json.loads liest
    sie vom Bus) liessen die ganze Antwort scheitern und die Geraeteliste der Einrichtung leer. Eine Zahl muss endlich sein
    (auch eine Ganzzahl ausserhalb des float-Bereichs gilt als nicht endlich), ein verschachtelter Wert darf keine enthalten.
    Zigbee2MQTT liefert flache Werte: tiefer als MAX_WERT_EBENEN verschachtelt faellt weg (json.dumps wirft sonst bei rund
    9998 Ebenen RecursionError, kein ValueError, und die Antwort legt noch Ebenen um den Wert)."""
    if isinstance(wert, int | float) and not isinstance(wert, bool):
        return ist_endliche_zahl(wert)
    if _tiefer_als(wert, MAX_WERT_EBENEN):
        return False
    try:
        json.dumps(wert, allow_nan=False)
    except (ValueError, TypeError, RecursionError):  # RecursionError: Guertel, nicht erreichbar bei Tiefengrenze 32
        return False
    return True


def _batterie(daten: dict):
    """Batteriestand wie gemeldet (Prozent oder Warnflagge); ein nicht sendbarer Wert ist wie keine Angabe None."""
    wert = daten.get("battery", daten.get("battery_low"))
    return wert if _sendbar(wert) else None


def _zigbee_geraet(eintrag: dict) -> dict:
    """Ein Geraet der Liste mit genau den Feldern des Vertrags (wire.ZIGBEE_DEVICE_FIELDS), art aus ZIGBEE_ARTEN. Eine
    Abweichung ist ein Vertragsfehler ohne Werte (Regel 6): der Befehl endet mit intern."""
    pruefen("zigbee_devices.device", eintrag, wire.ZIGBEE_DEVICE_FIELDS)
    if eintrag["art"] not in wire.ZIGBEE_ARTEN:
        raise Vertragsfehler("zigbee_devices.device: art steht nicht in ZIGBEE_ARTEN")
    return eintrag


@_handler("driver_login")
def driver_login(ctx: Kontext, payload: dict) -> Outcome:
    # Phasengenau (V1 strikt): genau die Felder von wire.DRIVER_LOGIN_PHASES[phase]; die Annahme des Registers
    # (commands.PHASENBEFEHLE) prueft davon nur fremde Namen ausserhalb aller Phasen.
    if "phase" not in payload:
        raise InvalidPayload("phase", "fehlt")
    phase = payload["phase"]
    if not isinstance(phase, str):
        raise InvalidPayload("phase", "ungueltig")
    if phase not in wire.DRIVER_LOGIN_PHASES:
        raise InvalidPayload("phase", "unbekannt")
    erlaubt = wire.DRIVER_LOGIN_PHASES[phase]
    if fremd := fehler.fremdes_feld(payload, erlaubt):
        return Failed("ungueltige_nutzlast", fremd)
    for feld in erlaubt:
        if feld not in payload:
            raise InvalidPayload(feld, "fehlt")
    driver = _driver(ctx, payload.get("driver_id"))
    if type(driver).login_kind is None:
        raise DriverError("login_nicht_noetig")

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
    if isinstance(filter_, dict) and (fremd := fehler.fremdes_feld(filter_, wire.INVENTUR_FILTER_GATEWAY_FIELDS)):
        return Failed("ungueltige_nutzlast", fremd)  # V1 ist beidseitig strikt (Nutzer 2026-10-16)
    treiber = filter_.get("treiber") if isinstance(filter_, dict) else None
    if not isinstance(treiber, str):
        fehlt = filter_ is None or (isinstance(filter_, dict) and "treiber" not in filter_)
        raise InvalidPayload("treiber", "fehlt" if fehlt else "ungueltig")
    driver = _driver(ctx, treiber)
    return _im_hintergrund(ctx, "inventory", lambda: Done({}, inventur=driver.erkennung()), frist_s=ERKENNUNG_SECONDS)


@_handler("inventory")
def inventory(ctx: Kontext, payload: dict) -> Outcome:
    hours = _int(payload, "stunden", 0, 48)
    if hours == 0:
        return _erkennung(ctx, payload)
    konfiguration = ctx.konfiguration()
    if not dokument_config.eingerichtet(konfiguration):
        return Failed("nicht_eingerichtet")
    try:
        spec = gateway_config.bindung(konfiguration).driver_spec()
    except ConfigError:
        raise DriverError("treiber_unbekannt") from None
    driver = _driver(ctx, spec["id"], spec["parameter"])
    series = _series(ctx, driver.driver_id, hours)
    deadline_in = min(max(series["started"] + hours * 3600 - ctx.wall(), 0), hours * 3600)
    ready_at = ctx.clock() + deadline_in
    last_failure = Failed("anlage_nicht_erreichbar")  # nur fuer eine Reihe ohne Probe
    laufend: Future | None = None  # Probe im Hintergrund

    def check() -> Done | Failed | None:
        nonlocal last_failure, laufend
        if not _adopt_saved(ctx, series):  # Reihe weg oder ersetzt (Abmelden, Aufraeumen): nichts mehr abrufen
            if laufend is not None:
                laufend.cancel()
            return Failed("anlage_nicht_erreichbar")
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
                last_failure = (Failed(error.grund, error.details) if isinstance(error, DriverError)
                                else Failed("kontingent_erschoepft"))
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


@_handler("sign_off")
def sign_off(ctx: Kontext, payload: dict) -> Outcome:
    """Abmelden (Spec TP7 3.3, 5b 4.5): Markierung fuer die Anlage, dann startet die Laufzeit als "abgemeldet" neu und
    setzt die Anlage zurueck (LeverPipeline.restore_and_clear): die Hebel aus restore_originals auf ihre Ursprungswerte,
    die uebrigen auf die zuletzt gelernten Werte (Wiederherstellungspunkt). Erneut zugestellt (nach dem Neustart):
    dieselbe Markierung, kein zweiter Neustart. Fertig, sobald der Status "abgemeldet" fuer diese Anlage zeigt; werte
    ist der Wiederherstellungspunkt danach."""
    konfiguration = ctx.konfiguration()
    if not dokument_config.eingerichtet(konfiguration):
        return Failed("nicht_eingerichtet")
    assert konfiguration is not None
    anlage = konfiguration["anlage"]
    if gateway_config.abmelden(ctx.paths, anlage):
        ctx.neustart()

    return abmeldung_abwarten(anlage, ctx.runtime_status, ctx.paths.backup, ctx.clock() + SIGN_OFF_WAIT_SECONDS)
