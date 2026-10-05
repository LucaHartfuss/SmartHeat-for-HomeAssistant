"""HA-Host der Laufzeit (Spec SHG 3.3, Plan SHG G1 Praezisierung 6): Optionen aus options.json, Warten auf Home
Assistant, Hilfs-Entities, Hersteller-Bindings und Trigger ueber HA. Die Laufzeit selbst (Start, Handler, Ruhezustand,
Abmelden) liegt hostneutral in smartheat_runtime.app."""
import functools
import logging
import os
import time

from heizungsbruecke import config, derived_sensors, triggers
from heizungsbruecke.derived_sensors import DerivedSensors
from heizungsbruecke.ha_binding import binding_for, binding_roles
from heizungsbruecke.ha_signals import HaSignalSource
from heizungsbruecke.ha_sinks import HaNotifySink, HaStatusSink
from heizungsbruecke.manifest import build_manifest, entity_ref
from heizungsbruecke.version import ADDON_VERSION
from smartheat_core import wallclock
from smartheat_core.binding import BINDINGS
from smartheat_runtime.app import Loaded, Notice, RestoreParts, StartFailure
from smartheat_runtime.ports import NotifySink, SignalSource, StatusSink, TriggerSource
from smartheat_runtime.roles import ChannelManifest, ManifestError
from smartheat_runtime.runtime_config import BootInfo
from smartheat_runtime.worker import RegulationWorker

# Das Add-on startet mit `startup: services`, evtl. vor HA Core. Solange HA nicht antwortet,
# wird unbegrenzt gewartet (B10). Erst bei erreichbarem HA zaehlt das Budget fuer fehlende
# Entities und nicht anlegbare Hilfs-Entities (~4 min, Cloud-Integrationen laden spaet);
# danach ist es ein Startfehler (Spec TP6 3.6).
HA_REACHABILITY_DELAYS_SECONDS = (5, 10, 20, 40, 60)
DERIVED_SENSORS_RETRY_DELAYS_SECONDS = (5, 10, 20, 40, 60, 60, 60)
REDACTED = "***"
SOURCE_CHANGE_MESSAGE = "SmartHeat: Die Quelle der Raum- oder Außentemperatur hat sich geändert."
NOT_CONFIGURED_HINT = (
    "Add-on ist noch nicht eingerichtet -- bitte die SmartHeat-Integration in "
    "Home Assistant installieren und dort die Verbindung zu diesem Add-on "
    "einrichten (sie schreibt die Konfiguration automatisch per Supervisor-API und "
    "startet das Add-on danach neu). Bis dahin bleibt das Add-on im Ruhezustand."
)

logger = logging.getLogger(__name__)


class StartupError(Exception):
    """Startfehler bei erreichbarem HA nach Ablauf des Budgets (Spec TP6 3.6). `key` ist die
    stabile Identitaet des Fehlers fuer den Meldezustand: der Fehlertext enthaelt oft
    laufzeitabhaengige Details (Flow-ID von HA, Objektadressen), die bei jedem Neustart anders
    waeren und sonst jedes Mal eine neue Meldung ausloesten."""

    def __init__(self, grund: str, key: str) -> None:
        super().__init__(grund)
        self.key = key


class _MissingEntities(Exception):
    pass


