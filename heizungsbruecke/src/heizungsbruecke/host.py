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
from heizungsbruecke.version import ADDON_VERSION
from smartheat_core import config_check, wallclock
from smartheat_core.logtext import lesbar
from smartheat_runtime import dokument_config
from smartheat_runtime.app import Loaded, Notice, RestoreParts, StartFailure
from smartheat_runtime.dokument_config import RECONFIGURE_HINT, ConfigError
from smartheat_runtime.notifier import STATE_OK
from smartheat_runtime.plausibility import ROOM_TEMP_RANGE, is_plausible
from smartheat_runtime.ports import NotifySink, SignalSource, StatusSink, TriggerSource
from smartheat_runtime.raum import RaumPublisher
from smartheat_runtime.roles import ChannelManifest, ManifestError
from smartheat_runtime.runtime_config import BATTERY_LOW_FLAG, BATTERY_PERCENT, BatteryRef, BootInfo, RuntimeConfig
from smartheat_runtime.soll_eingang import FROSTSCHUTZ_BIS
from smartheat_runtime.soll_speicher import (
    BEDIENBEREICH_MAX,
    BEDIENBEREICH_MIN,
    DEFAULT_RAUM_SOLL,
    TargetStore,
    bedienung_start,
)
from smartheat_runtime.texts import HA_TEXTS, HostTexts
from smartheat_runtime.worker import Event, RegulationWorker

# Das Add-on startet mit `startup: services`, evtl. vor HA Core. Solange HA nicht antwortet,
# wird unbegrenzt gewartet (B10). Erst bei erreichbarem HA zaehlt das Budget fuer fehlende
# Entities und nicht anlegbare Hilfs-Entities (~4 min, Cloud-Integrationen laden spaet);
# danach ist es ein Startfehler (Spec TP6 3.6).
HA_REACHABILITY_DELAYS_SECONDS = (5, 10, 20, 40, 60)
DERIVED_SENSORS_RETRY_DELAYS_SECONDS = (5, 10, 20, 40, 60, 60, 60)
#: Raum fuer up/status (wie das Gateway): Takt im Worker zusaetzlich zu jeder Soll-Aenderung.
EV_RAUM = "ha_raum"
RAUM_SECONDS = 30
ENTITY_FEHLT_MESSAGE = ("SmartHeat: {feld}: zugeordnete Entity fehlt in Home Assistant und wird bis zum nächsten Start "
                        "nicht gelesen.")
