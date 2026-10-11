"""Laufzeit-Host des Gateways (Spec SHG G2 3.1; Spec 5b 5.2/5.3, Plan D2): Konfiguration aus dem Konfigurationsdokument
des Geraetekerns, Signale aus Zigbee, Soll-Speicher und Treiber, Status und Meldungen ueber den Gateway-Host des
Geraetekerns (geraet_host: im Prozess bzw. als up/notifications), Anlage ueber den Treiber, Trigger ueber den lokalen
Bus. Die Laufzeit selbst ist smartheat_runtime.app (identisch mit dem HA-Add-on). Die Konfiguration gilt fuer die
Lebenszeit des Prozesses; eine laufzeitrelevante neue startet ihn neu (runtime_main). Die Bedienung gibt den Startwert
des Soll-Speichers; weicht ein gespeicherter Wert ab (Bedienung kam waehrend eines Neustarts), gilt die Bedienung, ausser
ein lokaler Wunsch ist noch offen (dirty)."""
import dataclasses
import logging
import time

from smartheat_core import wallclock
from smartheat_core.safety import resolve_local_safety
from smartheat_gateway import config as gateway_config
from smartheat_gateway.bus import Bus
from smartheat_gateway.drivers import registry
from smartheat_gateway.geraet_host import GatewayDeviceHost
from smartheat_gateway.paths import Paths
from smartheat_gateway.raum import RaumPublisher
from smartheat_gateway.signals import REF_ROOM_MEAN, REF_ROOM_TARGET, GatewaySignalSource
from smartheat_gateway.target_store import SOURCE_PORTAL, TargetStore
from smartheat_gateway.texts import SHG_TEXTS
from smartheat_gateway.triggers import EV_TARGET_INPUT, BusTriggerSource
from smartheat_gateway.version import GATEWAY_VERSION
from smartheat_gateway.zigbee import CAP_BATTERY, CAP_BATTERY_LOW, ZigbeeMirror
from smartheat_runtime import dokument_config, options
from smartheat_runtime.app import Loaded, RestoreParts, StartFailure
from smartheat_runtime.options import ConfigError
from smartheat_runtime.ports import NotifySink, SignalSource, StatusSink, TriggerSource
from smartheat_runtime.roles import ALL_ROLES, REQUIRED_ROLES_BY_LEVER_SET, ChannelManifest, ManifestError
from smartheat_runtime.runtime_config import BATTERY_LOW_FLAG, BATTERY_PERCENT, BatteryRef, BootInfo
from smartheat_runtime.texts import HostTexts
from smartheat_runtime.worker import Event, RegulationWorker

logger = logging.getLogger(__name__)

# Stabile Fehlerklassen fuer den Meldezustand (G2 3.1): bei gleicher Klasse pusht dieselbe Meldung nicht neu.
KEY_CONFIG = "konfiguration"
KEY_MANIFEST = "manifest"
KEY_DRIVER = "treiber"
BATTERY_POLL_SECONDS = 0.2

NOT_CONFIGURED_HINT = "Gateway ist noch nicht eingerichtet - die Einrichtung laeuft im SmartHeat-Portal."


def build_manifest(driver) -> ChannelManifest:
    refs = {"room_actual": REF_ROOM_MEAN, "room_target": REF_ROOM_TARGET, **driver.signals()}
    lever_set = driver.description.lever_set.id
    missing = [role for role in REQUIRED_ROLES_BY_LEVER_SET[lever_set] if role not in refs]
    if missing:
        raise ManifestError(f"Treiber liefert Pflicht-Rollen nicht: {', '.join(missing)}")
    return ChannelManifest(refs={role: refs[role] for role in ALL_ROLES if role in refs})


def check_lever_set(driver, lever_set_id: str) -> None:
    """Binding (Treiber) und lokale Sicherheitswerte (Konfiguration) muessen denselben Hebelsatz meinen (Regel 4)."""
    driver_set = driver.description.lever_set.id
    if driver_set != lever_set_id:
        raise ConfigError(
            f"Hebelsatz des Treibers ({driver_set}) weicht von der Konfiguration ({lever_set_id}) ab – "
            f"{options.RECONFIGURE_HINT}"
        )


