"""HA-Host der Laufzeit (Spec SHG 3.3, Spec 5c 1.3): Konfiguration aus dem Konfigurationsdokument des Geraetekerns,
Warten auf Home Assistant, Hilfs-Entities, Hersteller-Bindings und Trigger ueber HA; der Raum-Soll kommt aus dem
Soll-Speicher. Die Laufzeit selbst (Start, Handler, Ruhezustand, Abmelden) liegt hostneutral in smartheat_runtime.app.

Start des Soll-Speichers (wie das Gateway): die gespeicherte Datei, sonst die Bedienung des Servers, sonst der Wert, den
die Soll-Entity zeigt (im Bedienbereich), sonst DEFAULT_RAUM_SOLL. Weicht die Datei von einer nicht "dirty" Bedienung
ab (sie kam waehrend eines Neustarts), gilt die Bedienung. Bei einer Bedienung gleicht trigger_source die Soll-Entity
einmal an (Echo-Fenster), bevor der Trigger-Client verbindet: sonst wuerde das erste Lesen nach dem Verbinden den alten
Wert der Entity zum Wunsch machen, der die Bedienung ueberschreibt. Eine spaetere Bedienung wendet der Soll-Eingang an
(HaDeviceHost.bedienung_uebernommen). Den Raum fuer up/status (raum.RaumPublisher) legt der Worker bei jeder
Soll-Aenderung und alle RAUM_SECONDS beim Geraete-Host ab."""
import dataclasses
import logging
import os
import time

from heizungsbruecke import config, derived_sensors, dokument, geraet_trigger
from heizungsbruecke.derived_sensors import DerivedSensors
from heizungsbruecke.geraet_trigger import HaSollQuelle
from heizungsbruecke.ha_binding import binding_for, binding_roles
from heizungsbruecke.ha_signals import SOLL_REF, GeraetSignale, HaSignalSource
from heizungsbruecke.manifest import build_manifest, entity_ref
from heizungsbruecke.raum import RaumPublisher
from heizungsbruecke.version import ADDON_VERSION
from smartheat_core import wallclock
from smartheat_core.binding import BINDINGS
from smartheat_runtime import dokument_config
from smartheat_runtime.app import Loaded, Notice, RestoreParts, StartFailure
from smartheat_runtime.plausibility import ROOM_TEMP_RANGE, is_plausible
from smartheat_runtime.ports import NotifySink, SignalSource, StatusSink, TriggerSource
from smartheat_runtime.roles import ChannelManifest, ManifestError
from smartheat_runtime.runtime_config import BATTERY_LOW_FLAG, BATTERY_PERCENT, BatteryRef, BootInfo
from smartheat_runtime.soll_eingang import FROSTSCHUTZ_BIS
from smartheat_runtime.soll_speicher import BEDIENBEREICH_MAX, BEDIENBEREICH_MIN, TargetStore, im_bedienbereich
from smartheat_runtime.texts import HA_TEXTS, HostTexts
from smartheat_runtime.worker import Event, RegulationWorker

