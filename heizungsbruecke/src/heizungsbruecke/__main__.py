import functools
import json
import logging
import math
import os
import sys
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from heizungsbruecke.backup_store import load_backup, save_backup
from heizungsbruecke.boost import BoostDecision, decide_boost
from heizungsbruecke.bridge import (
    apply_boost_decision,
    apply_emergency_decision,
    handle_down_message,
    publish_snapshot,
    read_snapshot_roles,
)
from heizungsbruecke.clamping import clamp
from heizungsbruecke.emergency_boost import EmergencyBoostDecision, decide_emergency_boost
from heizungsbruecke import daynight_snapshot, delivery, derived_sensors, entitlement
from heizungsbruecke.ha_api import HomeAssistantApi
from heizungsbruecke.ha_trigger_client import HaTriggerClient
from heizungsbruecke.manifest import ChannelManifest, ManifestError, build_manifest
from heizungsbruecke.mqtt_client import BridgeMqttClient
from heizungsbruecke.profiles import (
    UnknownProfileError,
    resolve_boost_defaults,
    resolve_local_clamps,
    resolve_window_defaults,
    window_size_hours,
)
from heizungsbruecke.target_history import record_change, sanitize_history, time_weighted_mean
from heizungsbruecke.worker import Event, RegulationWorker

OPTIONS_PATH = Path("/data/options.json")
BACKUP_PATH = Path("/data/backup.json")
FAILSAFE_PATH = Path("/data/failsafe_state.json")
DERIVED_SENSORS_PATH = Path("/data/derived_sensors.json")
DAYNIGHT_SNAPSHOT_PATH = Path("/data/daynight_snapshot_state.json")
ENTITLEMENT_PATH = Path("/data/entitlement_state.json")

# Abo-inaktiv-Modus (B8, Design-Spec 2026-09-25, Abschnitt 4). Stabile notification_id,
# damit wiederholte Meldungen in HA ersetzen statt stapeln.
ABO_NOTIFICATION_ID = "smartheat_abo_inaktiv"
ABO_NOTIFICATION_TITLE = "SmartHeat"
ABO_ENDED_MESSAGE = (
    "SmartHeat: Abo seit 30 Tagen inaktiv. Die Heizungssteuerung ist beendet, "
    "die zuletzt gelernten Werte bleiben eingestellt."
)

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
# finished booting. Erst das bisherige Budget mit Backoff, danach (B10, Design-Spec
# 2026-09-26) unbegrenzt alle DERIVED_SENSORS_UNBOUNDED_RETRY_SECONDS weiter -- ein
# spaet startendes HA ist kein Konfigurationsfehler, das Add-on beendet sich deshalb nie.
DERIVED_SENSORS_RETRY_DELAYS_SECONDS = (5, 10, 20, 40, 60, 60, 60)
DERIVED_SENSORS_UNBOUNDED_RETRY_SECONDS = 300
HELPER_NOTIFICATION_ID = "smartheat_hilfssensoren"
HELPER_NOTIFICATION_MESSAGE = (
    "SmartHeat: Hilfssensoren konnten nicht angelegt werden – Home Assistant noch nicht bereit?"
)

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
# reagieren erst danach, siehe _make_trigger_event_callback/_Bridge.stable_target weiter
# unten). Bewusst fest im Code (kein Add-on-Options-Feld) -- kein bestehender
# Plumbing-Mechanismus fuer Boost-aehnliche Parameter (auch boost_threshold_k ist
# profilbasiert, nicht per Config-Flow einstellbar), nur client1 als realer Nutzer
# aktuell. YAGNI, spaeter bei Bedarf nachruestbar.
ROOM_TARGET_DEBOUNCE_SECONDS = 10

# Ereignisarten des Regel-Workers (Design-Spec 2026-09-26, Abschnitt 1).
EV_LOCAL_CHECK = "local_check"
EV_SETPOINTS = "setpoints"
EV_AUTH_REJECTED = "auth_rejected"
EV_ACK_TIMEOUT = "ack_timeout"
EV_RETRY_DUE = "retry_due"
EV_WATCHDOG = "watchdog"
EV_TELEMETRY = "telemetry"
EV_DAYNIGHT = "daynight"
EV_GRACE_CHECK = "grace_check"

# Optionale KPI-Rollen (Design-Spec KPI-Logging-Rework): nur vorhanden, wenn in
# manifest.entity_ids konfiguriert; fehlende/ausgefallene Sensoren werden im Payload
# weggelassen (nie null/0 senden -- der Server speichert NULL fuer fehlende Felder).
_KPI_NUMERIC_ROLES = (
    "flow_temperature", "return_temperature", "system_water_pressure", "efficiency_ratio",
)
_KPI_ENERGY_ROLES = (
    "energy_electrical_heating", "energy_electrical_dhw",
    "energy_primary_heating", "energy_primary_dhw",
    "energy_thermal_heating", "energy_thermal_dhw",
)

# Gleicher Hostname fuer jeden Tenant (kein Tenant-spezifischer Wert) -- siehe
# SmartHeat-HomeAssistant-Integration/custom_components/smartheat/api_client.py,
# DEFAULT_HEIZUNGSSERVER_BASE_URL. Hartkodiert wie MQTT_HOST/MQTT_PORT oben, aus
# demselben Grund: ein einzelner geteilter Wert, keine Pro-Tenant-Konfiguration.
ACCOUNTS_API_BASE_URL = "https://accounts.hartfussha.org"

logger = logging.getLogger(__name__)


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
    the telemetry schedule entry would fire as often as every second and flood the server.
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
    """Wraps `derived_sensors.ensure_all` with retry (see
    DERIVED_SENSORS_RETRY_DELAYS_SECONDS above): first the backoff budget, then forever
    every DERIVED_SENSORS_UNBOUNDED_RETRY_SECONDS. Beim Uebergang in die unbegrenzte
    Phase einmal ERROR-Log und best effort eine HA-Benachrichtigung. Wirft nie."""
    delays = DERIVED_SENSORS_RETRY_DELAYS_SECONDS
    attempt = 0
    while True:
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
            if attempt < len(delays):
                logger.warning(
                    "Anlegen der abgeleiteten Sensoren fehlgeschlagen (Versuch %s/%s, evtl. ist "
                    "HA Core beim Start des Add-ons noch nicht bereit): %s",
                    attempt + 1, len(delays) + 1, error,
                )
                delay = delays[attempt]
            else:
                if attempt == len(delays):
                    logger.error(
                        "Abgeleitete Sensoren nach %s Versuchen nicht angelegt, weiter alle %s s: %s",
                        attempt + 1, DERIVED_SENSORS_UNBOUNDED_RETRY_SECONDS, error,
                    )
                    try:
                        ha_api.create_persistent_notification(
                            ABO_NOTIFICATION_TITLE, HELPER_NOTIFICATION_MESSAGE, HELPER_NOTIFICATION_ID,
                        )
                    except Exception:
                        logger.warning("HA-Benachrichtigung zu den Hilfssensoren konnte nicht angelegt werden")
                else:
                    logger.warning("Anlegen der abgeleiteten Sensoren weiter fehlgeschlagen (Versuch %s): %s", attempt + 1, error)
                delay = DERIVED_SENSORS_UNBOUNDED_RETRY_SECONDS
            attempt += 1
            time.sleep(delay)