class HaHost:
    """Host der Laufzeit im HA-Add-on (Spec SHG 3.3): Optionen aus options.json, Signale, Status und Meldungen ueber
    die HA-API, Hersteller-Bindings ueber HA-Entities, Trigger ueber den HA-WebSocket."""

    def __init__(self, options: dict, ha_api) -> None:
        self._options = options
        self._effective: dict | None = None
        self._ha_api = ha_api
        # Als Port-Typen deklariert: Attribute eines Protocols sind invariant (pyright).
        self.signals: SignalSource = HaSignalSource(ha_api)
        self.status_sink: StatusSink = HaStatusSink(ha_api)
        self.notify_sink: NotifySink = HaNotifySink(ha_api, config.notify_services(options))

    def boot_info(self) -> BootInfo:
        options = self._options
        # Abmelden braucht keine Zugangsdaten (die Integration leert sie beim Entfernen), nur den Tenant: ein noch
        # scheiterndes Zuruecksetzen laeuft so auch nach einem Pi-Neustart weiter.
        signed_off = config.is_signed_off(options) and bool(options.get("tenant_id"))
        configured = config.is_configured(options)
        if not signed_off and not configured:
            logger.info(NOT_CONFIGURED_HINT)
        return BootInfo(
            configured=configured, signed_off=signed_off,
            tenant_id=options.get("tenant_id"), setup_id=options.get("setup_id"),
            lever_set=_status_lever_set(options), client_version=ADDON_VERSION,
            notify_hints_off=tuple(config.notify_hints_off(options)),
            local_check_interval=config.local_check_interval(options),
            backup_path=config.BACKUP_PATH, failsafe_path=config.FAILSAFE_PATH,
        )

    def wait_until_ready(self) -> None:
        _wait_until_reachable(self._ha_api)

    def sign_off_parts(self) -> RestoreParts | None:
        """Es wird kein Hilfssensor angelegt: fuer das Zuruecksetzen reichen die Hebel und die Clamps."""
        try:
            effective = config.resolve_effective_options(self._options)
        except config.ConfigError as error:
            logger.error(
                "Abmelden ohne Zuruecksetzen, Konfiguration ungueltig: %s",
                _without_credentials(str(error), self._options),
            )
            return None
        roles = [role for role in binding_roles(config.binding_description(effective)) if effective.get(f"entity_{role}")]
        manifest = ChannelManifest(entity_ids={role: entity_ref(role, effective[f"entity_{role}"]) for role in roles})
        return RestoreParts(binding_for(effective, self._ha_api, manifest), config.local_safety(effective))

    def load(self) -> Loaded:
        options = self._options
        try:
            options = config.resolve_effective_options(options)
            config.resolve_transport(options)
            error = config.validate(options)
            if error:
                raise config.ConfigError(error)
            _wait_for_required_entities(self._ha_api, options)
            derived = _ensure_derived_sensors_with_retry(self._ha_api, options)
            manifest = build_manifest(options, derived.entity_ids, config.lever_set_id(options))
        except StartupError as error:
            raise StartFailure(str(error), error.key) from error
        except (config.ConfigError, ManifestError) as error:
            # Pruefungstexte sind deterministisch: der Text ist zugleich die Identitaet.
            raise StartFailure(_without_credentials(str(error), options)) from error
        _check_timezone(self._ha_api)
        self._effective = options
        notices = (Notice("quellwechsel", derived.sources_fingerprint, SOURCE_CHANGE_MESSAGE),) if derived.replaced else ()
        return Loaded(
            config=config.runtime_config(options), manifest=manifest,
            binding=binding_for(options, self._ha_api, manifest), notices=notices,
        )

    def trigger_source(self, manifest: ChannelManifest, worker: RegulationWorker) -> TriggerSource:
        assert self._effective is not None  # load() lief vorher
        return triggers.build_ha_trigger_client(manifest, self._effective, self._ha_api, worker)


def _wait_until_reachable(ha_api) -> None:
    attempt = 0
    while not ha_api.is_reachable():
        if attempt == 0:
            logger.info("Home Assistant ist noch nicht erreichbar, warte ...")
        time.sleep(HA_REACHABILITY_DELAYS_SECONDS[min(attempt, len(HA_REACHABILITY_DELAYS_SECONDS) - 1)])
        attempt += 1


def _retry_with_budget(ha_api, attempt, describe, *, identify=None, redact=lambda text: text):
    """Ruft attempt() bis zum Erfolg. Ein Fehler bei nicht erreichbarem HA wartet unbegrenzt,
    ohne das Budget zu verbrauchen; bei erreichbarem HA gilt DERIVED_SENSORS_RETRY_DELAYS_SECONDS,
    danach StartupError(describe(letzter Fehler), identify(letzter Fehler)); ohne identify ist
    der Text selbst die Identitaet. `redact` gilt fuer Log, Text und Identitaet."""
    used = 0
    while True:
        try:
            return attempt()
        except Exception as error:
            if not ha_api.is_reachable():
                _wait_until_reachable(ha_api)
                continue
            if used >= len(DERIVED_SENSORS_RETRY_DELAYS_SECONDS):
                grund = redact(describe(error))
                key = redact(identify(error)) if identify is not None else grund
                raise StartupError(grund, key) from error
            logger.warning("Start noch nicht moeglich (Versuch %s/%s): %s",
                           used + 1, len(DERIVED_SENSORS_RETRY_DELAYS_SECONDS) + 1, redact(str(error)))
            time.sleep(DERIVED_SENSORS_RETRY_DELAYS_SECONDS[used])
            used += 1


