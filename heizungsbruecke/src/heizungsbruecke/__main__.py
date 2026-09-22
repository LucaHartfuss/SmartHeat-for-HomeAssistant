import json
import logging
import math
import os
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import requests

from heizungsbruecke.backup_store import load_backup, save_backup
from heizungsbruecke.boost import decide_boost
from heizungsbruecke.bridge import apply_boost_decision, handle_down_message, publish_snapshot
from heizungsbruecke import daynight_snapshot, derived_sensors
from heizungsbruecke.failsafe import (
    FailsafeState,
    build_discovery_config,
    build_state_payload,
    enter_failsafe_if_stale,
    record_valid_message,
)
from heizungsbruecke.ha_api import HomeAssistantApi
from heizungsbruecke.ha_trigger_client import HaTriggerClient
from heizungsbruecke.manifest import ManifestError, build_manifest
from heizungsbruecke.mqtt_client import BridgeMqttClient
from heizungsbruecke.profiles import (
    UnknownProfileError,
    resolve_boost_defaults,
    resolve_local_clamps,
    resolve_window_defaults,
    window_size_hours,
)

OPTIONS_PATH = Path("/data/options.json")
BACKUP_PATH = Path("/data/backup.json")
FAILSAFE_PATH = Path("/data/failsafe_state.json")
DERIVED_SENSORS_PATH = Path("/data/derived_sensors.json")
DAYNIGHT_SNAPSHOT_PATH = Path("/data/daynight_snapshot_state.json")

MQTT_HOST = "127.0.0.1"
# Muss mit cloudflared_access_mqtt/config.yaml's `local_port`-Default
# uebereinstimmen (siehe Kommentar dort) -- kein geteilter Konfigurationswert
# zwischen den beiden Add-ons, nur Konvention.
MQTT_PORT = 18830

_REQUIRED_OPTIONS = (
    "tenant_id", "profile", "mqtt_username", "mqtt_password",
    "entity_room_actual", "entity_room_target",
    "entity_curve_current", "entity_offset_current", "entity_outdoor_temp", "entity_heat_limit",
)

# The add-on runs with `startup: services`, i.e. it can be started before HA Core has
# finished booting. A transient failure here (HA API not answering yet) must not be fatal
# on the first attempt -- retry with backoff before giving up. No config.yaml `watchdog`
# is set on purpose: once retries are exhausted the failure is treated as a genuine
# misconfiguration, and _run_bridge() returns (logs, doesn't crash the whole process)
# rather than crash-looping forever.
DERIVED_SENSORS_RETRY_DELAYS_SECONDS = (5, 10, 20, 40, 60, 60, 60)

# Boot-Reihenfolge-Rennen zwischen den beiden Add-ons (cloudflared_access_mqtt startet
# eventuell noch) oder ein kurzer Broker-Restart duerfen nicht sofort als dauerhafte
# Fehlkonfiguration gewertet werden -- gleiche Begruendung/Muster wie
# DERIVED_SENSORS_RETRY_DELAYS_SECONDS oben (Design-Spec Phase 1, Punkt 2).
MQTT_CONNECT_RETRY_DELAYS_SECONDS = (5, 10, 20, 40, 60)

# Staleness wird ab der letzten GUELTIGEN DOWN-NACHRICHT gemessen, nicht ab dem lokalen
# Check-Takt (local_check_interval_seconds) -- der laeuft (als Fallback, wenn
# HaTriggerClient nicht verbunden ist) alle 30s bis 3600s und aktualisiert den
# Failsafe-Timer nicht selbst. Massgeblich ist die Down-Nachrichten-Kadenz: der volle
# Snapshot-Publish laeuft jetzt taeglich + event-driven statt stuendlich, d.h. im Normalfall
# vergehen zwischen zwei gueltigen Down-Nachrichten bereits ~24h. Der Schwellwert braucht
# also Luft gegen diese ~24h-Kadenz, nicht gegen den 30-60s-Check-Takt -- sonst schlaegt
# der Failsafe durch reines Timing-Jitter bei rund der Haelfte aller Tage faelschlich an.
# 26h = 24h Kadenz + ~2h Puffer, deckungsgleich mit dem historischen Vor-Haertungs-Default
# dieses Add-ons (vor der Verschaerfung auf 4h am 2026-09-15). Bewusst wieder gelockert
# (Design-Spec 2026-09-16, Abschnitt C) -- der Nutzer haelt einen zusaetzlichen, von der
# Kurvenberechnung unabhaengigen Heartbeat aktuell nicht fuer noetig und akzeptiert die
# vergroeberte Erkennungsgeschwindigkeit, gekoppelt an die seltenere Down-Nachrichten-Kadenz.
DEFAULT_FAILSAFE_STALE_AFTER_HOURS = 26.0

# Design-Spec 2026-09-21: seit der Umstellung auf HaTriggerClient steuert dieser Wert
# nur noch den Watchdog-/Fallback-Takt (Boost-/target_changed-Check nur, wenn die
# WS-Verbindung down ist), nicht mehr routinemaessiges Polling -- 300s deckt sich mit
# telemetry_interval_seconds' bestehendem Default.
DEFAULT_LOCAL_CHECK_INTERVAL_SECONDS = 300

# Design-Spec 2026-09-16 (KPI-Erfassung), Abschnitt 1: eigenes, von
# local_check_interval_seconds UND vom (jetzt seltenen) vollen Snapshot-Publish
# entkoppeltes Intervall, damit Komfort-/Boost-KPIs nicht zu grobkoernig werden.
DEFAULT_TELEMETRY_INTERVAL_SECONDS = 300

# Design-Spec 2026-09-22 (Zieltemperatur-Debounce): 10s Stabilitaetsfenster, bevor eine
# room_target-Aenderung als "final" gilt (Boost-Start/-Ende und Heizkurvenanpassung
# reagieren erst danach, siehe _make_trigger_event_callback/_StableTargetBox weiter
# unten). Bewusst fest im Code (kein Add-on-Options-Feld) -- kein bestehender
# Plumbing-Mechanismus fuer Boost-aehnliche Parameter (auch boost_threshold_k ist
# profilbasiert, nicht per Config-Flow einstellbar), nur client1 als realer Nutzer
# aktuell. YAGNI, spaeter bei Bedarf nachruestbar.
ROOM_TARGET_DEBOUNCE_SECONDS = 10

# In-memory only (not persisted to backup.json) -- persisting it would reintroduce
# ~288 SD-card writes/day, exactly what A.2 eliminated for the other backup.json
# fields. Losing this marker on an add-on restart just causes one extra early
# telemetry publish, which is harmless for a purely observational KPI feed.
_last_telemetry_publish_ts: float | None = None