def _check_timezone(ha_api) -> None:
    """B6 (Design-Spec 2026-09-26, Abschnitt 3): taegliche Zeitpunkte (Tagestick,
    Tag-/Nachtmittel) rechnet das Add-on in der Container-Zeitzone. Weicht sie von der
    HA-Zeitzone ab, nur warnen -- keine Aenderung an der Zeitrechnung, kein Abbruch."""
    try:
        ha_time_zone = ha_api.get_config().get("time_zone")
    except Exception as error:
        logger.warning("Zeitzone von Home Assistant konnte nicht abgefragt werden: %s", error)
        return
    container_time_zone = os.environ.get("TZ") or str(datetime.now().astimezone().tzinfo)
    if ha_time_zone and ha_time_zone != container_time_zone:
        logger.warning(
            "Zeitzone weicht ab: Home Assistant '%s', Add-on-Container '%s' - taegliche "
            "Zeitpunkte laufen in der Container-Zeit.", ha_time_zone, container_time_zone,
        )
    else:
        logger.info("Zeitzone: %s", container_time_zone)


_SETPOINT_STATUSES_WITH_VALUES = ("ok", "skipped_summer")


def _is_finite_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _load_failsafe_ctx(path: Path, backup_path: Path) -> dict:
    """Stellt den Kontext nach einem Neustart wieder her: `delivery` aus path
    (failsafe_state.json, tolerant ueber delivery.from_persisted -- ein offener Tick wird
    beim Start mit derselben seq erneut versucht, ein Notbetrieb endet so auch ueber
    Neustarts hinweg von selbst) und `emergency_boost_active` aus backup_path (backup.json,
    von `_save_emergency_active_if_changed` gepflegt). Beide MUESSEN den Neustart
    ueberleben: das Boot-Priming braucht den echten Vorher-Wert von
    `emergency_boost_active`, um eine laufende Notfall-Exkursion korrekt fortzusetzen bzw.
    das Geraet von den Maximalwerten zurueckzusetzen (Final-Review-Fund C1)."""
    raw = load_backup(path)
    backup = load_backup(backup_path)
    return {
        "delivery": delivery.from_persisted(raw),
        "emergency_boost_active": backup.get("emergency_boost_active", False),
    }


def _load_failsafe_ctx_safe(path: Path, backup_path: Path) -> dict:
    """Wraps `_load_failsafe_ctx` so a corrupt/truncated state file (e.g. after power
    loss on the Pi's SD card) cannot crash the add-on at startup. Falls back per file: a
    corrupt failsafe_state.json must not also discard a still-valid persisted
    `emergency_boost_active=True` from backup.json -- boot-priming relies on that flag to
    restore the live device from its max-heat values."""
    try:
        return _load_failsafe_ctx(path, backup_path)
    except Exception as error:
        logger.warning(
            "Fail-Safe-Zustand konnte nicht vollstaendig gelesen werden (%s, %s), starte "
            "mit Standardzustand fuer die nicht lesbare Datei: %s",
            path, backup_path, error,
        )
    try:
        delivery_state = delivery.from_persisted(load_backup(path))
    except Exception:
        delivery_state = delivery.DeliveryState()
    try:
        emergency_boost_active = load_backup(backup_path).get("emergency_boost_active", False)
    except Exception:
        emergency_boost_active = False
    return {"delivery": delivery_state, "emergency_boost_active": emergency_boost_active}


def _save_failsafe_ctx(ctx: dict, path: Path) -> None:
    """Persistiert den dauerhaften Teil des Zustell-Zustands (delivery.to_persisted) nach
    failsafe_state.json. `emergency_boost_active` liegt separat in backup.json."""
    save_backup(path, delivery.to_persisted(ctx["delivery"]))


def _persist_failsafe_ctx(ctx: dict) -> None:
    """Best effort: ein Schreibfehler (SD-Karte) darf die Regelung nicht aufhalten, der
    Zustand gilt dann nur bis zum naechsten Neustart."""
    try:
        _save_failsafe_ctx(ctx, FAILSAFE_PATH)
    except Exception:
        logger.exception(
            "failsafe_state.json konnte nicht geschrieben werden - Zustand gilt nur bis zum naechsten Neustart"
        )


def _save_emergency_active_if_changed(emergency_boost_active: bool, path: Path) -> None:
    backup = load_backup(path)
    if backup.get("emergency_boost_active", False) != emergency_boost_active:
        backup["emergency_boost_active"] = emergency_boost_active
        save_backup(path, backup)


def _end_emergency_boost_if_active(failsafe_ctx: dict, manifest, ha_api, options: dict) -> None:
    """Beendet eine laufende Notfall-Boost-Exkursion sofort, wenn Notbetrieb selbst
    gerade beendet wurde -- statt auf deren eigene temperaturbasierte Exit-Bedingung zu
    warten, die evtl. eine Weile nicht erneut greift, falls room_actual genau dann
    stabil ist. No-op, wenn Notfall-Boost ohnehin nicht aktiv ist. Wird sowohl direkt
    beim Notbetrieb-Ende (Zustell-Aktion EndEmergencyBoost) als auch als
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
    normal weiter"; Final-Review-Fund I2 b). Liest `boost_active` aus backup.json;
    `_run_local_check` persistiert den aktuellen Wert vor dem Notfall-Block.
    Gibt immer False zurueck (neuer Wert fuer `emergency_boost_active`).
    """
    clamp_kwargs = dict(
        curve_min=options["curve_min"], curve_max=options["curve_max"],
        offset_min=options["offset_min"], offset_max=options["offset_max"],
        backup_path=BACKUP_PATH,
    )
    if load_backup(BACKUP_PATH).get("boost_active", False):
        # Schreibt die Comfort-Boost-Werte ueber denselben (geclampten) Transition-Write
        # wie ein frisch startender Comfort-Boost. Der laeuft seit Task 6 ueber die
        # Wiederherstellungspunkt-Pruefung und kann daher ohne Schreiben False liefern.
        written = apply_boost_decision(
            decision=BoostDecision(
                active=True,
                curve_value=options["boost_curve_value"],
                offset_value=options["boost_offset_value"],
            ),
            boost_was_active=False,
            manifest=manifest, ha_api=ha_api, **clamp_kwargs,
        )
        if written:
            logger.warning("Notfall-Boost beendet: Comfort-Boost laeuft noch, dessen Werte wiederhergestellt")
        else:
            logger.warning(
                "Notfall-Boost beendet: Comfort-Boost laeuft noch, dessen Werte konnten aber nicht "
                "geschrieben werden (kein Wiederherstellungspunkt)"
            )
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


