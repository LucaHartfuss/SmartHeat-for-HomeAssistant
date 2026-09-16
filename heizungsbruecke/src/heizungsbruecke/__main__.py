import json
import logging
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
from heizungsbruecke.manifest import ManifestError, build_manifest
from heizungsbruecke.mqtt_client import BridgeMqttClient
from heizungsbruecke.profiles import UnknownProfileError, resolve_boost_defaults, resolve_local_clamps

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
    return {
        **options,
        "curve_min": clamps.curve_min,
        "curve_max": clamps.curve_max,
        "offset_min": clamps.offset_min,
        "offset_max": clamps.offset_max,
        "boost_threshold_k": boost.threshold_k,
        "boost_curve_value": boost.curve_value,
        "boost_offset_value": boost.offset_value,
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


def _check_failsafe_staleness(failsafe_ctx: dict, stale_after_seconds: float, mqtt_client, failsafe_path: Path) -> None:
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
        failsafe_ctx["state"] = new_state
        _save_failsafe_ctx(failsafe_ctx, failsafe_path)


def _run_tick(manifest, ha_api, mqtt_client, options, write_lock, boost_was_active: bool) -> bool:
    """Runs one poll cycle: publish the snapshot, then (if room roles are configured)
    evaluate and apply the local boost decision. Returns the boost-active state to
    carry into the next tick. An individual unreadable sensor only costs that role its
    snapshot value (handled inside publish_snapshot); any remaining I/O failure
    propagates -- the caller (_run_bridge's loop) is responsible for catching and
    logging so a single bad tick doesn't kill the whole process.
    """
    seq = str(uuid.uuid4())
    publish_snapshot(
        manifest=manifest,
        ha_api=ha_api,
        mqtt_client=mqtt_client,
        seq=seq,
        notify_service=options.get("notify_service", ""),
    )
    logger.info("Snapshot veroeffentlicht, seq=%s", seq)

    if "room_actual" in manifest.entity_ids and "room_target" in manifest.entity_ids:
        room_actual = ha_api.get_state(manifest.entity_ids["room_actual"])
        room_target = ha_api.get_state(manifest.entity_ids["room_target"])
        # backup.json is also written from the MQTT down-message callback thread (see
        # _make_down_callback/handle_down_message), so every read-modify-write of it --
        # including last_room_target below -- must happen under write_lock like every
        # other backup.json access in this module, not just the apply_boost_decision call.
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

    return boost_was_active


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

    write_lock = threading.Lock()

    failsafe_ctx = _load_failsafe_ctx_safe(FAILSAFE_PATH)
    if failsafe_ctx["last_valid_update"] is None:
        # Fresh install / no prior record: measure staleness from process start, so a
        # server that never sends a single valid value still trips fail-safe eventually
        # instead of reading "OK" forever.
        failsafe_ctx["last_valid_update"] = time.time()
    stale_after_seconds = options.get("failsafe_stale_after_hours", 26.0) * 3600

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
        mqtt_client.loop_start()
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

    boost_was_active = False

    while True:
        try:
            boost_was_active = _run_tick(manifest, ha_api, mqtt_client, options, write_lock, boost_was_active)
        except Exception:
            logger.exception("Fehler im Poll-Loop, wird beim naechsten Tick erneut versucht")

        try:
            with write_lock:
                _check_failsafe_staleness(failsafe_ctx, stale_after_seconds, mqtt_client, FAILSAFE_PATH)
        except Exception:
            logger.exception("Fehler bei der Fail-Safe-Staleness-Pruefung, wird beim naechsten Tick erneut versucht")

        try:
            daynight_snapshot.maybe_snapshot(
                ha_api=ha_api,
                room_12h_avg_entity_id=derived_entity_ids["_room_12h_avg"],
                day_avg_entity_id=derived_entity_ids["room_day_avg"],
                night_avg_entity_id=derived_entity_ids["room_night_avg"],
                state_path=DAYNIGHT_SNAPSHOT_PATH,
                now=datetime.now(),
            )
        except Exception:
            logger.exception("Fehler beim Tag-/Nachtmittel-Snapshot, wird beim naechsten Tick erneut versucht")

        time.sleep(options.get("poll_interval_seconds", 3600))


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