# Gleicher Hostname fuer jeden Tenant (kein Tenant-spezifischer Wert) -- siehe
# SmartHeat-HomeAssistant-Integration/custom_components/smartheat/api_client.py,
# DEFAULT_HEIZUNGSSERVER_BASE_URL. Hartkodiert wie MQTT_HOST/MQTT_PORT oben, aus
# demselben Grund: ein einzelner geteilter Wert, keine Pro-Tenant-Konfiguration.
ACCOUNTS_API_BASE_URL = "https://accounts.hartfussha.org"

logger = logging.getLogger(__name__)


class TenantNotEntitledError(Exception):
    """Raised when accounts-api meldet, dass dieser Tenant aktuell nicht berechtigt ist
    (Abo abgelaufen/pausiert, siehe Design-Spec Phase 3, Punkt 10)."""


def _check_entitlement(tenant_id: str, base_url: str = ACCOUNTS_API_BASE_URL) -> None:
    """Fragt vor jedem MQTT-Connect/HA-Zugriff bei accounts-api nach, ob dieser Tenant
    aktuell berechtigt ist. Ein Netzwerk-/Serverfehler wird bewusst NICHT als "nicht
    berechtigt" gewertet ("fail open") -- ein kurzer accounts-api-Ausfall soll die
    Heizungssteuerung eines zahlenden Kunden nicht stoppen. Nur eine explizite
    'active: false'-Antwort loest TenantNotEntitledError aus. Diese Abwaegung ist eine
    Plan-Entscheidung (nicht explizit von der Design-Spec vorgegeben) -- siehe Hinweis
    in den Global Constraints des Implementierungsplans.
    """
    try:
        response = requests.get(f"{base_url}/tenants/{tenant_id}/status", timeout=10)
        response.raise_for_status()
        body = response.json()
        if not body.get("active", True):
            raise TenantNotEntitledError(
                "Diese Anlage ist derzeit nicht aktiv (Abo abgelaufen/pausiert) - bitte Abo "
                "verlaengern und Add-on danach manuell neu starten."
            )
    except TenantNotEntitledError:
        raise
    except Exception as error:
        logger.warning(
            "Berechtigungspruefung bei accounts-api fehlgeschlagen (wird als "
            "berechtigt behandelt, um einen kurzen accounts-api-Ausfall nicht mit "
            "einem abgelaufenen Abo zu verwechseln): %s",
            error,
        )
        return


def _is_configured(options: dict) -> bool:
    """Ab 0.6.0 hat config.yaml keine Pflichtfelder mehr -- konfiguriert wird das
    Add-on nicht mehr ueber eine eigene UI, sondern ausschliesslich dadurch, dass die
    separate SmartHeat-Integration in Home Assistant die Werte per Supervisor-API in
    options.json schreibt. Ein frisch installiertes, noch nicht ueber die Integration
    konfiguriertes Add-on hat also ein leeres oder unvollstaendiges options.json; das
    ist ein normaler Zustand, kein Fehler.
    """
    return all(options.get(field) for field in _REQUIRED_OPTIONS)


def _resolve_effective_options(options: dict) -> dict:
    clamps = resolve_local_clamps(options["profile"])
    boost = resolve_boost_defaults(options["profile"])
    windows = resolve_window_defaults(options["profile"])
    return {
        **options,
        "curve_min": clamps.curve_min,
        "curve_max": clamps.curve_max,
        "offset_min": clamps.offset_min,
        "offset_max": clamps.offset_max,
        "boost_threshold_k": boost.threshold_k,
        "boost_curve_value": boost.curve_value,
        "boost_offset_value": boost.offset_value,
        "daily_trigger_time": windows.daily_trigger_time,
        "day_avg_window_start": windows.day_avg_window_start,
        "day_avg_window_end": windows.day_avg_window_end,
        "night_avg_window_start": windows.night_avg_window_start,
        "night_avg_window_end": windows.night_avg_window_end,
        "avg_window_hours": window_size_hours(windows.day_avg_window_start, windows.day_avg_window_end),
    }


def _validate_boost_config(options: dict) -> str | None:
    """Returns a German error message if the configured boost values fall outside
    the configured safety clamps, or None if the config is valid. A misconfigured
    boost value is a startup-time error, not something to silently clamp, since the
    boost path is the one write path that runs with no server oversight.
    """
    curve_min, curve_max = options["curve_min"], options["curve_max"]
    offset_min, offset_max = options["offset_min"], options["offset_max"]
    boost_curve_value = options["boost_curve_value"]
    boost_offset_value = options["boost_offset_value"]

    if not (curve_min <= boost_curve_value <= curve_max):
        return (
            f"boost_curve_value ({boost_curve_value}) liegt ausserhalb des konfigurierten "
            f"Bereichs [curve_min={curve_min}, curve_max={curve_max}]"
        )
    if not (offset_min <= boost_offset_value <= offset_max):
        return (
            f"boost_offset_value ({boost_offset_value}) liegt ausserhalb des konfigurierten "
            f"Bereichs [offset_min={offset_min}, offset_max={offset_max}]"
        )
    return None


def _validate_local_check_interval(options: dict) -> str | None:
    """Returns a German error message if local_check_interval_seconds is set but
    exceeds the maximum of 3600s, or None if absent/valid. The previous 60s hard cap
    (Design-Spec 2026-09-16, Abschnitt A.1) existed to bound SD-card wear from the then
    poll-driven boost/change-check; since Design-Spec 2026-09-21 that check only still
    runs on this cadence as a FALLBACK while HaTriggerClient's WS connection is down --
    the interval now bounds worst-case fallback staleness, not routine polling
    frequency, so a much larger ceiling is appropriate. 3600s (1h) keeps that worst case
    in the same order of magnitude as the fail-safe's own hour-scale staleness
    threshold (failsafe_stale_after_hours).
    """
    value = options.get("local_check_interval_seconds")
    if value is not None and (
        not isinstance(value, (int, float)) or math.isnan(value) or math.isinf(value)
    ):
        return (
            f"local_check_interval_seconds ({value!r}) ist kein gueltiger endlicher Zahlenwert"
        )
    if value is not None and value > 3600:
        return (
            f"local_check_interval_seconds ({value}) liegt ueber dem zulaessigen Maximum "
            f"von 3600 Sekunden (1h) - seit der Umstellung auf eventgetriebene Trigger "
            f"steuert dieser Wert nur noch den Watchdog-/Fallback-Takt, nicht mehr "
            f"routinemaessiges Polling"
        )
    return None