def _abo_inactive(failsafe_ctx: dict | None) -> bool:
    return failsafe_ctx is not None and failsafe_ctx.get("abo_inactive_since") is not None


def _abo_finished(failsafe_ctx: dict | None) -> bool:
    """True, sobald _finish_abo_grace die zuletzt gelernten Werte erfolgreich
    wiederhergestellt hat -- danach darf kein lokaler Check mehr schreiben."""
    return failsafe_ctx is not None and bool(failsafe_ctx.get("abo_finished"))


def _abo_grace_expired(failsafe_ctx: dict) -> bool:
    since = failsafe_ctx.get("abo_inactive_since")
    return since is not None and entitlement.grace_expired(since, datetime.now().astimezone())


def _abo_inactive_message(inactive_since: datetime) -> str:
    end = entitlement.grace_end(inactive_since).strftime("%d.%m.%Y")
    return (
        f"SmartHeat: Abo inaktiv. Die Heizung läuft noch bis {end} im Notbetrieb weiter, "
        f"danach bleiben die zuletzt gelernten Werte fest eingestellt."
    )


def _notify_abo(ha_api, notify_service: str, message: str) -> None:
    """Log (WARNING), Push (falls konfiguriert) und HA-persistent_notification -- jeder
    Kanal best effort, ein kaputter Kanal blockiert die anderen nicht."""
    logger.warning(message)
    if notify_service:
        try:
            ha_api.send_notification(notify_service, message)
        except Exception:
            logger.warning("Push-Benachrichtigung zum Abo-Status konnte nicht gesendet werden")
    try:
        ha_api.create_persistent_notification(ABO_NOTIFICATION_TITLE, message, ABO_NOTIFICATION_ID)
    except Exception:
        logger.warning("HA-Benachrichtigung zum Abo-Status konnte nicht angelegt werden")


def _enter_abo_inactive(failsafe_ctx: dict, mqtt_client, ha_api, notify_service: str, now: datetime) -> None:
    """Wechsel in den lokalen Abo-inaktiv-Modus: Notbetrieb an (persistiert), MQTT
    beendet, kein Snapshot/Telemetrie mehr. Meldung nur beim erstmaligen Setzen von
    inactive_since, nicht bei jedem Neustart. Läuft im Worker-Thread; `mqtt_client.stop()`
    joint den paho-Thread, dessen Callbacks nur einstellen und daher nie blockieren."""
    if _abo_inactive(failsafe_ctx):
        return
    try:
        since, newly_set = entitlement.mark_inactive(ENTITLEMENT_PATH, now)
    except Exception:
        # Final-Review Minor 4: ein Schreibfehler (z.B. defekte SD-Karte) darf den
        # Notbetrieb nicht verhindern -- Modus nur im Speicher, Frist ab jetzt.
        logger.exception(
            "Abo-inaktiv-Zeitpunkt konnte nicht gespeichert werden - Abo-inaktiv-Modus "
            "gilt nur bis zum naechsten Neustart, Frist ab jetzt"
        )
        since, newly_set = now, True
    failsafe_ctx["abo_inactive_since"] = since
    failsafe_ctx["delivery"] = replace(failsafe_ctx["delivery"], pending=None, notbetrieb=True)
    _persist_failsafe_ctx(failsafe_ctx)
    if mqtt_client is not None:
        try:
            mqtt_client.stop()
        except Exception:
            logger.exception("MQTT-Verbindung konnte nicht sauber beendet werden")
    if newly_set:
        _notify_abo(ha_api, notify_service, _abo_inactive_message(since))
    else:
        logger.warning(
            "Abo weiterhin inaktiv (seit %s) - Notbetrieb laeuft bis %s.",
            since.strftime("%d.%m.%Y"), entitlement.grace_end(since).strftime("%d.%m.%Y"),
        )


def _finish_abo_grace(
    manifest, ha_api, options: dict, failsafe_ctx: dict, *, always_restore: bool, final_notice: bool,
) -> bool:
    """Fristende (always_restore=True, final_notice=True) bzw. Abschluss-Start
    (always_restore=False, final_notice=False): laufende Boosts beenden und die zuletzt
    vom Server bestaetigten Werte aus backup.json geclampt auf das Geraet schreiben.

    Gibt True zurueck, wenn die Wiederherstellung geklappt hat (oder nichts
    wiederherzustellen war); dann wird failsafe_ctx["abo_finished"] gesetzt, damit ein
    danach noch verarbeiteter lokaler Check nichts mehr schreibt (Final-Review I-1).
    Scheitert das Schreiben, bleiben die Boost-Flags gesetzt, es gibt keine
    Abschlussmeldung und der Aufrufer versucht es erneut (Final-Review I-2) -- sonst
    bliebe das Geraet dauerhaft auf Boost-Werten."""
    backup = load_backup(BACKUP_PATH)
    boosting = bool(backup.get("boost_active")) or bool(backup.get("emergency_boost_active"))
    if always_restore or boosting:
        try:
            for role, minimum, maximum in (
                ("curve_current", options["curve_min"], options["curve_max"]),
                ("offset_current", options["offset_min"], options["offset_max"]),
            ):
                if role in backup and role in manifest.entity_ids:
                    ha_api.set_number_value(manifest.entity_ids[role], clamp(backup[role], minimum, maximum))
        except Exception:
            logger.exception(
                "Zuletzt gelernte Werte konnten nicht wiederhergestellt werden - Boost-Flags "
                "bleiben gesetzt, es wird erneut versucht"
            )
            return False
        backup["boost_active"] = False
        backup["emergency_boost_active"] = False
        save_backup(BACKUP_PATH, backup)
        failsafe_ctx["emergency_boost_active"] = False
    failsafe_ctx["abo_finished"] = True
    if final_notice:
        _notify_abo(ha_api, options.get("notify_service", ""), ABO_ENDED_MESSAGE)
    else:
        logger.info("Abo-inaktiv-Frist ist bereits abgelaufen - Add-on beendet sich ohne weitere Eingriffe.")
    return True


@dataclass
class _Bridge:
    """Laufzeit-Kontext des Regel-Workers. Wird ausschliesslich im Worker-Thread gelesen
    und geschrieben (Design-Spec 2026-09-26, Abschnitt 1) und braucht daher keinen Lock.
    Die volle Zustands-/Persistenzschicht (BridgeState) folgt in TP5."""
    manifest: ChannelManifest
    ha_api: HomeAssistantApi
    options: dict
    derived_entity_ids: dict
    worker: RegulationWorker
    failsafe_ctx: dict
    boost_active: bool = False
    stable_target: float | None = None
    mqtt_client: BridgeMqttClient | None = None
    trigger_client: HaTriggerClient | None = None


def _local_check_interval(options: dict) -> float:
    return options.get("local_check_interval_seconds", DEFAULT_LOCAL_CHECK_INTERVAL_SECONDS)