class GatewayHost:
    def __init__(self, paths: Paths, bus: Bus, mirror: ZigbeeMirror, geraet: GatewayDeviceHost, *,
                 clock=time.monotonic, devices_wait_seconds: float = 10.0, driver_threads: bool = True) -> None:
        self._paths, self._bus, self._clock = paths, bus, clock
        self._geraet = geraet
        self._devices_wait = devices_wait_seconds
        self._driver_threads = driver_threads
        self.konfiguration: dict = geraet.konfiguration() or {}
        self.mirror = mirror
        self._signals = GatewaySignalSource(bus, mirror)
        self.signals: SignalSource = self._signals
        self.status_sink: StatusSink = geraet.status_sink
        self.notify_sink: NotifySink = geraet.notify_sink
        self.texts: HostTexts = SHG_TEXTS
        self.driver = None
        self.raum: RaumPublisher | None = None
        self._store: TargetStore | None = None
        self._bindung: gateway_config.GatewayBindung | None = None
        self._runtime_config = None
        self._bedienung_start: float | None = None
        self._angleichen = False

    def boot_info(self) -> BootInfo:
        boot = dokument_config.boot_info(
            self.konfiguration, abgemeldet=gateway_config.abgemeldet(self._paths, self.konfiguration),
            client_version=GATEWAY_VERSION, backup_path=self._paths.backup, failsafe_path=self._paths.failsafe)
        if not boot.configured and not boot.signed_off:
            logger.info(NOT_CONFIGURED_HINT)
        return boot

    def wait_until_ready(self) -> None:
        while not self._bus.wait_connected(5):
            logger.info("Lokaler Bus noch nicht erreichbar, warte ...")
        self._paths.runtime_dir.mkdir(parents=True, exist_ok=True)

    def sign_off_parts(self) -> RestoreParts | None:
        hebelsatz = self.konfiguration.get("hebelsatz")
        try:
            if not isinstance(hebelsatz, str):
                raise ConfigError("Konfiguration ohne Hebelsatz")
            safety = resolve_local_safety(hebelsatz, self.konfiguration.get("verteilsystem"))
            driver = self._create_driver(gateway_config.bindung(self.konfiguration).driver_spec())
            check_lever_set(driver, hebelsatz)
        except (ConfigError, KeyError, TypeError, ValueError) as error:
            logger.error("Abmelden ohne Zuruecksetzen, Konfiguration ungueltig (%s)", type(error).__name__)
            return None
        self._activate(driver)
        return RestoreParts(driver, safety)

    def load(self) -> Loaded:
        try:
            runtime, bindung = gateway_config.parse_dokument(self.konfiguration, self._paths, self._geraet.abo_status)
        except (ConfigError, KeyError) as error:
            raise StartFailure(str(error), KEY_CONFIG) from error
        try:
            driver = self._create_driver(bindung.driver_spec())
        except (KeyError, TypeError, ValueError) as error:
            raise StartFailure(f"Treiber '{bindung.driver_id}' nicht startbar ({type(error).__name__})",
                               KEY_DRIVER) from error
        try:
            check_lever_set(driver, runtime.lever_set_id)
        except ConfigError as error:
            raise StartFailure(str(error), KEY_CONFIG) from error
        self._activate(driver)  # erste Abfrage vor dem Manifest: optionale Signale stehen dann im Cache (A4-32)
        try:
            manifest = build_manifest(driver)
        except ManifestError as error:
            driver.stop()  # kein Abfragen im Leerlauf, bis die Neupruefung den Treiber neu baut
            self.driver = None
            raise StartFailure(str(error), KEY_MANIFEST) from error
        bedienung, dirty = self._geraet.bedienung()
        start = gateway_config.raum_soll_start(bedienung)
        store = TargetStore(self._paths.room_target, start, self._clock, lambda: wallclock.now().isoformat(),
                            on_change=self._raum_changed)
        self._angleichen = bedienung is not None and not dirty
        self._bedienung_start = start if self._angleichen and store.value != start else None
        self._signals.bind(driver, store, bindung.room_sensors)
        runtime = dataclasses.replace(runtime, battery_refs=self._battery_refs(bindung))
        self._store, self._bindung, self._runtime_config = store, bindung, runtime
        self.raum = RaumPublisher(self._geraet.zustand, self.signals, store, lambda: wallclock.now().isoformat())
        return Loaded(config=runtime, manifest=manifest, binding=driver,
                      plant_id=registry.plant_id(bindung.driver_spec()))

    def trigger_source(self, manifest: ChannelManifest, worker: RegulationWorker) -> TriggerSource:
        assert self._store is not None and self._bindung is not None and self._runtime_config is not None
        bindung = self._bindung
        sensor_ieees = tuple(ref.split(":")[1] for ref in bindung.room_sensors if ref.startswith("zigbee:"))
        source = BusTriggerSource(
            self._bus, worker, self.mirror, self._store, thermostat=bindung.thermostat, sensor_ieees=sensor_ieees,
            daily_trigger_time=self._runtime_config.daily_trigger_time, wunsch=self._geraet.wunsch,
            soll_quelle_aus=self._geraet.soll_quelle_aus,
        )
        if self._bedienung_start is not None:
            worker.post(Event(EV_TARGET_INPUT, {"value": self._bedienung_start, "source": SOURCE_PORTAL}))
        elif self._angleichen and bindung.thermostat is not None:
            # Spec 3.3: Thermostat, Portal und Geraet zeigen denselben Wert (einmal je Start; Echo-Fenster).
            self.mirror.write_setpoint(bindung.thermostat, self._store.value)
            self._store.note_own_write()
        return source

    def _raum_changed(self) -> None:
        if self.raum is not None:
            self.raum.publish()

    def _create_driver(self, driver_spec: dict):
        return registry.create(
            driver_spec["id"], driver_spec.get("parameter") or {}, self._paths, clock=self._clock, writer=True,
        )

    def _activate(self, driver) -> None:
        """Erste Abfrage und Abfrage-Thread erst, wenn die Konfiguration vollstaendig geprueft ist."""
        try:
            driver.poll_once()
        except Exception:
            logger.exception("Erste Abfrage des Treibers fehlgeschlagen, die Laufzeit meldet Datenfehler")
        if self._driver_threads:
            driver.start()  # Tests treiben die Abfrage selbst (poll_once)
        self.driver = driver

    def _battery_refs(self, bindung: gateway_config.GatewayBindung) -> tuple[BatteryRef, ...]:
        ieees = [ref.split(":")[1] for ref in bindung.room_sensors if ref.startswith("zigbee:")]
        if bindung.thermostat:
            ieees.append(bindung.thermostat)
        # Begrenzt ueber die echte monotone Uhr, nicht ueber die eingespeiste: ein leeres bridge/devices darf nie endlos
        # warten (auch nicht mit einer stehenden Test-Uhr).
        deadline = time.monotonic() + self._devices_wait
        while ieees and not self.mirror.devices() and time.monotonic() < deadline:
            time.sleep(BATTERY_POLL_SECONDS)
        refs = []
        for ieee in dict.fromkeys(ieees):
            device = self.mirror.device(ieee)
            if device is None:
                logger.warning("Zigbee-Geraet %s unbekannt, keine Batterie-Ueberwachung", ieee)
            elif CAP_BATTERY in device.faehigkeiten:
                refs.append(BatteryRef(f"zigbee:{ieee}:battery", BATTERY_PERCENT))
            elif CAP_BATTERY_LOW in device.faehigkeiten:
                refs.append(BatteryRef(f"zigbee:{ieee}:battery_low", BATTERY_LOW_FLAG))
        return tuple(refs)