def _validate_telemetry_interval(options: dict) -> str | None:
    """Returns a German error message if telemetry_interval_seconds is set but falls
    below config.yaml's schema floor of 10s, or None if absent/valid. Guards against a
    manually edited options.json on the Pi bypassing that floor (Korrektheit-Review-
    Fund sh-1, same rationale as _validate_local_check_interval above): without this,
    the cadence gate in _maybe_publish_telemetry effectively never throttles, and every
    local check (every 30-60s) publishes telemetry instead of every 300s -- 5-10x more
    MQTT traffic with no startup error to surface the misconfiguration.
    """
    value = options.get("telemetry_interval_seconds")
    if value is not None and (
        not isinstance(value, (int, float)) or math.isnan(value) or math.isinf(value)
    ):
        return (
            f"telemetry_interval_seconds ({value!r}) ist kein gueltiger endlicher Zahlenwert"
        )
    if value is not None and value < 10:
        return (
            f"telemetry_interval_seconds ({value}) liegt unter dem zulaessigen Minimum "
            f"von 10 Sekunden"
        )
    return None


def _validate_failsafe_stale_after_hours(options: dict) -> str | None:
    """Returns a German error message if failsafe_stale_after_hours is set but is not
    a finite, positive number, or None if absent/valid. Same NaN/Infinity guard as
    _validate_local_check_interval/_validate_telemetry_interval above (whole-branch
    review finding I3, same rationale/pattern as those two): without it, a hand-edited
    options.json with NaN for this value silently and permanently disables dead-server
    detection (the fail-safe threshold itself), since `seconds_since >= NaN` is always
    False in `enter_failsafe_if_stale`/`_check_failsafe_staleness`, with no startup
    error to surface the misconfiguration. A non-positive value is rejected too --
    unambiguously nonsensical for this field regardless of the current default (0 or
    negative would mean "always stale" or "stale before any time has passed").
    """
    value = options.get("failsafe_stale_after_hours")
    if value is not None and (
        not isinstance(value, (int, float)) or math.isnan(value) or math.isinf(value)
    ):
        return (
            f"failsafe_stale_after_hours ({value!r}) ist kein gueltiger endlicher Zahlenwert"
        )
    if value is not None and value <= 0:
        return (
            f"failsafe_stale_after_hours ({value}) muss groesser als 0 sein"
        )
    return None


def _validate_derived_sensor_prerequisites(options: dict) -> str | None:
    """Returns a German error message if a field the automatic DAT/DART/day-night-avg
    provisioning needs (derived_sensors.ensure_all) is missing, or None if both are
    present. Checked explicitly, before ensure_all() runs, so a customer who forgot
    entity_outdoor_temp gets a clean startup error instead of a raw KeyError.
    """
    missing = [
        field for field in ("entity_room_actual", "entity_outdoor_temp")
        if not options.get(field)
    ]
    if missing:
        return (
            "Folgende Pflichtfelder fehlen in der Add-on-Konfiguration (werden fuer "
            f"automatisch berechnete Sensoren gebraucht): {', '.join(missing)}"
        )
    return None


def _ensure_derived_sensors_with_retry(ha_api, options: dict) -> dict[str, str]:
    """Wraps `derived_sensors.ensure_all` with retry-with-backoff (see
    DERIVED_SENSORS_RETRY_DELAYS_SECONDS above for the rationale) so a transient failure
    while HA Core is still starting up doesn't crash the whole add-on on the first try.
    """
    delays = DERIVED_SENSORS_RETRY_DELAYS_SECONDS
    last_error: Exception | None = None
    for attempt in range(len(delays) + 1):
        try:
            return derived_sensors.ensure_all(
                ha_api=ha_api,
                tenant_id=options["tenant_id"],
                room_actual_entity_id=options["entity_room_actual"],
                outdoor_temp_entity_id=options["entity_outdoor_temp"],
                avg_window_hours=options["avg_window_hours"],
                state_path=DERIVED_SENSORS_PATH,
            )
        except Exception as error:
            last_error = error
            if attempt == len(delays):
                break
            logger.warning(
                "Anlegen der abgeleiteten Sensoren fehlgeschlagen (Versuch %s/%s, evtl. ist "
                "HA Core beim Start des Add-ons noch nicht bereit): %s",
                attempt + 1, len(delays) + 1, error,
            )
            time.sleep(delays[attempt])
    raise last_error


def _connect_mqtt_with_retry(options: dict) -> BridgeMqttClient:
    """Wraps `BridgeMqttClient` constructor with retry-with-backoff (see
    MQTT_CONNECT_RETRY_DELAYS_SECONDS above for the rationale) so a transient failure
    during add-on startup (e.g. cloudflared_access_mqtt hasn't started yet) doesn't
    crash the whole add-on on the first try.
    """
    delays = MQTT_CONNECT_RETRY_DELAYS_SECONDS
    last_error: Exception | None = None
    for attempt in range(len(delays) + 1):
        try:
            return BridgeMqttClient(
                host=MQTT_HOST, port=MQTT_PORT, tenant_id=options["tenant_id"],
                username=options["mqtt_username"], password=options["mqtt_password"],
            )
        except Exception as error:
            last_error = error
            if attempt == len(delays):
                break
            logger.warning(
                "MQTT-Verbindungsaufbau fehlgeschlagen (Versuch %s/%s, evtl. ist "
                "cloudflared_access_mqtt noch nicht bereit): %s",
                attempt + 1, len(delays) + 1, error,
            )
            time.sleep(delays[attempt])
    raise last_error


def _make_down_callback(role, manifest, ha_api, options, write_lock, failsafe_ctx, mqtt_client):
    def _callback(client, userdata, message):
        try:
            payload = json.loads(message.payload)
            with write_lock:
                handle_down_message(
                    role=role,
                    value=payload["v"],
                    manifest=manifest,
                    ha_api=ha_api,
                    curve_min=options["curve_min"],
                    curve_max=options["curve_max"],
                    offset_min=options["offset_min"],
                    offset_max=options["offset_max"],
                    backup_path=BACKUP_PATH,
                )
                _record_valid_message(failsafe_ctx, mqtt_client, FAILSAFE_PATH)
        except Exception:
            logger.exception("Fehler bei der Verarbeitung einer Down-Nachricht fuer Rolle '%s'", role)
    return _callback


def _load_failsafe_ctx(path: Path) -> dict:
    raw = load_backup(path)
    return {
        "last_valid_update": raw.get("last_valid_update"),
        "state": FailsafeState(
            active=raw.get("failsafe_active", False),
            recovery_count=raw.get("recovery_count", 0),
        ),
    }