def _run_local_check(
    manifest, ha_api, options, boost_was_active: bool, room_target: float | None, failsafe_ctx: dict | None = None,
) -> bool:
    """Ein lokaler Boost-/Notfall-Boost-Durchlauf (Design-Spec 2026-09-16, Abschnitt A):
    liest room_actual lokal, wertet Comfort- und Notfall-Boost aus und persistiert
    boost_active fuer bridge.py::handle_down_message (Abschnitt D). Publiziert selbst
    nichts -- ob ein Tick faellig ist, entscheidet danach _maybe_start_tick
    (Design-Spec 2026-09-26). Gibt den Boost-Zustand fuer den naechsten Check zurueck;
    I/O-Fehler gehen an den Aufrufer (Worker-Handler), der sie loggt.

    `room_target` ist ein PARAMETER (Stable-Target-Cache, Design-Spec 2026-09-22), jeder
    Aufrufer liest ihn selbst live oder aus dem Cache. `None` heisst: Cache noch nicht
    befuellt -- wie eine ungemappte Rolle ein No-Op.
    """
    if "room_actual" not in manifest.entity_ids or "room_target" not in manifest.entity_ids:
        return boost_was_active
    if room_target is None:
        # Whole-Branch-Review Important #2: sichtbar machen, falls der Cache nie befuellt wurde.
        logger.warning("Lokaler Check uebersprungen: Stable-Target-Cache noch nicht befuellt (room_target=None)")
        return boost_was_active
    if _abo_finished(failsafe_ctx):
        # Final-Review I-1: nach abgeschlossener Abo-Frist darf kein Check mehr schreiben.
        return boost_was_active

    room_actual = ha_api.get_state(manifest.entity_ids["room_actual"])
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

    now_epoch = time.time()
    raw_history = backup.get("target_history", [])
    history = sanitize_history(raw_history)
    if history != raw_history:
        logger.warning(
            "target_history in Backup war ungueltig (%r) - wird als leer behandelt und neu aufgebaut.",
            raw_history,
        )
    updated_history = record_change(history, now_epoch, room_target)

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

    # B5 (Design-Spec 2026-09-26, Abschnitt 3): wurde ein Comfort-Boost mangels
    # Wiederherstellungspunkt ausgesetzt, bleibt der alte Sollwert gemerkt, damit der
    # naechste Check die Erhoehung erneut sieht und es noch einmal versucht.
    boost_refused = decision.active and not boost_was_active
    remembered_target = previous_room_target if boost_refused else room_target
    # I/O-Fix (Abschnitt A.2): nur schreiben, wenn sich etwas geaendert hat. Frisch laden:
    # apply_boost_decision kann gerade einen Wiederherstellungspunkt gesichert haben.
    if remembered_target != previous_room_target or updated_history != history:
        backup = load_backup(BACKUP_PATH)
        backup["last_room_target"] = remembered_target
        backup["target_history"] = updated_history
        save_backup(BACKUP_PATH, backup)
    _save_boost_active_if_changed(boost_was_active, BACKUP_PATH)

    if failsafe_ctx is not None:
        if failsafe_ctx["delivery"].notbetrieb:
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

    return boost_was_active


