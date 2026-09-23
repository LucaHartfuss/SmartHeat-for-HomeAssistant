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
from heizungsbruecke.boost import BoostDecision, decide_boost
from heizungsbruecke.bridge import apply_boost_decision, apply_emergency_decision, handle_down_message, publish_snapshot
from heizungsbruecke.emergency_boost import EmergencyBoostDecision, decide_emergency_boost
from heizungsbruecke import daynight_snapshot, derived_sensors
from heizungsbruecke.failsafe import (
    FailsafeState,
    build_discovery_config,
    build_state_payload,
    enter_notbetrieb_on_ack_timeout,
    exit_notbetrieb_on_ack,
    register_publish_attempt,
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

# Design-Spec 2026-09-21: seit der Umstellung auf HaTriggerClient steuert dieser Wert
# nur noch den Watchdog-/Fallback-Takt (Boost-/target_changed-Check nur, wenn die
# WS-Verbindung down ist), nicht mehr routinemaessiges Polling -- 300s deckt sich mit
# telemetry_interval_seconds' bestehendem Default.
DEFAULT_LOCAL_CHECK_INTERVAL_SECONDS = 300

# Design-Spec 2026-09-16 (KPI-Erfassung), Abschnitt 1: eigenes, von
# local_check_interval_seconds UND vom (jetzt seltenen) vollen Snapshot-Publish
# entkoppeltes Intervall, damit Komfort-/Boost-KPIs nicht zu grobkoernig werden.
DEFAULT_TELEMETRY_INTERVAL_SECONDS = 300

# Design-Spec 2026-09-23: wie lange nach einem vollen Snapshot-Publish auf eine
# seq-passende Down-Antwort gewartet wird, bevor Notbetrieb ausgeloest wird. Normale
# Serververarbeitung laeuft synchron im on_message-Handler des Servers (Sekundenbereich)
# -- 30s deckt ueblichen Broker-/Tunnel-Jitter ab, ohne einen echten Ausfall lange zu
# verschleppen. Nutzer-bestaetigter Wert, siehe Design-Spec, Abschnitt 1.
ACK_TIMEOUT_SECONDS = 30

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
    frequency, so a much larger ceiling is appropriate. 3600s (1h) is an independent
    outer bound on that fallback staleness, unrelated to the fail-safe's own detection
    speed (an ack-timeout on the next full snapshot publish, seconds-scale -- see
    Design-Spec 2026-09-23 -- not tied to this interval at all).
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
            if message.retain:
                # paho-mqtt sets .retain only on the broker's initial post-(re)subscribe
                # replay of the last retained value, never on a genuine live publish --
                # see docs/superpowers/specs/2026-09-22-heizungsbruecke-retained-down-replay-fix-design.md.
                # Assumes MQTT 3.1.1 semantics (this client's default): under MQTT v5 with
                # the Retain-As-Published subscribe option, a genuine live publish could also
                # arrive with retain=1 and would be wrongly skipped here -- including its
                # ack, so a real server answer could no longer end Notbetrieb. Do not enable
                # RAP for this subscription without revisiting this check.
                logger.info(
                    "Retained Down-Nachricht fuer Rolle '%s' beim (Re-)Subscribe uebersprungen "
                    "(Broker-Replay, kein frisches Server-Signal)",
                    role,
                )
                return
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
                was_active = failsafe_ctx["state"].active
                _handle_ack(
                    failsafe_ctx=failsafe_ctx, mqtt_client=mqtt_client, failsafe_path=FAILSAFE_PATH,
                    acked_seq=payload.get("seq"), ha_api=ha_api,
                    notify_service=options.get("notify_service", ""),
                )
                if was_active and not failsafe_ctx["state"].active:
                    _end_emergency_boost_if_active(failsafe_ctx, manifest, ha_api, options)
        except Exception:
            logger.exception("Fehler bei der Verarbeitung einer Down-Nachricht fuer Rolle '%s'", role)
    return _callback


def _load_failsafe_ctx(path: Path, backup_path: Path) -> dict:
    """Stellt den Fail-Safe-Kontext nach einem Neustart wieder her (Design-Spec
    2026-09-23, Abschnitt 4): `state.active` aus `path` (failsafe_state.json) und
    `emergency_boost_active` aus `backup_path` (backup.json, dort von
    `_save_emergency_active_if_changed` gepflegt). Beide MUESSEN den Neustart
    ueberleben: das Boot-Priming-`_run_local_check` braucht den echten Vorher-Wert von
    `emergency_boost_active`, um eine noch laufende Notfall-Exkursion korrekt
    fortzusetzen (Hysterese aus dem "war aktiv"-Zweig) bzw. -- falls Notbetrieb vor dem
    Neustart schon beendet war -- das Live-Geraet von den Maximalwerten zurueckzusetzen.
    Mit einem hart auf False gesetzten Startwert blieb das Geraet in beiden Faellen auf
    curve_max/offset_max haengen (Final-Review-Fund C1). Nur `awaiting_seq` startet
    frisch (siehe _save_failsafe_ctx).
    """
    raw = load_backup(path)
    backup = load_backup(backup_path)
    return {
        "state": FailsafeState(active=raw.get("failsafe_active", False), awaiting_seq=None),
        "emergency_boost_active": backup.get("emergency_boost_active", False),
    }


def _load_failsafe_ctx_safe(path: Path, backup_path: Path) -> dict:
    """Wraps `_load_failsafe_ctx` so a corrupt/truncated state file (e.g. after power
    loss on the Pi's SD card) cannot crash the whole add-on at startup -- every other
    `load_backup` call site in this codebase runs inside a caller-provided try/except
    (see bridge.py's handle_down_message/apply_boost_decision), this is that guard for
    the fail-safe state. Falls back to the same default a missing file would produce,
    but per file: a corrupt failsafe_state.json must not also discard a still-valid
    persisted `emergency_boost_active=True` from backup.json -- boot-priming (which then
    sees Notbetrieb inactive) relies on that flag to restore the live device from its
    max-heat values.
    """
    try:
        return _load_failsafe_ctx(path, backup_path)
    except Exception as error:
        logger.warning(
            "Fail-Safe-Zustand konnte nicht vollstaendig gelesen werden (%s, %s), starte "
            "mit Standardzustand fuer die nicht lesbare Datei: %s",
            path, backup_path, error,
        )
    try:
        failsafe_active = load_backup(path).get("failsafe_active", False)
    except Exception:
        failsafe_active = False
    try:
        emergency_boost_active = load_backup(backup_path).get("emergency_boost_active", False)
    except Exception:
        emergency_boost_active = False
    return {
        "state": FailsafeState(active=failsafe_active, awaiting_seq=None),
        "emergency_boost_active": emergency_boost_active,
    }


def _save_failsafe_ctx(ctx: dict, path: Path) -> None:
    """Persistiert `state.active` nach failsafe_state.json. `emergency_boost_active` wird
    separat in backup.json persistiert (`_save_emergency_active_if_changed`, dort liest
    auch handle_down_message's Live-Write-Sperre mit) und von `_load_failsafe_ctx` beim
    Boot wieder eingelesen -- beide ueberleben einen Neustart (Design-Spec 2026-09-23,
    Abschnitt 4). Einzig `awaiting_seq` wird bewusst NICHT persistiert: es bezieht sich
    auf einen in-Prozess-`threading.Timer`, der nach einem Neustart nicht mehr existiert
    und den Ack-/Timeout-Vergleich daher nie mehr aufloesen koennte.
    """
    save_backup(path, {"failsafe_active": ctx["state"].active})


def _save_emergency_active_if_changed(emergency_boost_active: bool, path: Path) -> None:
    backup = load_backup(path)
    if backup.get("emergency_boost_active", False) != emergency_boost_active:
        backup["emergency_boost_active"] = emergency_boost_active
        save_backup(path, backup)


def _handle_ack(
    failsafe_ctx: dict, mqtt_client, failsafe_path: Path, acked_seq: str | None,
    ha_api, notify_service: str = "",
) -> None:
    """Verarbeitet eine eingehende Down-Nachricht als moeglichen Ack fuer den zuletzt
    erwarteten Up-Snapshot-Publish. Beendet Notbetrieb, wenn `acked_seq` zum aktuell
    erwarteten `awaiting_seq` passt -- ein einzelner passender Ack genuegt (kein
    Anti-Flap-Zaehler mehr, siehe Design-Spec 2026-09-23, Abschnitt 1).
    """
    if acked_seq is None:
        return
    previous_state = failsafe_ctx["state"]
    new_state = exit_notbetrieb_on_ack(previous_state, acked_seq)
    failsafe_ctx["state"] = new_state
    if new_state.active == previous_state.active:
        return
    mqtt_client.publish_status("failsafe", build_state_payload(new_state.active))
    _save_failsafe_ctx(failsafe_ctx, failsafe_path)
    logger.warning("Notbetrieb beendet - Server hat Up-Snapshot (seq=%s) beantwortet.", acked_seq)
    if notify_service:
        try:
            ha_api.send_notification(
                notify_service,
                "Heizungsbruecke: Notbetrieb beendet, Serververbindung wiederhergestellt.",
            )
        except Exception:
            logger.warning("Push-Benachrichtigung fuer Notbetrieb-Ende konnte nicht gesendet werden")


def _handle_ack_timeout(
    seq: str, failsafe_ctx: dict, mqtt_client, failsafe_path: Path, write_lock,
    ha_api, notify_service: str,
) -> None:
    with write_lock:
        new_state = enter_notbetrieb_on_ack_timeout(failsafe_ctx["state"], seq)
        if new_state == failsafe_ctx["state"]:
            return
        failsafe_ctx["state"] = new_state
        mqtt_client.publish_status("failsafe", build_state_payload(new_state.active))
        _save_failsafe_ctx(failsafe_ctx, failsafe_path)
        logger.warning(
            "Notbetrieb aktiviert - keine Antwort vom Server auf Up-Snapshot (seq=%s) "
            "innerhalb von %s Sekunden.", seq, ACK_TIMEOUT_SECONDS,
        )
        if notify_service:
            try:
                ha_api.send_notification(
                    notify_service,
                    "Heizungsbruecke: Server antwortet nicht auf Up-Snapshot, Notbetrieb "
                    "aktiviert. Bitte Serververbindung pruefen.",
                )
            except Exception:
                logger.warning("Push-Benachrichtigung fuer Notbetrieb-Alarm konnte nicht gesendet werden")


def _schedule_ack_timeout(
    seq: str, failsafe_ctx: dict, mqtt_client, failsafe_path: Path, write_lock,
    ha_api, notify_service: str,
) -> threading.Timer:
    timer = threading.Timer(
        ACK_TIMEOUT_SECONDS, _handle_ack_timeout,
        kwargs=dict(
            seq=seq, failsafe_ctx=failsafe_ctx, mqtt_client=mqtt_client,
            failsafe_path=failsafe_path, write_lock=write_lock,
            ha_api=ha_api, notify_service=notify_service,
        ),
    )
    timer.daemon = True
    timer.start()
    return timer


def _end_emergency_boost_if_active(failsafe_ctx: dict, manifest, ha_api, options: dict) -> None:
    """Beendet eine laufende Notfall-Boost-Exkursion sofort, wenn Notbetrieb selbst
    gerade beendet wurde -- statt auf deren eigene temperaturbasierte Exit-Bedingung zu
    warten, die evtl. eine Weile nicht erneut greift, falls room_actual genau dann
    stabil ist. No-op, wenn Notfall-Boost ohnehin nicht aktiv ist. Wird sowohl direkt
    im Down-Callback (sofortige Reaktion auf einen erfolgreichen Ack) als auch als
    Rueckfallebene im naechsten _run_local_check-Tick aufgerufen (Design-Spec
    2026-09-23 Abschnitt 3 + Edge Cases).
    """
    if not failsafe_ctx["emergency_boost_active"]:
        return
    failsafe_ctx["emergency_boost_active"] = _exit_emergency_boost(manifest, ha_api, options)
    _save_emergency_active_if_changed(failsafe_ctx["emergency_boost_active"], BACKUP_PATH)


def _exit_emergency_boost(manifest, ha_api, options: dict) -> bool:
    """Einziger Ausstiegspfad einer Notfall-Boost-Exkursion (Hysterese-Exit in
    _run_local_check ebenso wie Notbetrieb-Ende via _end_emergency_boost_if_active).
    Gibt das Live-Geraet an den Zustand zurueck, der OHNE Notfall-Boost gelten wuerde:
    laeuft der Comfort-Boost laut backup.json noch (`boost_active`), dessen Werte --
    sonst die zuletzt vom Server bestaetigten Werte (apply_emergency_decision's
    Standard-Restore). Ohne diese Unterscheidung wuerde ein noch laufender Comfort-Boost
    beim Notfall-Boost-Ende stillschweigend ueberschrieben, waehrend `boost_active`
    weiter True bleibt (Design-Spec 2026-09-23, Abschnitt 3: "Der Comfort-Boost laeuft
    mit seinem eigenen State unbeeinflusst weiter ... endet Notbetrieb, arbeitet er
    normal weiter"; Final-Review-Fund I2 b). Liest `boost_active` aus backup.json statt
    aus dem In-Memory-_BoostStateBox, weil auch der Down-Callback-Thread hierher kommt;
    _run_local_check persistiert den aktuellen Tick-Wert vor dem Notfall-Block.
    Gibt immer False zurueck (neuer Wert fuer `emergency_boost_active`).
    """
    clamp_kwargs = dict(
        curve_min=options["curve_min"], curve_max=options["curve_max"],
        offset_min=options["offset_min"], offset_max=options["offset_max"],
        backup_path=BACKUP_PATH,
    )
    if load_backup(BACKUP_PATH).get("boost_active", False):
        # Schreibt die Comfort-Boost-Werte ueber denselben (geclampten) Transition-Write
        # wie ein frisch startender Comfort-Boost.
        apply_boost_decision(
            decision=BoostDecision(
                active=True,
                curve_value=options["boost_curve_value"],
                offset_value=options["boost_offset_value"],
            ),
            boost_was_active=False,
            manifest=manifest, ha_api=ha_api, **clamp_kwargs,
        )
        logger.warning("Notfall-Boost beendet: Comfort-Boost laeuft noch, dessen Werte wiederhergestellt")
        return False
    return apply_emergency_decision(
        decision=EmergencyBoostDecision(active=False, curve_value=None, offset_value=None),
        emergency_was_active=True,
        manifest=manifest, ha_api=ha_api, **clamp_kwargs,
    )


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
        # Whole-Branch-Review Important #2 (final-review-report.md): sichtbar machen,
        # falls der Stable-Target-Cache noch nie befuellt wurde -- ohne dieses Log sieht
        # ein leerer Check (kein Boost-Eval, kein Snapshot) im Log genauso aus wie ein
        # regulaerer No-Op.
        logger.warning("Lokaler Check uebersprungen: Stable-Target-Cache noch nicht befuellt (room_target=None)")
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

        if failsafe_ctx is not None and failsafe_ctx["emergency_boost_active"]:
            # Praezedenz Notfall- vor Comfort-Boost (Design-Spec 2026-09-23, Abschnitt 3;
            # Final-Review-Fund I2 a): haelt der Notfall-Boost das Geraet bereits (aus
            # einem frueheren Tick), fuehrt der Comfort-Boost nur seinen eigenen State
            # weiter und schreibt NICHT live -- apply_boost_decision schreibt nur bei
            # einem Zustandswechsel, und der Notfall-Block unten schreibt im
            # Dauerzustand gar nicht, ein Comfort-Transition-Write wuerde die Maximalwerte
            # also sonst unbemerkt ueberschreiben. Endet der Notfall-Boost (in diesem
            # oder einem spaeteren Tick), setzt _exit_emergency_boost das Geraet anhand
            # des dann aktuellen `boost_active` korrekt. `decision.active` ist exakt der
            # Rueckgabewert, den apply_boost_decision geliefert haette.
            boost_was_active = decision.active
        else:
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

        if failsafe_ctx is not None:
            if failsafe_ctx["state"].active:
                emergency_decision = decide_emergency_boost(
                    room_actual=room_actual, room_target=room_target,
                    emergency_was_active=failsafe_ctx["emergency_boost_active"],
                    exit_threshold_k=options.get("boost_threshold_k", 0.5),
                    max_curve_value=options["curve_max"], max_offset_value=options["offset_max"],
                )
                if failsafe_ctx["emergency_boost_active"] and not emergency_decision.active:
                    failsafe_ctx["emergency_boost_active"] = _exit_emergency_boost(manifest, ha_api, options)
                else:
                    failsafe_ctx["emergency_boost_active"] = apply_emergency_decision(
                        decision=emergency_decision,
                        emergency_was_active=failsafe_ctx["emergency_boost_active"],
                        manifest=manifest, ha_api=ha_api,
                        curve_min=options["curve_min"], curve_max=options["curve_max"],
                        offset_min=options["offset_min"], offset_max=options["offset_max"],
                        backup_path=BACKUP_PATH,
                    )
                _save_emergency_active_if_changed(failsafe_ctx["emergency_boost_active"], BACKUP_PATH)
            else:
                _end_emergency_boost_if_active(failsafe_ctx, manifest, ha_api, options)

        seq = _maybe_publish_full_snapshot(
            manifest=manifest, ha_api=ha_api, mqtt_client=mqtt_client, options=options,
            room_target=room_target, notify_service=options.get("notify_service", ""), now=datetime.now(),
        )
        if seq is not None and failsafe_ctx is not None:
            failsafe_ctx["state"] = register_publish_attempt(failsafe_ctx["state"], seq)
            _schedule_ack_timeout(
                seq=seq, failsafe_ctx=failsafe_ctx, mqtt_client=mqtt_client,
                failsafe_path=FAILSAFE_PATH, write_lock=write_lock,
                ha_api=ha_api, notify_service=options.get("notify_service", ""),
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
) -> str | None:
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
        return None

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
    return seq


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
                    logger.info(
                        "Stable-Target-Cache aktualisiert (room_target-Trigger): room_target=%s",
                        stable_target.value,
                    )
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


def _make_on_connected_callback(manifest, ha_api, write_lock, stable_target):
    """Baut den `on_connected`-Callback fuer `HaTriggerClient` (Re-Review final-
    review-report.md, Nachfolger des `was_connected`-Watchdog-Sampling-Ansatzes aus
    d8176c2): `HaTriggerClient` ruft diesen Callback synchron aus
    `_handle_subscribe_result` heraus auf, exakt einmal pro erfolgreicher
    (Re-)Verbindung -- erster Connect nach `start()` ebenso wie jeder spaetere
    Reconnect, mit garantiert null Sampling-Luecke, unabhaengig davon, wie kurz ein
    Ausfall war oder ob der Watchdog-Loop ihn ueberhaupt als "getrennt" beobachtet
    haette. Deckt damit sowohl den alten Reconnect-Fall (Important #1) als auch den
    alten "Cache nach fehlgeschlagenem Boot-Priming nie befuellt"-Fall (Important #2)
    mit demselben einen Mechanismus ab, ohne auf den 300s-Takt des Watchdog-Loops
    angewiesen zu sein.
    """
    def _on_connected() -> None:
        with write_lock:
            stable_target.value = _read_room_target_live(manifest, ha_api)
            logger.info(
                "Stable-Target-Cache aktualisiert (On-Connect-Hook, (Re-)Verbindung "
                "hergestellt): room_target=%s", stable_target.value,
            )
    return _on_connected


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
    on_connected = _make_on_connected_callback(
        manifest=manifest, ha_api=ha_api, write_lock=write_lock, stable_target=stable_target,
    )
    return HaTriggerClient(
        ws_url=ha_api.websocket_url(), token=ha_api.token, triggers=triggers,
        on_trigger_event=on_trigger_event, on_connected=on_connected,
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

    failsafe_ctx = _load_failsafe_ctx_safe(FAILSAFE_PATH, BACKUP_PATH)

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
        logger.info("Stable-Target-Cache initial befuellt (Boot-Priming): room_target=%s", stable_target.value)
        boost_state.active = _run_local_check(
            manifest, ha_api, mqtt_client, options, write_lock, boost_state.active,
            room_target=stable_target.value, failsafe_ctx=failsafe_ctx,
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
        failsafe_ctx["emergency_boost_active"] = False
        _save_emergency_active_if_changed(False, BACKUP_PATH)

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
            if not ha_trigger_client.connected:
                with write_lock:
                    # Undebounced mit Absicht (Design-Spec 2026-09-22, Punkt 2): hier
                    # existiert keine subscribe_trigger-Debounce-Garantie, konsistent
                    # mit "degradiert automatisch auf reinen Poll-Betrieb". Dies ist der
                    # urspruengliche, unveraenderte Watchdog-Fallback fuer echtes,
                    # andauerndes Getrenntsein -- der Reconnect-/Erstverbindungs-Fall
                    # wird seit dem On-Connect-Hook (_make_on_connected_callback) direkt
                    # von HaTriggerClient selbst abgedeckt, ohne auf diesen Tick zu
                    # warten.
                    stable_target.value = _read_room_target_live(manifest, ha_api)
                    logger.info(
                        "Stable-Target-Cache aktualisiert (Watchdog-Fallback, WS getrennt): room_target=%s",
                        stable_target.value,
                    )
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