def _load_failsafe_ctx_safe(path: Path) -> dict:
    """Wraps `_load_failsafe_ctx` so a corrupt/truncated state file (e.g. after power
    loss on the Pi's SD card) cannot crash the whole add-on at startup -- every other
    `load_backup` call site in this codebase runs inside a caller-provided try/except
    (see bridge.py's handle_down_message/apply_boost_decision), this is that guard for
    the fail-safe state file. Falls back to the same default context a missing file
    would produce.
    """
    try:
        return _load_failsafe_ctx(path)
    except Exception as error:
        logger.warning(
            "Fail-Safe-Zustandsdatei konnte nicht gelesen werden (%s), starte mit Standardzustand: %s",
            path, error,
        )
        return {
            "last_valid_update": None,
            "state": FailsafeState(active=False, recovery_count=0),
        }


def _save_failsafe_ctx(ctx: dict, path: Path) -> None:
    save_backup(path, {
        "last_valid_update": ctx["last_valid_update"],
        "failsafe_active": ctx["state"].active,
        "recovery_count": ctx["state"].recovery_count,
    })


def _record_valid_message(failsafe_ctx: dict, mqtt_client, failsafe_path: Path) -> None:
    failsafe_ctx["last_valid_update"] = time.time()
    new_state = record_valid_message(failsafe_ctx["state"])
    if new_state != failsafe_ctx["state"]:
        mqtt_client.publish_status("failsafe", build_state_payload(new_state.active))
    failsafe_ctx["state"] = new_state
    _save_failsafe_ctx(failsafe_ctx, failsafe_path)


def _check_failsafe_staleness(
    failsafe_ctx: dict, stale_after_seconds: float, mqtt_client, failsafe_path: Path,
    ha_api, notify_service: str = "",
) -> None:
    last = failsafe_ctx["last_valid_update"]
    seconds_since = (time.time() - last) if last is not None else None
    new_state = enter_failsafe_if_stale(failsafe_ctx["state"], seconds_since, stale_after_seconds)
    if new_state != failsafe_ctx["state"]:
        mqtt_client.publish_status("failsafe", build_state_payload(new_state.active))
        if new_state.active:
            logger.warning(
                "Fail-Safe aktiviert - seit ueber %s Sekunden kein gueltiger Live-Wert empfangen.",
                stale_after_seconds,
            )
            # Seit der Boost-Neudefinition (Task 17) ist dies das EINZIGE verbleibende
            # Signal fuer eine tote/veraltete Serververbindung -- Push-Benachrichtigung
            # analog zu publish_snapshot()s Broken-Sensor-Nachricht (best effort, eine
            # fehlschlagende Notify-Aktion darf die Fail-Safe-Erkennung selbst nicht stoeren).
            if notify_service:
                try:
                    ha_api.send_notification(
                        notify_service,
                        f"Heizungsbruecke: Fail-Safe aktiviert - seit ueber "
                        f"{stale_after_seconds / 3600:.1f}h kein gueltiger Live-Wert vom "
                        f"Server empfangen. Bitte Serververbindung pruefen.",
                    )
                except Exception:
                    logger.warning("Push-Benachrichtigung fuer Fail-Safe-Alarm konnte nicht gesendet werden")
        failsafe_ctx["state"] = new_state
        _save_failsafe_ctx(failsafe_ctx, failsafe_path)


def _preview_failsafe_ctx(failsafe_ctx: dict, stale_after_seconds: float) -> dict:
    """Read-only preview of `failsafe_ctx` with `state.active` freshly evaluated
    against the current on-disk staleness -- used ONLY to give the very first,
    pre-`loop_start()` telemetry publish (see the priming `_run_local_check` call in
    `_run_bridge`) a freshly-evaluated `failsafe_active` value instead of the stale
    value that was on disk at process start.

    Deliberately does NOT call `_check_failsafe_staleness`: that function COMMITS a
    transition (MQTT publish, `failsafe_state.json` write, and a push notification).
    Calling it here, before `mqtt_client.loop_start()` has had a chance to redeliver
    the retained down-messages that prove the server is actually still alive, produced
    a real regression (Whole-Branch-Review-Fund I1, final-review-fixes-plan,
    2026-09-20): on an add-on restart ~26-30h after the last down-message (normal
    daily-snapshot cadence, only ~2h margin against the 26h threshold), it fired a
    false "Fail-Safe aktiviert" push notification to the customer's phone, then flipped
    back OFF milliseconds later once `loop_start()` delivered the retained messages.

    `enter_failsafe_if_stale` is a pure function (see failsafe.py) with no I/O and no
    side effects, so calling it here to compute a *preview* state -- without ever
    assigning the result back into the real `failsafe_ctx`, publishing it, persisting
    it, or notifying about it -- is safe. The real transition (and its side effects)
    is decided later, once `loop_start()` has run, by the main loop's own
    `_check_failsafe_staleness()` call.
    """
    last = failsafe_ctx["last_valid_update"]
    seconds_since = (time.time() - last) if last is not None else None
    previewed_state = enter_failsafe_if_stale(failsafe_ctx["state"], seconds_since, stale_after_seconds)
    return {**failsafe_ctx, "state": previewed_state}


def _save_boost_active_if_changed(boost_active: bool, path: Path) -> None:
    backup = load_backup(path)
    if backup.get("boost_active", False) != boost_active:
        backup["boost_active"] = boost_active
        save_backup(path, backup)


def _read_room_target_live(manifest, ha_api) -> float | None:
    """Reads room_target directly from Home Assistant, bypassing the stable-target
    cache (Design-Spec 2026-09-22) -- used only by the call sites that must see a
    fresh value themselves (boot-priming, the debounced room_target trigger's own
    callback, and the undebounced watchdog fallback), never for a plain cache read.
    Returns None if the role isn't mapped, mirroring _run_local_check's own manifest
    guard, so callers can (re-)seed the cache unconditionally without checking the
    role first.
    """
    if "room_target" not in manifest.entity_ids:
        return None
    return ha_api.get_state(manifest.entity_ids["room_target"])