# Das Add-on startet mit `startup: services`, evtl. vor HA Core. Solange HA nicht antwortet,
# wird unbegrenzt gewartet (B10). Erst bei erreichbarem HA zaehlt das Budget fuer fehlende
# Entities und nicht anlegbare Hilfs-Entities (~4 min, Cloud-Integrationen laden spaet);
# danach ist es ein Startfehler (Spec TP6 3.6).
HA_REACHABILITY_DELAYS_SECONDS = (5, 10, 20, 40, 60)
DERIVED_SENSORS_RETRY_DELAYS_SECONDS = (5, 10, 20, 40, 60, 60, 60)
#: Start des Soll-Speichers, wenn weder eine gespeicherte Datei noch eine Bedienung vom Server noch ein lesbarer Wert der
#: Soll-Entity vorliegt.
DEFAULT_RAUM_SOLL = 20.0
#: Raum fuer up/status (wie das Gateway): Takt im Worker zusaetzlich zu jeder Soll-Aenderung.
EV_RAUM = "ha_raum"
RAUM_SECONDS = 30
SOURCE_CHANGE_MESSAGE = "SmartHeat: Die Quelle der Raum- oder Außentemperatur hat sich geändert."
NOT_CONFIGURED_HINT = (
    "Add-on ist noch nicht eingerichtet -- bitte in Home Assistant unter Einstellungen -> Geraete & Dienste -> "
    "SmartHeat einrichten. Bis dahin bleibt das Add-on im Ruhezustand."
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
    """Host der Laufzeit im HA-Add-on ab 1.0.0 (Spec 5c 1.3): Konfiguration aus dem Konfigurationsdokument des
    Geraetekerns (geraet), Signale ueber die HA-API mit dem Soll-Speicher als Raum-Soll, Hersteller-Bindings und
    Hilfssensoren wie bisher (Optionen aus dokument.optionen), Trigger ueber den HA-WebSocket."""

    def __init__(self, geraet, ha_api, *, clock=time.monotonic) -> None:
        self._geraet, self._ha_api, self._clock = geraet, ha_api, clock
        self._konfiguration = geraet.konfiguration() or {}
        self._effective: dict | None = None
        self.store: TargetStore | None = None
        self.soll_entity: str | None = None
        self.raum: RaumPublisher | None = None
        self._angleichen = False  # Start aus einer Bedienung: Soll-Entity einmal angleichen (trigger_source)
        # Als Port-Typen deklariert: Attribute eines Protocols sind invariant (pyright).
        self.signals: SignalSource = GeraetSignale(HaSignalSource(ha_api), lambda: self.store)
        self.status_sink: StatusSink = geraet.status_sink
        self.notify_sink: NotifySink = geraet.notify_sink_fuer(
            dokument.optionen(self._konfiguration)["notify_services"] if self._konfiguration else [])
        self.texts: HostTexts = HA_TEXTS

    def boot_info(self) -> BootInfo:
        boot = dokument_config.boot_info(
            self._konfiguration, abgemeldet=dokument_config.abgemeldet(config.ABGEMELDET_PATH, self._konfiguration),
            client_version=ADDON_VERSION, backup_path=config.BACKUP_PATH, failsafe_path=config.FAILSAFE_PATH)
        hints = dokument.optionen(self._konfiguration)["notify_hints_off"] if self._konfiguration else []
        boot = dataclasses.replace(boot, notify_hints_off=tuple(hints))
        if not boot.configured and not boot.signed_off:
            logger.info(NOT_CONFIGURED_HINT)
        return boot

    def wait_until_ready(self) -> None:
        _wait_until_reachable(self._ha_api)
        config.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    def sign_off_parts(self) -> RestoreParts | None:
        """Es wird kein Hilfssensor angelegt: fuer das Zuruecksetzen reichen die Hebel und die Clamps."""
        try:
            effective = dokument.optionen(self._konfiguration)
            description = config.binding_description(effective)
            safety = config.local_safety(effective)
        except config.ConfigError as error:
            logger.error("Abmelden ohne Zuruecksetzen, Konfiguration ungueltig: %s", error)
            return None
        roles = [role for role in binding_roles(description) if effective.get(f"entity_{role}")]
        manifest = ChannelManifest(refs={role: entity_ref(role, effective[f"entity_{role}"]) for role in roles})
        return RestoreParts(binding_for(effective, self._ha_api, manifest), safety)

    def load(self) -> Loaded:
        options = dokument.optionen(self._konfiguration)
        try:
            # Pflichtrollen und Schreibregeln des Hebelsatzes zuerst: _wait_for_required_entities und die Hilfssensoren
            # indizieren die Optionen, ein fehlender Schluessel waere sonst ein KeyError statt eines Konfigurationsfehlers.
            # Muster der Raumfuehler/Aussenquelle/Batterien/Handys und die Hinweise prueft config_check (Task 2) bzw. der
            # Server, nicht der Host.
            text = dokument.lokal_pruefen(self._konfiguration)
            if text:
                raise config.ConfigError(text)
            geraet_trigger.pruefe_soll_ref(dokument.soll_entity(self._konfiguration))
            runtime = dokument_config.runtime_config(
                self._konfiguration, entitlement_path=config.ENTITLEMENT_PATH, abo_source=self._geraet.abo_status)
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
            raise StartFailure(str(error)) from error
        _check_timezone(self._ha_api)
        self._effective = options
        self.soll_entity = dokument.soll_entity(self._konfiguration)
        assert self.soll_entity is not None  # lokal_pruefen verlangt sie
        bedienung, dirty = self._geraet.bedienung()
        start = bedienung.get("raum_soll") if isinstance(bedienung, dict) else None
        bedient = float(start) if isinstance(start, int | float) and im_bedienbereich(start) else None
        start_value = bedient if bedient is not None else _angezeigter_start(self._ha_api, self.soll_entity)
        self.store = TargetStore(config.ROOM_TARGET_PATH, start_value, self._clock, lambda: wallclock.now().isoformat(),
                                 on_change=self._raum_changed)
        self._angleichen = bedient is not None and not dirty
        if self._angleichen and self.store.value != start_value:
            self.store.apply_portal(start_value)  # Bedienung kam waehrend eines Neustarts (Spec 5b 3.3)
        self.raum = RaumPublisher(self._geraet.raum_merken, self.signals, self.store, manifest.refs.get("room_actual"),
                                  lambda: wallclock.now().isoformat())
        runtime = dataclasses.replace(
            runtime, notify_hints_off=tuple(options["notify_hints_off"]),
            battery_refs=tuple(
                BatteryRef(entity_id, BATTERY_LOW_FLAG if entity_id.startswith("binary_sensor.") else BATTERY_PERCENT)
                for entity_id in options["battery_entities"]))
        notices = (Notice("quellwechsel", derived.sources_fingerprint, SOURCE_CHANGE_MESSAGE),) if derived.replaced else ()
        return Loaded(config=runtime, manifest=manifest, binding=binding_for(options, self._ha_api, manifest),
                      notices=notices)

    def trigger_source(self, manifest: ChannelManifest, worker: RegulationWorker) -> TriggerSource:
        assert self._effective is not None and self.store is not None  # load() lief vorher
        geraet = self._geraet
        # eine Soll-Quelle fuer Start und Betrieb: scheitert das Angleichen beim Start, holt der Trigger es nach
        quelle = HaSollQuelle(self._ha_api, self.soll_entity) if self.soll_entity else None
        client = geraet_trigger.build(
            manifest, self._effective, self._ha_api, worker, self.store, self.soll_entity,
            wunsch=lambda wert, herkunft, durch: geraet.wunsch(wert, herkunft=herkunft, durch=durch),
            soll_quelle_aus=geraet.soll_quelle_aus, quelle=quelle)
        if self._angleichen and quelle is not None:
            self._soll_entity_angleichen(quelle)
        raum = self.raum
        assert raum is not None

        def _raum(event: Event) -> None:
            worker.schedule(RAUM_SECONDS, Event(EV_RAUM))  # zuerst: der Takt darf nie abreissen
            raum.publish()

        worker.register(EV_RAUM, _raum)
        worker.schedule(0, Event(EV_RAUM))
        return client

    def _soll_entity_angleichen(self, quelle: HaSollQuelle) -> None:
        """Spec 5c 1.3 "Bedienung angleichen" beim Start (Gateway: _angleichen): Thermostat, HA und Portal zeigen
        denselben Wert. Nur wenn die Entity etwas anderes zeigt und nicht aus ist (HVAC off oder Frostschutz-Stellung bis
        FROSTSCHUTZ_BIS = Soll-Quelle aus: keine Bedienung an die Quelle, Spec 5b 3.2; beim Wiedereinschalten gleicht
        der Soll-Eingang an). Danach gilt das Echo-Fenster; der
        zuletzt gezeigte Wert zaehlt als veraltet. Scheitert das Schreiben, holt der Trigger es beim Verbinden nach
        (quelle.angleichen_offen, geraet_trigger.build). Laeuft im Hauptthread vor dem Worker und vor dem
        Trigger-Client."""
        assert self.store is not None and self.soll_entity is not None
        entity_id = self.soll_entity.partition("::")[0]
        if not entity_id.startswith("climate."):
            return  # nur eine Climate-Entity ist schreibbar (HaSollQuelle)
        try:
            if self._ha_api.get_raw_state(entity_id) == "off":
                return
        except Exception as error:
            logger.warning("HVAC-Modus der Soll-Entity nicht gelesen (%s)", type(error).__name__)
        angezeigt = quelle.angezeigt()
        if angezeigt == self.store.value:
            return
        if angezeigt is not None and angezeigt <= FROSTSCHUTZ_BIS:
            return  # Frostschutz-Stellung ist ebenfalls "Soll-Quelle aus" (Spec 5b 3.2, SollEingang)
        if quelle.schreiben(self.store.value):
            self.store.note_own_write(veraltet=angezeigt)

    def _raum_changed(self) -> None:
        if self.raum is not None:
            self.raum.publish()


def _angezeigter_start(ha_api, soll_ref: str) -> float:
    """Start ohne Bedienung: der Wert der Soll-Entity, wenn plausibel und im Bedienbereich (15-25 °C), ohne Rasterzwang
    (gerastert wuerde das erste Lesen nach dem Verbinden ihn als Wunsch melden); sonst DEFAULT_RAUM_SOLL."""
    value = HaSollQuelle(ha_api, soll_ref).angezeigt()
    if value is not None and is_plausible(value, ROOM_TEMP_RANGE) and BEDIENBEREICH_MIN <= value <= BEDIENBEREICH_MAX:
        return value
    return DEFAULT_RAUM_SOLL


def _wait_until_reachable(ha_api) -> None:
    attempt = 0
    while not ha_api.is_reachable():
        if attempt == 0:
            logger.info("Home Assistant ist noch nicht erreichbar, warte ...")
        time.sleep(HA_REACHABILITY_DELAYS_SECONDS[min(attempt, len(HA_REACHABILITY_DELAYS_SECONDS) - 1)])
        attempt += 1


def _retry_with_budget(ha_api, attempt, describe, *, identify=None):
    """Ruft attempt() bis zum Erfolg. Ein Fehler bei nicht erreichbarem HA wartet unbegrenzt,
    ohne das Budget zu verbrauchen; bei erreichbarem HA gilt DERIVED_SENSORS_RETRY_DELAYS_SECONDS,
    danach StartupError(describe(letzter Fehler), identify(letzter Fehler)); ohne identify ist
    der Text selbst die Identitaet."""
    used = 0
    while True:
        try:
            return attempt()
        except Exception as error:
            if not ha_api.is_reachable():
                _wait_until_reachable(ha_api)
                continue
            if used >= len(DERIVED_SENSORS_RETRY_DELAYS_SECONDS):
                grund = describe(error)
                key = identify(error) if identify is not None else grund
                raise StartupError(grund, key) from error
            logger.warning("Start noch nicht moeglich (Versuch %s/%s): %s",
                           used + 1, len(DERIVED_SENSORS_RETRY_DELAYS_SECONDS) + 1, error)
            time.sleep(DERIVED_SENSORS_RETRY_DELAYS_SECONDS[used])
            used += 1


def _wait_for_required_entities(ha_api, options: dict) -> None:
    """Pflicht-Entities muessen existieren; `unavailable` ist kein Startfehler (das behandeln
    Datenfehler und Notbetrieb im Betrieb)."""
    keys = config.required_entity_options(config.lever_set_id(options))
    refs = [options[key] for key in keys if options[key] != SOLL_REF] + list(options["room_sensors"])
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

    _retry_with_budget(ha_api, _check, _describe, identify=_identify)


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
    )


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