ENTITY_DA_MESSAGE = "SmartHeat: {feld}: zugeordnete Entity ist in Home Assistant wieder vorhanden und wird gelesen."
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
    Hilfssensoren wie bisher (Bindung aus dokument.bindung), Trigger ueber den HA-WebSocket."""

    def __init__(self, geraet, ha_api, *, clock=time.monotonic) -> None:
        self._geraet, self._ha_api, self._clock = geraet, ha_api, clock
        # Spec Stufe 3 S3 Abschnitt 4: steht nach einer leeren Konfiguration das Zuruecksetzen aus, laeuft die Laufzeit
        # mit der alten Konfiguration aus der Aufraeum-Markierung im Modus abgemeldet.
        self._konfiguration, self._leer_abmelden = geraet.laufzeit_konfiguration()
        self._bindung: dokument.HaBindung | None = None
        self._runtime: RuntimeConfig | None = None
        self.store: TargetStore | None = None
        self.soll_entity: str | None = None
        self.raum: RaumPublisher | None = None
        self._angleichen = False  # Start aus einer Bedienung: Soll-Entity einmal angleichen (trigger_source)
        # Als Port-Typen deklariert: Attribute eines Protocols sind invariant (pyright).
        self.signals: SignalSource = GeraetSignale(HaSignalSource(ha_api), lambda: self.store)
        self.status_sink: StatusSink = geraet.status_sink
        vorab = self._bindung_oder_none()
        # A6-02: in die Senke nur Handy-Dienste (config_check.HA_DIENST); load lehnt die uebernommene Konfiguration mit
        # anderen Diensten ab, ihre Fehlermeldung soll sie aber nicht erst erreichen. Im Log nur die Anzahl.
        gebunden = vorab.benachrichtigung if vorab else []
        dienste = [d for d in gebunden if config_check.HA_DIENST.fullmatch(d)]
        if len(dienste) < len(gebunden):
            logger.warning("%s Benachrichtigungsdienste der Konfiguration sind keine Handy-Dienste und werden nicht "
                           "genutzt.", len(gebunden) - len(dienste))
        self.notify_sink: NotifySink = geraet.notify_sink_fuer(dienste)
        self.texts: HostTexts = HA_TEXTS

    def _bindung_oder_none(self) -> dokument.HaBindung | None:
        """Die Bindung fuer Meldedienste und Hinweise vor dem Laden: ohne Konfiguration oder mit unbekanntem Hebelsatz
        None. Der Host wirft hier nie, load meldet den Konfigurationsfehler."""
        if not self._konfiguration:
            return None
        try:
            return dokument.bindung(self._konfiguration)
        except ConfigError:
            return None

    def boot_info(self) -> BootInfo:
        abgemeldet = self._leer_abmelden or dokument_config.abgemeldet(config.ABGEMELDET_PATH, self._konfiguration)
        boot = dokument_config.boot_info(
            self._konfiguration, abgemeldet=abgemeldet, client_version=ADDON_VERSION, backup_path=config.BACKUP_PATH,
            failsafe_path=config.FAILSAFE_PATH, aufraeumen_path=config.AUFRAEUMEN_PATH)
        vorab = self._bindung_oder_none()
        boot = dataclasses.replace(boot, notify_hints_off=vorab.hinweise_aus if vorab else ())
        if not boot.configured and not boot.signed_off:
            logger.info(NOT_CONFIGURED_HINT)
        return boot

    def wait_until_ready(self) -> None:
        _wait_until_reachable(self._ha_api)
        config.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    def ursprungswerte_melden(self, werte: dict) -> None:
        self._geraet.ursprungswerte_merken(werte)

    def ursprungswerte_server(self) -> dict:
        return self._geraet.ursprungswerte_server(self._konfiguration, self._leer_abmelden)

    def sign_off_parts(self) -> RestoreParts | None:
        """Es wird kein Hilfssensor angelegt: fuer das Zuruecksetzen reichen die Hebel und die Clamps."""
        try:
            dokument_config.rastung_pruefen(self._konfiguration, config.RASTUNG_PATH)
            b = dokument.bindung(self._konfiguration)
            description = dokument_config.beschreibung(self._konfiguration)
            safety = dokument_config.sicherheit(self._konfiguration)
        except ConfigError as error:
            logger.error("Abmelden ohne Zuruecksetzen: %s", lesbar(str(error)))
            return None
        roles = [role for role in binding_roles(description) if b.rollen.get(role)]
        manifest = ChannelManifest(refs={role: entity_ref(role, b.rollen[role]) for role in roles})
        return RestoreParts(binding_for(description, self._ha_api, manifest), safety)

    def load(self) -> Loaded:
        try:
            # Pflichtrollen und Schreibregeln des Hebelsatzes zuerst: _wait_for_required_entities und die
            # Hilfssensoren indizieren die Rollen, ein fehlender Schluessel waere sonst ein KeyError statt eines
            # Konfigurationsfehlers. Muster der Raumfuehler/Aussenquelle/Batterien/Handys und die Hinweise prueft der
            # Server (config_check); die Positivliste und Formen der Rollen prueft der Host selbst weiter unten
            # (_bindung_pruefen, A6-02).
            text = dokument.lokal_pruefen(self._konfiguration)
            if text:
                raise ConfigError(text)
            b = dokument.bindung(self._konfiguration)
            geraet_trigger.pruefe_soll_ref(b.soll_entity)
            runtime = dokument_config.runtime_config(
                self._konfiguration, rastung_pfad=config.RASTUNG_PATH, entitlement_path=config.ENTITLEMENT_PATH,
                abo_source=self._geraet.abo_status)
            # Spec Stufe 3 S4b 4.3: ohne gueltige Bindungsparameter kein Start
            description = dokument_config.beschreibung(self._konfiguration)
            _wait_for_required_entities(self._ha_api, b)
            # A6-02, L4-27: die Regeln des Annehmens (Positivliste, Form je Feld, Schreibziele) gelten auch fuer eine
            # schon uebernommene Konfiguration, und zwar vor den Hilfssensoren (derived_sensors baut aus einer Rolle
            # schon ein Template in HA) und vor jedem Schreiben.
            _bindung_pruefen(self._konfiguration)
            ohne_zustand = _rollen_pruefen_mit_wiederholung(self._ha_api, b)
            derived = _ensure_derived_sensors_with_retry(self._ha_api, b, runtime.tenant_id)
            # optionale Lese-Rollen und Batterien ohne Zustand bleiben fuer diesen Lauf ungelesen (Spec 3.2 Nachtrag)
            ungelesen = {ref for _, ref in ohne_zustand}
            rollen = {rolle: ref for rolle, ref in b.rollen.items() if ref not in ungelesen}
            manifest = build_manifest({**rollen, "room_target": SOLL_REF}, derived.entity_ids, b.hebelsatz)
            binding = binding_for(description, self._ha_api, manifest)
        except StartupError as error:
            raise StartFailure(str(error), error.key) from error
        except (ConfigError, ManifestError) as error:
            # Pruefungstexte sind deterministisch: der Text ist zugleich die Identitaet.
            raise StartFailure(str(error)) from error
        _check_timezone(self._ha_api)
        self._bindung, self._runtime = b, runtime
        self.soll_entity = b.soll_entity
        assert self.soll_entity is not None  # lokal_pruefen verlangt sie
        bedienung, dirty = self._geraet.bedienung()
        bedient = bedienung_start(bedienung)
        start_value = bedient if bedient is not None else _angezeigter_start(self._ha_api, self.soll_entity)
        self.store = TargetStore(config.ROOM_TARGET_PATH, start_value, self._clock, lambda: wallclock.now().isoformat(),
                                 on_change=self._raum_changed)
        self._angleichen = bedient is not None and not dirty
        if self._angleichen and self.store.value != start_value:
            self.store.apply_portal(start_value)  # Bedienung kam waehrend eines Neustarts (Spec 5b 3.3)
        self.raum = RaumPublisher(self._geraet.raum_merken, self.signals, self.store, manifest.refs.get("room_actual"),
                                  lambda: wallclock.now().isoformat())
        runtime = dataclasses.replace(
            runtime, notify_hints_off=b.hinweise_aus,
            battery_refs=tuple(
                BatteryRef(entity_id, BATTERY_LOW_FLAG if entity_id.startswith("binary_sensor.") else BATTERY_PERCENT)
                for entity_id in b.batterien if entity_id not in ungelesen))
        notices: tuple[Notice, ...] = ()
        if derived.replaced:
            notices = (Notice("quellwechsel", derived.sources_fingerprint, SOURCE_CHANGE_MESSAGE),)
        fehlend = {feld for feld, _ in ohne_zustand}
        # L4-25: jedes gebundene optionale Feld meldet seinen Zustand; mit Zustand entwarnt der Melder einen offenen
        # Hinweis (und nur diesen), sonst bliebe „fehlt“ nach der Rueckkehr stehen und ein erneutes Fehlen schwiege.
        notices += tuple(
            Notice(f"entity_fehlt:{feld}", "fehlt", ENTITY_FEHLT_MESSAGE.format(feld=feld)) if feld in fehlend
            else Notice(f"entity_fehlt:{feld}", STATE_OK, ENTITY_DA_MESSAGE.format(feld=feld))
            for feld in dokument.optionale_felder(b))
        return Loaded(config=runtime, manifest=manifest, binding=binding,
                      notices=notices)

    def trigger_source(self, manifest: ChannelManifest, worker: RegulationWorker) -> TriggerSource:
        assert self._bindung is not None and self._runtime is not None and self.store is not None  # load() lief vorher
        geraet = self._geraet
        # eine Soll-Quelle fuer Start und Betrieb: scheitert das Angleichen beim Start, holt der Trigger es nach
        quelle = HaSollQuelle(self._ha_api, self.soll_entity) if self.soll_entity else None
        client = geraet_trigger.build(
            manifest, self._ha_api, worker, self.store, self.soll_entity,
            tagestick=self._runtime.daily_trigger_time, wache_s=self._runtime.local_check_interval,
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
        denselben Wert. Nur wenn die Entity etwas anderes zeigt und nicht aus ist (HVAC off oder Frostschutz-Stellung
        bis FROSTSCHUTZ_BIS = Soll-Quelle aus: keine Bedienung an die Quelle, Spec 5b 3.2; beim Wiedereinschalten
        gleicht der Soll-Eingang an). Danach gilt das Echo-Fenster; der
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
                           used + 1, len(DERIVED_SENSORS_RETRY_DELAYS_SECONDS) + 1, lesbar(str(error)))
            time.sleep(DERIVED_SENSORS_RETRY_DELAYS_SECONDS[used])
            used += 1


def _wait_for_required_entities(ha_api, b: dokument.HaBindung) -> None:
    """Pflicht-Entities muessen existieren; `unavailable` ist kein Startfehler (das behandeln
    Datenfehler und Notbetrieb im Betrieb)."""
    refs = [b.rollen[rolle] for rolle in dokument.PFLICHT_ROLLEN[b.hebelsatz]] + list(b.raumfuehler)
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


def _bindung_pruefen(konfiguration) -> None:
    """Form der uebernommenen HA-Bindung (config_check): lokaler Text mit dem Feld, nie mit der Entity (Regel 6)."""
    ablehnung = config_check.ha_bindung_pruefen(konfiguration)
    if ablehnung is not None:
        feld = (ablehnung.details or {}).get("feld", "bindung")
        raise ConfigError(f"{feld}: Form der Zuordnung ungültig – {RECONFIGURE_HINT}")


def _rollen_pruefen_mit_wiederholung(ha_api, b: dokument.HaBindung) -> list[tuple[str, str]]:
    """Lokale Positivliste fuer jede Rolle (A6-02) und die Regeln der Schreibziele (dokument.schreibziele_befund:
    Plattform des Hebelsatzes, ein Config-Entry, Soll-Entity nicht am Geraet eines Schreibziels; L4-27, A5-70). Ein
    Urteil ist ein Konfigurationsfehler; nicht pruefbar (HA nicht RUNNING, Soll-Entity, Pflichtrolle, Schreibziel oder
    Raumfuehler ohne Zustand, Schreibziel ohne Registry-Eintrag, Registry oder Zustaende scheitern) wird wie fehlende
    Entities wiederholt, nach dem Budget StartupError. Ergebnis: (Feld, Referenz) der optionalen Lese-Rollen und
    Batterien ohne Zustand, die der Lauf nicht liest. Das Zuruecksetzen beim Abmelden (sign_off_parts) prueft die
    Schreibziele nicht erneut: die Rueckkehr zu den Ursprungswerten ist die sichere Richtung."""
    def _versuch():
        if not ha_api.is_reachable():  # faehrt HA hoch, fehlen Zustaende noch: kein Urteil
            raise config_check.NichtPruefbar
        return dokument.rollen_befund(b, ha_api)  # NichtPruefbar und Fehler der HA-API: erneut versuchen

    def _describe(error):
        if isinstance(error, config_check.NichtPruefbar) and error.args:
            return f"{error.args[0]}: zugeordnete Entity fehlt in Home Assistant – {RECONFIGURE_HINT}"
        return "Home Assistant lieferte Registry oder Zustände nicht"

    verstoss, ohne_zustand = _retry_with_budget(
        ha_api, _versuch, _describe, identify=lambda error: "rollen_nicht_pruefbar")
    if verstoss is not None:
        raise ConfigError(f"{verstoss[0]}: diese Entity darf SmartHeat nicht nutzen (lokale Positivliste)")
    return ohne_zustand


def _ensure_derived_sensors_with_retry(ha_api, b: dokument.HaBindung, tenant_id: str) -> DerivedSensors:
    return _retry_with_budget(
        ha_api,
        lambda: derived_sensors.ensure_all(
            ha_api=ha_api,
            tenant_id=tenant_id,
            room_sensors=list(b.raumfuehler),
            outdoor_source=b.rollen["outdoor_temp"],
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
        logger.warning("Zeitzone von Home Assistant konnte nicht abgefragt werden: %s", lesbar(str(error)))
        return
    container_time_zone = os.environ.get("TZ") or str(wallclock.now().tzinfo)
    if ha_time_zone and ha_time_zone != container_time_zone:
        logger.warning(
            "Zeitzone weicht ab: Home Assistant '%s', Add-on-Container '%s' - taegliche "
            "Zeitpunkte laufen in der Container-Zeit.", lesbar(str(ha_time_zone)), container_time_zone,
        )
    else:
        logger.info("Zeitzone: %s", container_time_zone)