def _run_local_check(
    manifest, ha_api, mqtt_client, options, write_lock, boost_was_active: bool,
    room_target: float | None, failsafe_ctx: dict | None = None,
) -> bool:
    """Runs one local check cycle (Design-Spec 2026-09-16, Abschnitt A): reads
    room_actual locally from Home Assistant (no server/MQTT contact), evaluates and
    applies the boost decision, and persists boost_active for
    bridge.py::handle_down_message to read (Abschnitt D). Does NOT publish a full
    snapshot itself -- see _maybe_publish_full_snapshot, called at the end of this
    function once Task 8 wires it in. Returns the boost-active state to carry into
    the next check. Propagates any I/O error to the caller (_run_bridge's loop),
    which is responsible for catching and logging so a single bad check doesn't kill
    the whole process.

    `room_target` is an explicit PARAMETER, not read live here (Design-Spec
    2026-09-22, Stable-Target-Cache): every caller resolves it themselves, either from
    a fresh live read (boot-priming, the room_target trigger's own callback, the
    watchdog fallback) or from the shared cache (every other trigger). `None` means
    the cache hasn't been populated yet (accepted boot-priming edge case, see
    _StableTargetBox) -- treated the same as the role being unmapped, a no-op.
    """
    if "room_actual" not in manifest.entity_ids or "room_target" not in manifest.entity_ids:
        return boost_was_active
    if room_target is None:
        return boost_was_active

    room_actual = ha_api.get_state(manifest.entity_ids["room_actual"])

    with write_lock:
        backup = load_backup(BACKUP_PATH)
        previous_room_target = backup.get("last_room_target")

        decision = decide_boost(
            room_actual=room_actual,
            room_target=room_target,
            previous_room_target=previous_room_target,
            boost_was_active=boost_was_active,
            arrival_threshold_k=options.get("boost_threshold_k", 0.5),
            boost_curve_value=options["boost_curve_value"],
            boost_offset_value=options["boost_offset_value"],
        )

        # I/O-Fix (Abschnitt A.2): nur schreiben, wenn sich der Wert tatsaechlich
        # geaendert hat -- bei local_check_interval_seconds=30 sonst bis zu 2.880
        # SD-Karten-Schreibvorgaenge/Tag statt vorher 24.
        if room_target != previous_room_target:
            backup["last_room_target"] = room_target
            save_backup(BACKUP_PATH, backup)

        boost_was_active = apply_boost_decision(
            decision=decision,
            boost_was_active=boost_was_active,
            manifest=manifest,
            ha_api=ha_api,
            curve_min=options["curve_min"],
            curve_max=options["curve_max"],
            offset_min=options["offset_min"],
            offset_max=options["offset_max"],
            backup_path=BACKUP_PATH,
        )
        _save_boost_active_if_changed(boost_was_active, BACKUP_PATH)

        _maybe_publish_full_snapshot(
            manifest=manifest, ha_api=ha_api, mqtt_client=mqtt_client, options=options,
            room_target=room_target, notify_service=options.get("notify_service", ""), now=datetime.now(),
        )

    return boost_was_active


def _run_telemetry_tick(
    manifest, ha_api, mqtt_client, options: dict, boost_active: bool, failsafe_active: bool,
) -> None:
    """Publishes the KPI telemetry snapshot on its own cadence, independent of whether
    `_run_local_check` ran this tick (Design-Spec 2026-09-21: telemetry stays a
    periodic, non-eventified watchdog-loop concern -- see spec's Watchdog-Loop
    section -- now that `_run_local_check` itself only runs on trigger events or the
    disconnected-fallback, not on every watchdog tick). Reads room_actual itself: a
    plain local HA REST call, not a cloud roundtrip, so doing it unconditionally here
    is cheap. `_maybe_publish_telemetry` still self-throttles via its own interval
    marker, so most calls to this function are no-ops.
    """
    if "room_actual" not in manifest.entity_ids:
        return
    try:
        room_actual = ha_api.get_state(manifest.entity_ids["room_actual"])
        _maybe_publish_telemetry(
            mqtt_client=mqtt_client, options=options, room_actual=room_actual,
            boost_active=boost_active, failsafe_active=failsafe_active, now=time.time(),
        )
    except Exception:
        logger.exception(
            "Fehler beim Veroeffentlichen der KPI-Telemetrie, wird beim naechsten Tick erneut versucht"
        )


def _maybe_publish_full_snapshot(
    manifest, ha_api, mqtt_client, options: dict, room_target: float, notify_service: str, now: datetime,
) -> None:
    """Triggers a full snapshot publish (curve.py recompute server-side) when target_rt
    has changed since the last publish, or the profile's daily_trigger_time has been
    reached for the first time today -- Design-Spec 2026-09-16, Abschnitt A.3/B. Called
    from inside _run_local_check's write_lock block, after the boost decision, so a
    triggered snapshot always carries the just-updated boost_active state (Abschnitt D).

    Unlike daynight_snapshot.maybe_snapshot's boundary-crossing-since-last-check
    algorithm, a simple "time of day already past, not yet fired today" check is
    correct here for a different reason since Design-Spec 2026-09-21: while
    HaTriggerClient is connected, its own `time`-platform trigger fires this path
    exactly once at daily_trigger_time, regardless of local_check_interval_seconds.
    While disconnected, the watchdog loop's fallback still calls this at least every
    local_check_interval_seconds (now up to 3600s, see _validate_local_check_interval),
    so the daily boundary is still caught within one fallback tick even in that case.
    """
    backup = load_backup(BACKUP_PATH)
    today = now.date().isoformat()

    daily_trigger_time = options.get("daily_trigger_time")
    daily_due = False
    if daily_trigger_time:
        trigger_time = datetime.strptime(daily_trigger_time, "%H:%M").time()
        daily_due = now.time() >= trigger_time and backup.get("last_daily_trigger_date") != today

    target_changed = room_target != backup.get("last_published_target_rt")

    if not (daily_due or target_changed):
        return

    seq = str(uuid.uuid4())
    publish_snapshot(
        manifest=manifest, ha_api=ha_api, mqtt_client=mqtt_client, seq=seq, notify_service=notify_service,
    )
    logger.info(
        "Voller Snapshot veroeffentlicht (seq=%s, Grund=%s)",
        seq, "taeglicher Zeitpunkt" if daily_due else "target_rt geaendert",
    )

    backup["last_published_target_rt"] = room_target
    if daily_due:
        backup["last_daily_trigger_date"] = today
    save_backup(BACKUP_PATH, backup)


def _maybe_publish_telemetry(
    mqtt_client, options: dict, room_actual: float, boost_active: bool,
    failsafe_active: bool, now: float,
) -> None:
    """Publishes the KPI telemetry snapshot (Design-Spec 2026-09-16 KPI-Erfassung,
    Abschnitt 1) on its own interval (telemetry_interval_seconds, default 300s) --
    decoupled from local_check_interval_seconds (would flood the server with a write
    every 30-60s again) and from the now-daily/event-driven full snapshot (would make
    comfort/boost KPIs too coarse-grained). Not retained, QoS 1 only (see
    BridgeMqttClient.publish_telemetry): this topic feeds only observational KPI
    reporting and has no influence on curve.py or any control decision. The cadence
    marker is in-memory only (see `_last_telemetry_publish_ts`), not persisted.
    """
    global _last_telemetry_publish_ts
    interval = options.get("telemetry_interval_seconds", DEFAULT_TELEMETRY_INTERVAL_SECONDS)
    if _last_telemetry_publish_ts is not None and (now - _last_telemetry_publish_ts) < interval:
        return

    mqtt_client.publish_telemetry({
        "room_actual": room_actual,
        "boost_active": boost_active,
        "failsafe_active": failsafe_active,
        "ts": datetime.now().isoformat(),
    })

    _last_telemetry_publish_ts = now