def _wait_for_required_entities(ha_api, options: dict) -> None:
    """Pflicht-Entities muessen existieren; `unavailable` ist kein Startfehler (das behandeln
    Datenfehler und Notbetrieb im Betrieb)."""
    keys = config.required_entity_options(config.lever_set_id(options))
    refs = [options[key] for key in keys] + list(options["room_sensors"])
    entity_ids = sorted({ref.partition("::")[0] for ref in refs})

    def _check():
        missing = [entity_id for entity_id in entity_ids if not ha_api.entity_exists(entity_id)]
        if missing:
            raise _MissingEntities(", ".join(missing))

    def _describe(error):
        if isinstance(error, _MissingEntities):
            return f"Entity fehlt in Home Assistant: {error}"
        return f"Entities konnten nicht geprüft werden: {error}"

    def _identify(error):
        if isinstance(error, _MissingEntities):
            return f"entity_fehlt:{error}"  # sortiert, also stabil
        return "entities_nicht_pruefbar"

    _retry_with_budget(
        ha_api, _check, _describe, identify=_identify, redact=functools.partial(_without_credentials, options=options),
    )


def _ensure_derived_sensors_with_retry(ha_api, options: dict) -> DerivedSensors:
    return _retry_with_budget(
        ha_api,
        lambda: derived_sensors.ensure_all(
            ha_api=ha_api,
            tenant_id=options["tenant_id"],
            room_sensors=options["room_sensors"],
            outdoor_source=options["entity_outdoor_temp"],
            state_path=config.DERIVED_SENSORS_PATH,
        ),
        lambda error: f"Hilfs-Entities konnten nicht angelegt werden: {error}",
        identify=lambda error: "hilfs_entities",
        redact=functools.partial(_without_credentials, options=options),
    )


def _without_credentials(text: str, options: dict) -> str:
    """Startfehler zitieren ungueltige Optionswerte (!r) und landen im Status-Event, in Meldungen und im
    Log. Steht ein Geheimnis (MQTT-Passwort, privater Schluessel, Installations-Token) versehentlich in so
    einem Wert, wird es unkenntlich gemacht, auch in der von repr() maskierten Form (Regel 6)."""
    for key in config.SECRET_OPTIONS:
        secret = options.get(key)
        if isinstance(secret, str) and secret:
            for variant in (secret, repr(secret)[1:-1]):
                text = text.replace(variant, REDACTED)
    return text


def _check_timezone(ha_api) -> None:
    """Taegliche Zeitpunkte (Tagestick) laufen in der Container-Zeitzone.
    Weicht sie von der HA-Zeitzone ab, nur warnen; kein Abbruch."""
    try:
        ha_time_zone = ha_api.get_config().get("time_zone")
    except Exception as error:
        logger.warning("Zeitzone von Home Assistant konnte nicht abgefragt werden: %s", error)
        return
    container_time_zone = os.environ.get("TZ") or str(wallclock.now().tzinfo)
    if ha_time_zone and ha_time_zone != container_time_zone:
        logger.warning(
            "Zeitzone weicht ab: Home Assistant '%s', Add-on-Container '%s' - taegliche "
            "Zeitpunkte laufen in der Container-Zeit.", ha_time_zone, container_time_zone,
        )
    else:
        logger.info("Zeitzone: %s", container_time_zone)


def _status_lever_set(options: dict):
    """Hebelsatz fuers Statusereignis. Ein unbekannter Hebelsatz ist ein Konfigurationsfehler (der Start meldet ihn
    gleich danach); das Statusereignis braucht dann trotzdem einen Satz, es faellt auf den Standard zurueck."""
    try:
        return config.binding_description(options).lever_set
    except config.ConfigError:
        return BINDINGS[config.DEFAULT_LEVER_SET].lever_set