def _claim_due_tick(options: dict, room_target: float, now: datetime) -> str | None:
    """Entscheidet, ob ein neuer Tick faellig ist -- room_target seit dem letzten Tick
    geaendert ("target_change") oder daily_trigger_time heute erstmals erreicht
    ("daily"), Design-Spec 2026-09-16 Abschnitt A.3/B -- und bucht ihn sofort in
    backup.json. last_published_target_rt/last_daily_trigger_date werden beim ENTSTEHEN
    des Ticks gesetzt, nicht erst beim Publish (Design-Spec 2026-09-26, Abschnitt 2
    "Buchhaltung"): ein wegen Datenfehler zurueckgehaltener Tick erzeugt keine
    Duplikate, seine Wiederholungen laufen ueber die Zustellung.

    Eine einfache "Uhrzeit erreicht, heute noch nicht gefeuert"-Pruefung genuegt: bei
    verbundenem HaTriggerClient loest dessen `time`-Trigger genau zu daily_trigger_time
    einen lokalen Check aus, bei getrennter Verbindung tut es spaetestens das naechste
    Watchdog-Ereignis (alle local_check_interval_seconds, max. 3600 s).
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

    backup["last_published_target_rt"] = room_target
    if daily_due:
        backup["last_daily_trigger_date"] = today
    save_backup(BACKUP_PATH, backup)
    return "target_change" if target_changed else "daily"


def _maybe_start_tick(bridge: _Bridge, now: datetime) -> None:
    if _abo_inactive(bridge.failsafe_ctx) or bridge.stable_target is None:
        return
    trigger = _claim_due_tick(bridge.options, bridge.stable_target, now)
    if trigger is not None:
        _deliver(bridge, delivery.TickDue(seq=str(uuid.uuid4()), trigger=trigger))


def _deliver(bridge: _Bridge, event) -> None:
    """Fuehrt ein Zustell-Ereignis durch delivery.step und dessen Aktionen aus. Aktionen
    mit Ergebnis (attempt, Abo-Abfrage) speisen es als neues Ereignis zurueck.
    Persistiert nur, wenn sich der dauerhafte Teil des Zustands aendert (SD-Karte)."""
    pending_events = [event]
    while pending_events:
        current = pending_events.pop(0)
        before = bridge.failsafe_ctx["delivery"]
        after, actions = delivery.step(before, current)
        bridge.failsafe_ctx["delivery"] = after
        if delivery.to_persisted(after) != delivery.to_persisted(before):
            _persist_failsafe_ctx(bridge.failsafe_ctx)
        for action in actions:
            try:
                follow_up = _execute_delivery_action(bridge, action)
            except Exception:
                logger.exception("Zustell-Aktion %r fehlgeschlagen", action)
                follow_up = _fallback_follow_up(action)
            if follow_up is not None:
                pending_events.append(follow_up)


def _fallback_follow_up(action):
    """Ergebnis-Ereignis fuer eine unerwartet gescheiterte Aktion mit Ergebnis: ohne
    Rueckmeldung bliebe der offene Tick ohne Zeitplaneintrag haengen (Review Focus 2).
    Ein gescheiterter attempt zaehlt wie ein Publish ohne Antwort (der Ack-Timeout plant
    den naechsten Versuch), eine gescheiterte Abo-Abfrage wie "unbekannt" (fail-open)."""
    if isinstance(action, delivery.Attempt):
        return delivery.Published(seq=action.seq)
    if isinstance(action, delivery.QueryEntitlement):
        return delivery.EntitlementChecked(seq=action.seq, status=entitlement.UNKNOWN)
    return None


def _execute_delivery_action(bridge: _Bridge, action):
    if isinstance(action, delivery.Attempt):
        return _attempt(bridge, action.seq, action.trigger)
    if isinstance(action, delivery.QueryEntitlement):
        status = entitlement.query_status(bridge.options["tenant_id"], ACCOUNTS_API_BASE_URL)
        return delivery.EntitlementChecked(seq=action.seq, status=status)
    if isinstance(action, delivery.ScheduleAckTimeout):
        bridge.worker.schedule(action.delay_s, Event(EV_ACK_TIMEOUT, {"seq": action.seq, "gen": action.gen}))
    elif isinstance(action, delivery.ScheduleRetry):
        bridge.worker.schedule(action.delay_s, Event(EV_RETRY_DUE, {"seq": action.seq, "gen": action.gen}))
    elif isinstance(action, delivery.EnterAboInactive):
        _enter_abo_inactive(
            bridge.failsafe_ctx, bridge.mqtt_client, bridge.ha_api,
            bridge.options.get("notify_service", ""), datetime.now().astimezone(),
        )
    elif isinstance(action, delivery.Notify):
        _notify_delivery(bridge, action)
    elif isinstance(action, delivery.PublishFailsafe):
        if bridge.mqtt_client is not None:
            bridge.mqtt_client.publish_status("failsafe", delivery.build_state_payload(action.active))
    elif isinstance(action, delivery.EndEmergencyBoost):
        _end_emergency_boost_if_active(bridge.failsafe_ctx, bridge.manifest, bridge.ha_api, bridge.options)
    else:
        raise TypeError(f"Unbekannte Zustell-Aktion: {action!r}")
    return None


def _attempt(bridge: _Bridge, seq: str, trigger: str):
    """attempt (Design-Spec 2026-09-26, Abschnitt 2): Pflichtrollen und room_actual frisch
    lesen; bei ungueltigem Wert kein Publish (ReadInvalid), sonst den Snapshot mit dieser
    seq publizieren (Published). Darf die Retry-Kette nie abreissen lassen: auch ein
    Publish-Fehler meldet Published, der Ack-Timeout plant dann den naechsten Versuch."""
    read = read_snapshot_roles(
        bridge.manifest, bridge.ha_api, computed_values={"room_target_avg_24h": _current_target_avg()},
    )
    if read.invalid_roles:
        logger.warning("Snapshot (seq=%s) zurueckgehalten, ungueltige Werte: %s", seq, ", ".join(read.invalid_roles))
        return delivery.ReadInvalid(seq=seq, roles=read.invalid_roles)
    try:
        publish_snapshot(bridge.mqtt_client, seq=seq, trigger=trigger, roles=read.roles)
        logger.info("Voller Snapshot veroeffentlicht (seq=%s, trigger=%s)", seq, trigger)
    except Exception:
        logger.exception("Snapshot (seq=%s) konnte nicht veroeffentlicht werden - Retry nach dem Ack-Timeout", seq)
    return delivery.Published(seq=seq)


def _current_target_avg() -> float | None:
    """Zeitgewichtetes 24-h-Mittel des Sollwerts (room_target_avg_24h) aus backup.json."""
    try:
        history = sanitize_history(load_backup(BACKUP_PATH).get("target_history", []))
        return time_weighted_mean(history, time.time())
    except Exception as error:
        logger.warning("Sollwert-Mittel nicht berechenbar, wird weggelassen: %s", error)
        return None


def _notify_delivery(bridge: _Bridge, action) -> None:
    text = delivery.notification_text(action.kind, action.detail, bridge.manifest.entity_ids)
    logger.warning(text)
    notify_service = bridge.options.get("notify_service", "")
    if notify_service:
        try:
            bridge.ha_api.send_notification(notify_service, text)
        except Exception:
            logger.warning("Push-Benachrichtigung (%s) konnte nicht gesendet werden", action.kind)


def _on_local_check(bridge: _Bridge, event: Event) -> None:
    """Lokaler Check (Design-Spec 2026-09-26, Abschnitt 1): bei room_target_fired den
    Stable-Target-Cache frisch lesen, dann Boost-/Notfall-Boost-Logik, dann "Tick
    faellig?". Beide Teile sind getrennt abgesichert: ein toter room_actual-Fuehler laesst
    den Boost-Teil scheitern, darf aber einen Tick nicht verhindern -- dessen attempt
    meldet den Fuehler dann als Datenfehler."""
    if _abo_finished(bridge.failsafe_ctx):
        return
    if event.data.get("room_target_fired"):
        try:
            bridge.stable_target = _read_room_target_live(bridge.manifest, bridge.ha_api)
            logger.info("Stable-Target-Cache aktualisiert: room_target=%s", bridge.stable_target)
        except Exception as error:
            logger.warning("room_target nicht lesbar, Stable-Target-Cache bleibt bei %s: %s", bridge.stable_target, error)
    try:
        bridge.boost_active = _run_local_check(
            bridge.manifest, bridge.ha_api, bridge.options, bridge.boost_active,
            room_target=bridge.stable_target, failsafe_ctx=bridge.failsafe_ctx,
        )
    except Exception:
        logger.exception("Fehler im lokalen Check (Boost/Notfall-Boost), wird beim naechsten Ereignis erneut versucht")
    _maybe_start_tick(bridge, datetime.now())


def _on_setpoints(bridge: _Bridge, event: Event) -> None:
    """Server-Antwort (Schema 2). Zaehlt nur fuer den offenen Tick (delivery.accepts_ack),
    auch verspaetet. Bei ok/skipped_summer mit gueltigen Werten werden diese VOR dem Ack
    geschrieben (geclampt, Boost-Sperre ueber backup.json); scheitert das Schreiben, gibt es
    keinen Ack -- Ack-Timeout und Retry mit derselben seq holen die Werte erneut, der
    Server antwortet idempotent. Unbekannter Status oder ungueltige Werte zaehlen als
    Datenfehler vom Server (Bewusste Abweichung 5)."""
    payload = event.data["payload"]
    seq = payload.get("seq")
    state = bridge.failsafe_ctx["delivery"]
    if not delivery.accepts_ack(state, seq):
        expected = state.pending.seq if state.pending is not None else None
        logger.info("Setpoints-Antwort mit seq=%r ignoriert (erwartet: %r)", seq, expected)
        return

    status = payload.get("status")
    curve, offset = payload.get("curve"), payload.get("offset")
    if status in _SETPOINT_STATUSES_WITH_VALUES and _is_finite_number(curve) and _is_finite_number(offset):
        options = bridge.options
        for role, value in (("curve_current", curve), ("offset_current", offset)):
            if role in bridge.manifest.entity_ids:
                handle_down_message(
                    role=role, value=value, manifest=bridge.manifest, ha_api=bridge.ha_api,
                    curve_min=options["curve_min"], curve_max=options["curve_max"],
                    offset_min=options["offset_min"], offset_max=options["offset_max"],
                    backup_path=BACKUP_PATH,
                )
        _deliver(bridge, delivery.Ack(seq=seq, status=status))
    elif status == delivery.STATUS_REJECTED:
        reason = payload.get("reason")
        reason = reason if isinstance(reason, str) and reason else None
        logger.warning("Server hat Snapshot (seq=%s) abgelehnt: %s", seq, reason)
        _deliver(bridge, delivery.Ack(seq=seq, status=delivery.STATUS_REJECTED, reason=reason))
    else:
        reason = f"ungültige Serverantwort (status={status!r}, curve={curve!r}, offset={offset!r})"
        logger.warning("Setpoints-Antwort (seq=%s): %s - nichts geschrieben", seq, reason)
        _deliver(bridge, delivery.Ack(seq=seq, status=delivery.STATUS_REJECTED, reason=reason))


def _on_ack_timeout(bridge: _Bridge, event: Event) -> None:
    _deliver(bridge, delivery.AckTimeout(seq=event.data["seq"], gen=event.data["gen"]))


def _on_retry_due(bridge: _Bridge, event: Event) -> None:
    _deliver(bridge, delivery.RetryDue(seq=event.data["seq"], gen=event.data["gen"]))


def _on_auth_rejected(bridge: _Bridge, event: Event) -> None:
    """Der Broker hat die Zugangsdaten abgelehnt (Credentials beim Suspend widerrufen,
    hs-2). Nur ein eindeutiges "inactive" wechselt in den Abo-inaktiv-Modus; sonst bleibt
    es beim Log und paho versucht weiter zu verbinden. Die Abfrage blockiert den Worker
    bis zu 10 s -- akzeptiert (Design-Spec 2026-09-26, Abschnitt 1)."""
    status = entitlement.query_status(bridge.options["tenant_id"], ACCOUNTS_API_BASE_URL)
    if status != entitlement.INACTIVE:
        logger.error(
            "MQTT-Anmeldung vom Broker abgelehnt, Abo-Status ist aber '%s' - Zugangsdaten "
            "pruefen (ggf. SmartHeat-Integration neu einrichten).", status,
        )
        return
    _enter_abo_inactive(
        bridge.failsafe_ctx, bridge.mqtt_client, bridge.ha_api,
        bridge.options.get("notify_service", ""), datetime.now().astimezone(),
    )


def _on_watchdog(bridge: _Bridge, event: Event) -> None:
    """Fallback bei getrennter WS-Verbindung (Design-Spec 2026-09-21/22): Cache frisch und
    undebounced lesen, dann lokaler Check. Bei verbundenem Client tut er nichts."""
    bridge.worker.schedule(_local_check_interval(bridge.options), Event(EV_WATCHDOG))
    if bridge.trigger_client is None or not bridge.trigger_client.connected:
        bridge.worker.post_coalesced(EV_LOCAL_CHECK, room_target_fired=True)


def _on_telemetry(bridge: _Bridge, event: Event) -> None:
    bridge.worker.schedule(
        bridge.options.get("telemetry_interval_seconds", DEFAULT_TELEMETRY_INTERVAL_SECONDS), Event(EV_TELEMETRY),
    )
    if _abo_inactive(bridge.failsafe_ctx) or bridge.mqtt_client is None:
        return
    _run_telemetry_tick(
        bridge.manifest, bridge.ha_api, bridge.mqtt_client,
        boost_active=bridge.boost_active, failsafe_active=bridge.failsafe_ctx["delivery"].notbetrieb,
    )


def _on_daynight(bridge: _Bridge, event: Event) -> None:
    bridge.worker.schedule(_local_check_interval(bridge.options), Event(EV_DAYNIGHT))
    daynight_snapshot.maybe_snapshot(
        ha_api=bridge.ha_api,
        room_12h_avg_entity_id=bridge.derived_entity_ids["_room_12h_avg"],
        day_avg_entity_id=bridge.derived_entity_ids["room_day_avg"],
        night_avg_entity_id=bridge.derived_entity_ids["room_night_avg"],
        day_avg_window_end=bridge.options["day_avg_window_end"],
        night_avg_window_end=bridge.options["night_avg_window_end"],
        state_path=DAYNIGHT_SNAPSHOT_PATH,
        now=datetime.now(),
    )


def _on_grace_check(bridge: _Bridge, event: Event) -> None:
    """Abo-Fristende waehrend der Laufzeit (Final-Review I-1/I-2): erst wiederherstellen,
    dann den Trigger-Client stoppen und den Worker mit Exit 0 beenden. Scheitert die
    Wiederherstellung, laeuft die lokale Regelung weiter und der naechste Check versucht
    es erneut."""
    bridge.worker.schedule(_local_check_interval(bridge.options), Event(EV_GRACE_CHECK))
    if not _abo_grace_expired(bridge.failsafe_ctx):
        return
    if _finish_abo_grace(
        bridge.manifest, bridge.ha_api, bridge.options, bridge.failsafe_ctx, always_restore=True, final_notice=True,
    ):
        if bridge.trigger_client is not None:
            bridge.trigger_client.stop()
        bridge.worker.request_exit(0)
        return
    logger.error(
        "Abo-inaktiv-Frist abgelaufen, zuletzt gelernte Werte konnten nicht wiederhergestellt "
        "werden - Notbetrieb laeuft weiter, erneuter Versuch beim naechsten Check"
    )


def _register_handlers(bridge: _Bridge) -> None:
    handlers = {
        EV_LOCAL_CHECK: _on_local_check,
        EV_SETPOINTS: _on_setpoints,
        EV_AUTH_REJECTED: _on_auth_rejected,
        EV_ACK_TIMEOUT: _on_ack_timeout,
        EV_RETRY_DUE: _on_retry_due,
        EV_WATCHDOG: _on_watchdog,
        EV_TELEMETRY: _on_telemetry,
        EV_DAYNIGHT: _on_daynight,
        EV_GRACE_CHECK: _on_grace_check,
    }
    for kind, handler in handlers.items():
        bridge.worker.register(kind, functools.partial(handler, bridge))


def _run_telemetry_tick(manifest, ha_api, mqtt_client, boost_active: bool, failsafe_active: bool) -> None:
    """Publiziert den KPI-Telemetrie-Snapshot (Design-Spec 2026-09-16 KPI-Erfassung). Den
    Takt (telemetry_interval_seconds) gibt der Planeintrag EV_TELEMETRY vor (Design-Spec
    2026-09-26); eine eigene Drosselung gibt es nicht mehr (Bewusste Abweichung 15).
    Liest room_actual selbst -- ein lokaler HA-REST-Aufruf, kein Cloud-Roundtrip."""
    if "room_actual" not in manifest.entity_ids:
        return
    try:
        room_actual = ha_api.get_state(manifest.entity_ids["room_actual"])
        _publish_telemetry(
            mqtt_client=mqtt_client, room_actual=room_actual,
            boost_active=boost_active, failsafe_active=failsafe_active,
            kpi_fields=_read_kpi_fields(manifest, ha_api),
        )
    except Exception:
        logger.exception(
            "Fehler beim Veroeffentlichen der KPI-Telemetrie, wird beim naechsten Tick erneut versucht"
        )


def _publish_telemetry(
    mqtt_client, room_actual: float, boost_active: bool, failsafe_active: bool, kpi_fields: dict | None = None,
) -> None:
    """Nicht retained, nur QoS 1 (siehe BridgeMqttClient.publish_telemetry): reine
    KPI-Beobachtung ohne Einfluss auf curve.py oder eine Regelentscheidung."""
    mqtt_client.publish_telemetry({
        "room_actual": room_actual,
        "boost_active": boost_active,
        "failsafe_active": failsafe_active,
        "ts": datetime.now().isoformat(),
        **(kpi_fields or {}),
    })


def _read_kpi_fields(manifest, ha_api) -> dict:
    """Reads the configured optional KPI sensors, each in isolation: an unavailable/
    unknown/non-numeric sensor is omitted (with a warning naming the role) and never
    blocks the core telemetry or the other sensors. `energy` is only set if at least
    one channel could be read."""
    kpi_fields: dict = {}

    def _read(role, reader):
        try:
            value = reader(manifest.entity_ids[role])
            # "nan"/"inf" pass float() but the server rejects non-finite JSON numbers
            # for the whole message.
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"nicht-endlicher Wert {value!r}")
            return True, value
        except Exception as exc:
            logger.warning("KPI-Sensor '%s' nicht lesbar, Feld wird weggelassen: %s", role, exc)
            return False, None

    for role in _KPI_NUMERIC_ROLES:
        if role in manifest.entity_ids:
            ok, value = _read(role, ha_api.get_state)
            if ok:
                kpi_fields[role] = value
    if "operating_mode" in manifest.entity_ids:
        ok, value = _read("operating_mode", ha_api.get_raw_state)
        if ok:
            kpi_fields["operating_mode"] = value

    energy: dict = {}
    for role in _KPI_ENERGY_ROLES:
        if role in manifest.entity_ids:
            ok, value = _read(role, ha_api.get_state)
            if ok:
                energy[role.removeprefix("energy_")] = value
    if energy:
        kpi_fields["energy"] = energy
    return kpi_fields


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


def _make_trigger_event_callback(manifest, worker: RegulationWorker):
    """WS-Thread-Callback: stellt nur einen (koaleszierten) lokalen Check ein. Ob der
    (debounced) room_target-Trigger selbst gefeuert hat, wird ueber entity_id UND attribute
    erkannt: room_actual und room_target koennen dieselbe physische Entity referenzieren
    (z.B. ein Thermostat mit current_temperature/temperature), nur das attribute-Feld
    unterscheidet dann die beiden Trigger (Design-Spec 2026-09-22)."""
    room_target_entity_id = None
    room_target_attribute = None
    if "room_target" in manifest.entity_ids:
        room_target_entity_id = _strip_attribute_suffix(manifest.entity_ids["room_target"])
        room_target_attribute = _extract_attribute_suffix(manifest.entity_ids["room_target"])

    def _on_trigger_event(trigger: dict) -> None:
        room_target_fired = (
            room_target_entity_id is not None
            and trigger.get("platform") == "state"
            and trigger.get("entity_id") == room_target_entity_id
            and trigger.get("attribute") == room_target_attribute
        )
        worker.post_coalesced(EV_LOCAL_CHECK, room_target_fired=room_target_fired)
    return _on_trigger_event


def _state_trigger(raw_entity_id: str) -> dict:
    """State-Trigger fuer eine gemappte Rolle; mit `attribute`, wenn die Rolle als
    `<entity>::<attribut>` gemappt ist (B7: gilt jetzt auch fuer room_actual)."""
    trigger = {"platform": "state", "entity_id": _strip_attribute_suffix(raw_entity_id)}
    attribute = _extract_attribute_suffix(raw_entity_id)
    if attribute:
        trigger["attribute"] = attribute
    return trigger


def _build_ha_trigger_client(bridge) -> HaTriggerClient:
    manifest = bridge.manifest
    triggers = []
    if "room_target" in manifest.entity_ids:
        triggers.append({
            **_state_trigger(manifest.entity_ids["room_target"]),
            "for": {"seconds": ROOM_TARGET_DEBOUNCE_SECONDS},
        })
    if "room_actual" in manifest.entity_ids:
        triggers.append(_state_trigger(manifest.entity_ids["room_actual"]))
    daily_trigger_time = bridge.options.get("daily_trigger_time")
    if daily_trigger_time:
        triggers.append({"platform": "time", "at": daily_trigger_time})

    worker = bridge.worker
    return HaTriggerClient(
        ws_url=bridge.ha_api.websocket_url(), token=bridge.ha_api.token, triggers=triggers,
        on_trigger_event=_make_trigger_event_callback(manifest, worker),
        # B7: bei jeder (Re-)Verbindung Cache frisch lesen UND lokal pruefen -- eine
        # Sollwertaenderung waehrend der Trennung wird so sofort verarbeitet.
        on_connected=lambda: worker.post_coalesced(EV_LOCAL_CHECK, room_target_fired=True),
    )


def _make_setpoints_callback(worker: RegulationWorker):
    """paho-Callback fuer down/setpoints: parst nur und stellt die Antwort in den
    Regel-Worker ein. Retained Nachrichten (Broker-Replay beim (Re-)Subscribe, siehe
    2026-09-22-heizungsbruecke-retained-down-replay-fix-design.md) und Nicht-JSON-Objekte
    werden hier verworfen."""
    def _callback(client, userdata, message):
        try:
            if message.retain:
                logger.info("Retained Setpoints-Nachricht beim (Re-)Subscribe uebersprungen (Broker-Replay)")
                return
            payload = json.loads(message.payload)
            if not isinstance(payload, dict):
                logger.warning("Setpoints-Nachricht ist kein JSON-Objekt, verworfen: %r", payload)
                return
            worker.post(Event(EV_SETPOINTS, {"payload": payload}))
        except Exception:
            logger.exception("Setpoints-Nachricht nicht lesbar, verworfen")
    return _callback


def _create_mqtt_client(options: dict, worker: RegulationWorker, notbetrieb: bool) -> BridgeMqttClient:
    """Baut den MQTT-Client (B10: verbindet asynchron, kein Retry-Budget, kein Exit).
    Discovery, Status und Subscription werden gespeichert und bei jedem (Re-)Connect
    erneut gesendet; die Auth-Ablehnung aus dem paho-Thread wird nur eingestellt."""
    client = BridgeMqttClient(
        host=MQTT_HOST, port=MQTT_PORT, tenant_id=options["tenant_id"],
        username=options["mqtt_username"], password=options["mqtt_password"],
        on_auth_rejected=lambda _client: worker.post(Event(EV_AUTH_REJECTED)),
    )
    client.publish_discovery(
        component="binary_sensor", object_id="failsafe",
        config=delivery.build_discovery_config(options["tenant_id"]),
    )
    client.publish_status("failsafe", delivery.build_state_payload(notbetrieb))
    client.subscribe_setpoints(on_message=_make_setpoints_callback(worker))
    return client


def _prime(bridge: _Bridge) -> None:
    """Boost-Active-Bootstrap-Fix (Sicherheits-Review-Fund): der erste lokale Check laeuft
    synchron VOR mqtt.loop_start(), damit backup.json["boost_active"] nach einem Neustart
    frisch aus echten Sensorwerten bestimmt ist, bevor eine Setpoints-Antwort verarbeitet
    werden kann (handle_down_message, Design-Spec Abschnitt D)."""
    try:
        bridge.stable_target = _read_room_target_live(bridge.manifest, bridge.ha_api)
        logger.info("Stable-Target-Cache initial befuellt (Boot-Priming): room_target=%s", bridge.stable_target)
        bridge.boost_active = _run_local_check(
            bridge.manifest, bridge.ha_api, bridge.options, bridge.boost_active,
            room_target=bridge.stable_target, failsafe_ctx=bridge.failsafe_ctx,
        )
    except Exception:
        logger.exception("Fehler beim initialen lokalen Check vor MQTT-Start, wird beim naechsten Ereignis erneut versucht")
        # Fail-open (whole-branch review finding): backup.json["boost_active"] must not be
        # left at a stale pre-restart value -- a stuck stale-True value can gate out a
        # down-message and leave the live device pinned at a boost value.
        _save_boost_active_if_changed(False, BACKUP_PATH)
        bridge.boost_active = False
        bridge.failsafe_ctx["emergency_boost_active"] = False
        _save_emergency_active_if_changed(False, BACKUP_PATH)


def _start_bridge(options: dict, ha_api, clock=time.monotonic) -> "_Bridge | int":
    """Boot-Ablauf (Design-Spec 2026-09-26, Abschnitt 1). Gibt den gestarteten `_Bridge`
    zurueck oder einen Exit-Code, wenn gar nicht erst geregelt wird: 0 = nicht
    konfiguriert (ab 0.6.0 ein normaler Zustand, siehe _is_configured) oder Abo-Frist
    bereits abgeschlossen, 1 = Konfigurationsfehler."""
    if not _is_configured(options):
        logger.info(
            "Add-on ist noch nicht eingerichtet -- bitte die SmartHeat-Integration in "
            "Home Assistant installieren und dort die Verbindung zu diesem Add-on "
            "einrichten (sie schreibt die Konfiguration automatisch per Supervisor-API). "
            "Die Regelung startet erst, sobald options.json vollstaendig ist, und "
            "danach automatisch beim naechsten Neustart des Add-ons."
        )
        return 0

    try:
        options = _resolve_effective_options(options)
    except UnknownProfileError as error:
        logger.error("FEHLER: %s", error)
        return 1

    for validate in (
        _validate_boost_config, _validate_local_check_interval,
        _validate_telemetry_interval, _validate_derived_sensor_prerequisites,
    ):
        error = validate(options)
        if error:
            logger.error("FEHLER: %s", error)
            return 1

    derived_entity_ids = _ensure_derived_sensors_with_retry(ha_api, options)
    _check_timezone(ha_api)

    try:
        manifest = build_manifest(options, derived_entity_ids)
    except ManifestError as error:
        logger.error("FEHLER: %s", error)
        return 1

    failsafe_ctx = _load_failsafe_ctx_safe(FAILSAFE_PATH, BACKUP_PATH)
    notify_service = options.get("notify_service", "")
    # Abo-Status (B8, Design-Spec 2026-09-25, Abschnitt 4): bewusst erst hier, weil
    # Abschluss-Start und lokaler Modus Manifest und Clamps brauchen. "unknown"
    # (accounts-api nicht erreichbar) startet normal -- fail-open.
    now = datetime.now().astimezone()
    abo_status = entitlement.query_status(options["tenant_id"], ACCOUNTS_API_BASE_URL)
    if abo_status == entitlement.ACTIVE:
        entitlement.clear(ENTITLEMENT_PATH)
    elif abo_status == entitlement.INACTIVE:
        inactive_since = entitlement.load_inactive_since(ENTITLEMENT_PATH)
        if inactive_since is not None and entitlement.grace_expired(inactive_since, now):
            # Final-Review I-2: scheitert die Wiederherstellung (HA/Cloud beim Booten noch
            # nicht erreichbar), nicht mit liegengebliebenen Boost-Werten beenden.
            while not _finish_abo_grace(
                manifest, ha_api, options, failsafe_ctx, always_restore=False, final_notice=False,
            ):
                retry_seconds = _local_check_interval(options)
                logger.error(
                    "Abo-inaktiv-Frist abgelaufen, Boost-Werte konnten nicht zurueckgesetzt werden - "
                    "erneuter Versuch in %s s", retry_seconds,
                )
                time.sleep(retry_seconds)
            return 0
        _enter_abo_inactive(failsafe_ctx, None, ha_api, notify_service, now)

    bridge = _Bridge(
        manifest=manifest, ha_api=ha_api, options=options, derived_entity_ids=derived_entity_ids,
        worker=RegulationWorker(clock=clock), failsafe_ctx=failsafe_ctx,
    )
    _register_handlers(bridge)
    if not _abo_inactive(failsafe_ctx):
        bridge.mqtt_client = _create_mqtt_client(options, bridge.worker, failsafe_ctx["delivery"].notbetrieb)

    _prime(bridge)
    if bridge.mqtt_client is not None:
        bridge.mqtt_client.loop_start()

    bridge.trigger_client = _build_ha_trigger_client(bridge)
    bridge.trigger_client.start()
    for kind in (EV_WATCHDOG, EV_TELEMETRY, EV_DAYNIGHT, EV_GRACE_CHECK):
        bridge.worker.schedule(0, Event(kind))
    if not _abo_inactive(failsafe_ctx):
        # Offenen Tick aus failsafe_state.json sofort mit derselben seq erneut versuchen.
        _deliver(bridge, delivery.Boot())
    return bridge


def _run_bridge(options: dict, ha_api) -> int:
    """Laeuft synchron im Hauptthread (siehe main()) und gibt den Exit-Code zurueck: 0 =
    nicht konfiguriert oder Abo-Frist regulaer abgeschlossen (ohne `watchdog` in
    config.yaml bleibt das Add-on dann gestoppt), 1 = Konfigurationsfehler.
    Voruebergehende Fehler (HA noch nicht bereit, Broker nicht erreichbar, Server
    schweigt) beenden das Add-on nie (Design-Spec 2026-09-26, "Verbleibende Exits")."""
    started = _start_bridge(options, ha_api)
    if isinstance(started, int):
        return started
    return started.worker.run()


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

    exit_code = _run_bridge(options, ha_api)
    if exit_code:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