class _BoostStateBox:
    """Mutable box for the shared `boost_was_active` flag, so the main watchdog-loop
    thread and HaTriggerClient's callback thread can read-modify-write it while both
    hold the same (now reentrant) `write_lock`, instead of each tracking its own local
    copy -- which raced once more than one thread could call `_run_local_check`
    (Design-Spec 2026-09-21, Risiko 2). `_run_local_check` itself is unchanged; only
    the call sites coordinate through this box.
    """
    def __init__(self, active: bool) -> None:
        self.active = active


class _StableTargetBox:
    """Mutable box for the in-memory `room_target` stable-value cache (Design-Spec
    2026-09-22, "Stable-Target-Cache"). Shared, like `_BoostStateBox` above, between
    the main watchdog-loop thread and HaTriggerClient's callback thread under the same
    `write_lock`. Holds the last room_target value confirmed stable for
    ROOM_TARGET_DEBOUNCE_SECONDS -- refreshed only when the debounced room_target
    trigger itself fires (_make_trigger_event_callback), or by the deliberately
    undebounced watchdog fallback/boot-priming live reads. Every other
    _run_local_check call (room_actual trigger, daily_trigger_time trigger) reuses
    this cached value instead of re-reading room_target live, closing the gap where a
    stray event during the 10s stabilization window could otherwise still act on a
    non-final value for both boost start/end and the curve snapshot.
    """
    def __init__(self, value: float | None) -> None:
        self.value = value


def _strip_attribute_suffix(entity_id: str) -> str:
    """Strips the `::attribute` suffix `ha_api.get_state()` uses for climate-attribute
    roles (e.g. `climate.wohnzimmer::temperature`) -- a `subscribe_trigger` state
    trigger's `entity_id` filter needs the real HA entity_id, not this add-on-internal
    convention.
    """
    real_entity_id, _, _ = entity_id.partition("::")
    return real_entity_id


def _extract_attribute_suffix(entity_id: str) -> str | None:
    """Gegenstueck zu `_strip_attribute_suffix`: liefert den `::attribute`-Suffix (z.B.
    `climate.wohnzimmer::temperature` -> `"temperature"`), oder `None` wenn die
    Entity-ID keinen Suffix hat. Wird gebraucht, um den `attribute`-Filter des
    `room_target`-subscribe_trigger-Triggers aufzubauen (Design-Spec 2026-09-22,
    Abschnitt 1).
    """
    _, _, attribute = entity_id.partition("::")
    return attribute or None


def _make_trigger_event_callback(
    manifest, ha_api, mqtt_client, options, write_lock, boost_state, failsafe_ctx, stable_target,
):
    room_target_entity_id = None
    room_target_attribute = None
    if "room_target" in manifest.entity_ids:
        room_target_entity_id = _strip_attribute_suffix(manifest.entity_ids["room_target"])
        room_target_attribute = _extract_attribute_suffix(manifest.entity_ids["room_target"])

    def _on_trigger_event(trigger: dict) -> None:
        try:
            with write_lock:
                if (
                    room_target_entity_id is not None
                    and trigger.get("platform") == "state"
                    and trigger.get("entity_id") == room_target_entity_id
                    and trigger.get("attribute") == room_target_attribute
                ):
                    # Der (debounced) room_target-Trigger selbst ist gefeuert -- der
                    # Wert ist jetzt garantiert seit ROOM_TARGET_DEBOUNCE_SECONDS
                    # stabil (Design-Spec 2026-09-22, Abschnitt 2). Frisch lesen
                    # (nicht aus trigger["to_state"] uebernehmen -- einfacher/robuster
                    # gegen Payload-Formvarianten) und den Cache aktualisieren, den
                    # jeder andere Trigger unten mitbenutzt. Matching ueber entity_id
                    # UND attribute (nicht nur entity_id): room_actual und room_target
                    # koennen dieselbe physische Entity referenzieren (z.B. ein
                    # einzelnes Thermostat mit current_temperature/temperature), nur
                    # das attribute-Feld unterscheidet dann, welcher der beiden
                    # Trigger tatsaechlich gefeuert hat.
                    stable_target.value = _read_room_target_live(manifest, ha_api)
                boost_state.active = _run_local_check(
                    manifest, ha_api, mqtt_client, options, write_lock, boost_state.active,
                    room_target=stable_target.value, failsafe_ctx=failsafe_ctx,
                )
        except Exception:
            logger.exception(
                "Fehler im lokalen Check (ausgeloest durch Trigger-Event, platform=%s), wird beim "
                "naechsten Trigger/Fallback erneut versucht", trigger.get("platform"),
            )
    return _on_trigger_event


def _build_ha_trigger_client(
    manifest, ha_api, options: dict, mqtt_client, write_lock, boost_state, failsafe_ctx, stable_target,
):
    triggers = []
    if "room_target" in manifest.entity_ids:
        room_target_raw = manifest.entity_ids["room_target"]
        room_target_trigger = {
            "platform": "state",
            "entity_id": _strip_attribute_suffix(room_target_raw),
            "for": {"seconds": ROOM_TARGET_DEBOUNCE_SECONDS},
        }
        room_target_attribute = _extract_attribute_suffix(room_target_raw)
        if room_target_attribute:
            room_target_trigger["attribute"] = room_target_attribute
        triggers.append(room_target_trigger)
    if "room_actual" in manifest.entity_ids:
        triggers.append({
            "platform": "state", "entity_id": _strip_attribute_suffix(manifest.entity_ids["room_actual"]),
        })
    daily_trigger_time = options.get("daily_trigger_time")
    if daily_trigger_time:
        triggers.append({"platform": "time", "at": daily_trigger_time})

    on_trigger_event = _make_trigger_event_callback(
        manifest=manifest, ha_api=ha_api, mqtt_client=mqtt_client, options=options,
        write_lock=write_lock, boost_state=boost_state, failsafe_ctx=failsafe_ctx,
        stable_target=stable_target,
    )
    return HaTriggerClient(
        ws_url=ha_api.websocket_url(), token=ha_api.token, triggers=triggers, on_trigger_event=on_trigger_event,
    )


def _run_bridge(options: dict, ha_api) -> bool:
    """Laeuft synchron im Hauptthread (siehe main()); validiert/loest Optionen selbst
    auf und startet die Poll-Loop nur, wenn die SmartHeat-Integration das Add-on schon
    (per Supervisor-API in options.json) konfiguriert hat. Ein noch nicht konfiguriertes
    Add-on ist ab 0.6.0 ein normaler Zustand (siehe _is_configured), deshalb gibt genau
    dieser Fall `True` zurueck (main() beendet den Prozess dann mit Exit 0). Jeder andere
    fruehe Return ist ein echter Validierungs-/Startfehler und gibt `False` zurueck, damit
    main() mit einem Fehlercode abbricht und der Supervisor den Absturz sieht, statt ihn
    mit "noch nicht konfiguriert" zu verwechseln.
    """
    if not _is_configured(options):
        logger.info(
            "Add-on ist noch nicht eingerichtet -- bitte die SmartHeat-Integration in "
            "Home Assistant installieren und dort die Verbindung zu diesem Add-on "
            "einrichten (sie schreibt die Konfiguration automatisch per Supervisor-API). "
            "Die Poll-Loop startet erst, sobald options.json vollstaendig ist, und "
            "danach automatisch beim naechsten Neustart des Add-ons."
        )
        return True

    try:
        _check_entitlement(options["tenant_id"])
    except TenantNotEntitledError as error:
        logger.error("FEHLER: %s", error)
        return False

    try:
        options = _resolve_effective_options(options)
    except UnknownProfileError as error:
        logger.error("FEHLER: %s", error)
        return False

    boost_config_error = _validate_boost_config(options)
    if boost_config_error:
        logger.error("FEHLER: %s", boost_config_error)
        return False

    local_check_interval_error = _validate_local_check_interval(options)
    if local_check_interval_error:
        logger.error("FEHLER: %s", local_check_interval_error)
        return False

    telemetry_interval_error = _validate_telemetry_interval(options)
    if telemetry_interval_error:
        logger.error("FEHLER: %s", telemetry_interval_error)
        return False

    failsafe_stale_after_hours_error = _validate_failsafe_stale_after_hours(options)
    if failsafe_stale_after_hours_error:
        logger.error("FEHLER: %s", failsafe_stale_after_hours_error)
        return False

    prerequisite_error = _validate_derived_sensor_prerequisites(options)
    if prerequisite_error:
        logger.error("FEHLER: %s", prerequisite_error)
        return False

    try:
        derived_entity_ids = _ensure_derived_sensors_with_retry(ha_api, options)
    except Exception as error:
        logger.error(
            "FEHLER: Anlegen der abgeleiteten Sensoren fehlgeschlagen nach %d Versuchen: %s",
            len(DERIVED_SENSORS_RETRY_DELAYS_SECONDS) + 1, error,
        )
        return False

    try:
        manifest = build_manifest(options, derived_entity_ids)
    except ManifestError as error:
        logger.error("FEHLER: %s", error)
        return False

    write_lock = threading.RLock()

    failsafe_ctx = _load_failsafe_ctx_safe(FAILSAFE_PATH)
    if failsafe_ctx["last_valid_update"] is None:
        # Fresh install / no prior record: measure staleness from process start, so a
        # server that never sends a single valid value still trips fail-safe eventually
        # instead of reading "OK" forever.
        failsafe_ctx["last_valid_update"] = time.time()
    stale_after_seconds = options.get("failsafe_stale_after_hours", DEFAULT_FAILSAFE_STALE_AFTER_HOURS) * 3600

    try:
        mqtt_client = _connect_mqtt_with_retry(options)
        mqtt_client.publish_discovery(
            component="binary_sensor", object_id="failsafe",
            config=build_discovery_config(options["tenant_id"]),
        )
        mqtt_client.publish_status("failsafe", build_state_payload(failsafe_ctx["state"].active))

        for role in ("curve_current", "offset_current"):
            if role in manifest.entity_ids:
                mqtt_client.subscribe_down(
                    role=role,
                    on_message=_make_down_callback(role, manifest, ha_api, options, write_lock, failsafe_ctx, mqtt_client),
                )
    except ConnectionRefusedError as error:
        logger.error(
            "FEHLER: MQTT-Verbindung zum Broker fehlgeschlagen (Connection refused): %s. "
            "Pruefen, ob das Add-on 'cloudflared_access_mqtt' laeuft und auf demselben "
            "Port (%s) lauscht wie hier konfiguriert (MQTT_PORT in __main__.py).",
            error, MQTT_PORT,
        )
        return False
    except Exception as error:
        logger.error("FEHLER: MQTT-Verbindung zum Broker fehlgeschlagen: %s", error)
        return False

    # Boost-Active-Bootstrap-Fix (Sicherheits-Review-Fund, siehe SDD-Ledger dieses Plans):
    # mindestens ein lokaler Check MUSS abgeschlossen sein, bevor MQTT-Down-Nachrichten
    # verarbeitet werden koennen -- sonst kann backup.json["boost_active"] nach einem
    # Neustart waehrend eines aktiven Boosts fuer ein kurzes Fenster veraltet sein (siehe
    # handle_down_message, Design-Spec Abschnitt D), und eine in diesem Fenster eintreffende
    # Down-Nachricht wuerde gegen einen Wert behandelt, der seit dem Neustart nie frisch aus
    # echten Sensor-Werten neu bestimmt wurde. subscribe_down() liefert allein noch keine
    # Nachrichten aus -- erst loop_start() startet die Hintergrund-Verarbeitung -- daher
    # genuegt es, den allerersten _run_local_check()-Aufruf synchron VOR loop_start()
    # abzuschliessen, statt einen "sichereren" statischen Default zu waehlen.
    # sh-2-Folgefund (Task 3b, final-review-fixes-plan): _run_local_check liest
    # failsafe_active aus failsafe_ctx["state"] (siehe dessen Docstring), aber nur der
    # Hauptloop-Aufruf unten liess _check_failsafe_staleness vorher laufen (Task 11 des
    # Vorgaenger-Plans). Ohne eine Vorab-Bewertung hier published der allererste,
    # synchrone Telemetrie-Call (Kaltstart, in-memory-Marker daher zurueckgesetzt,
    # siehe DOCS.md 0.10.2-Note) den beim Laden von FAILSAFE_PATH gesetzten Startwert
    # statt eines frisch evaluierten.
    #
    # I1-Fix (Whole-Branch-Review-Fund, final-review-fixes-plan, 2026-09-20): hierfuer
    # NICHT _check_failsafe_staleness() aufrufen -- das committet eine echte
    # Zustandsaenderung (MQTT-Publish, failsafe_state.json-Schreibvorgang, Push-
    # Benachrichtigung), noch bevor loop_start() unten die retained Down-Nachrichten
    # zugestellt hat, die belegen wuerden, dass der Server tatsaechlich noch lebt. Das
    # fuehrte bei einem Neustart ~26-30h nach der letzten Down-Nachricht (normale
    # taegliche Snapshot-Kadenz, nur ~2h Puffer gegen die 26h-Schwelle) zu einer
    # falschen "Fail-Safe aktiviert"-Push-Benachrichtigung, die Millisekunden spaeter
    # durch loop_start() wieder zurueckgenommen wurde. Stattdessen rein lesend eine
    # Vorschau bilden (_preview_failsafe_ctx, reine Funktion, keine Nebenwirkungen) und
    # nur fuer DIESEN einen priming-Aufruf verwenden -- der echte failsafe_ctx bleibt
    # unveraendert, die echte Zustandsaenderung entscheidet weiterhin ausschliesslich
    # der Hauptloop-Aufruf unten, nach loop_start().
    priming_failsafe_ctx = _preview_failsafe_ctx(failsafe_ctx, stale_after_seconds)

    boost_state = _BoostStateBox(active=False)
    # Stable-Target-Cache-Bootstrap (Design-Spec 2026-09-22): ein einmaliger Live-Read
    # vor loop_start() initialisiert den Cache, analog zum Boost-Active-Bootstrap-Fix
    # oben. Bewusst INNERHALB desselben try/except wie der priming _run_local_check()-
    # Aufruf -- ein HA-API-Hickup beim Booten soll boost_active gleich behandeln,
    # unabhaengig davon, ob es beim room_target-Read oder erst im lokalen Check selbst
    # auftritt (gleiche Fail-Open-Begruendung wie dort).
    stable_target = _StableTargetBox(value=None)
    try:
        stable_target.value = _read_room_target_live(manifest, ha_api)
        boost_state.active = _run_local_check(
            manifest, ha_api, mqtt_client, options, write_lock, boost_state.active,
            room_target=stable_target.value, failsafe_ctx=priming_failsafe_ctx,
        )
    except Exception:
        logger.exception(
            "Fehler beim initialen lokalen Check vor MQTT-Start, wird im regulaeren Loop erneut versucht"
        )
        # Fail-open (whole-branch review finding): backup.json["boost_active"] must not
        # be left at a stale pre-restart value here -- that defeats the boot-sync fix in
        # exactly the failure case it needs to handle (a transient HA-API hiccup at boot).
        # A missed live-write during a genuine boost costs a little comfort for one cycle;
        # a stuck stale-True value can gate out a down-message and leave the live device
        # pinned at a boost value for up to a day under the new publish cadence.
        _save_boost_active_if_changed(False, BACKUP_PATH)
        boost_state.active = False

    try:
        mqtt_client.loop_start()
    except Exception as error:
        logger.error("FEHLER: MQTT-Verbindung zum Broker fehlgeschlagen: %s", error)
        return False

    ha_trigger_client = _build_ha_trigger_client(
        manifest=manifest, ha_api=ha_api, options=options, mqtt_client=mqtt_client,
        write_lock=write_lock, boost_state=boost_state, failsafe_ctx=failsafe_ctx,
        stable_target=stable_target,
    )
    ha_trigger_client.start()

    while True:
        try:
            with write_lock:
                _check_failsafe_staleness(
                    failsafe_ctx, stale_after_seconds, mqtt_client, FAILSAFE_PATH,
                    ha_api=ha_api, notify_service=options.get("notify_service", ""),
                )
        except Exception:
            logger.exception("Fehler bei der Fail-Safe-Staleness-Pruefung, wird beim naechsten Check erneut versucht")

        try:
            if not ha_trigger_client.connected:
                with write_lock:
                    # Undebounced mit Absicht (Design-Spec 2026-09-22, Punkt 2): hier
                    # existiert keine subscribe_trigger-Debounce-Garantie, konsistent
                    # mit "degradiert automatisch auf reinen Poll-Betrieb". Aktualisiert
                    # den Cache trotzdem nach dem eigenen Live-Read, damit nach einem
                    # WS-Reconnect kein veralteter Vor-Ausfall-Wert uebrig bleibt.
                    stable_target.value = _read_room_target_live(manifest, ha_api)
                    boost_state.active = _run_local_check(
                        manifest, ha_api, mqtt_client, options, write_lock, boost_state.active,
                        room_target=stable_target.value, failsafe_ctx=failsafe_ctx,
                    )
        except Exception:
            logger.exception("Fehler im lokalen Check (Watchdog-Fallback), wird beim naechsten Tick erneut versucht")

        _run_telemetry_tick(
            manifest, ha_api, mqtt_client, options,
            boost_active=boost_state.active, failsafe_active=failsafe_ctx["state"].active,
        )

        try:
            daynight_snapshot.maybe_snapshot(
                ha_api=ha_api,
                room_12h_avg_entity_id=derived_entity_ids["_room_12h_avg"],
                day_avg_entity_id=derived_entity_ids["room_day_avg"],
                night_avg_entity_id=derived_entity_ids["room_night_avg"],
                day_avg_window_end=options["day_avg_window_end"],
                night_avg_window_end=options["night_avg_window_end"],
                state_path=DAYNIGHT_SNAPSHOT_PATH,
                now=datetime.now(),
            )
        except Exception:
            logger.exception("Fehler beim Tag-/Nachtmittel-Snapshot, wird beim naechsten Check erneut versucht")

        time.sleep(options.get("local_check_interval_seconds", DEFAULT_LOCAL_CHECK_INTERVAL_SECONDS))


def _load_options_safe(path: Path) -> dict:
    """Loads options.json, defaulting to `{}` both when the file is missing (fresh
    install, not yet configured -- see _is_configured) and when it is present but
    corrupt/truncated (e.g. after power loss on the Pi's SD card, the same failure
    mode _load_failsafe_ctx_safe already guards against). A bad options file must
    not crash the process before _run_bridge can log a clean startup error.
    """
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as error:
        logger.warning(
            "options.json konnte nicht gelesen werden (%s), starte mit leerer Konfiguration: %s",
            path, error,
        )
        return {}


def main() -> None:
    logging.basicConfig(level=logging.INFO)

    options = _load_options_safe(OPTIONS_PATH)
    ha_api = HomeAssistantApi(base_url="http://supervisor", token=os.environ["SUPERVISOR_TOKEN"])

    if not _run_bridge(options, ha_api):
        sys.exit(1)


if __name__ == "__main__":
    main()
