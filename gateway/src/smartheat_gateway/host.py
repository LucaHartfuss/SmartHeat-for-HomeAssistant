"""SHG-Host der Laufzeit (Spec SHG G2 3.1): Konfiguration aus /data (Agent), Signale aus Zigbee/Soll-Speicher/Treiber,
Status und Meldungen auf den lokalen Bus, Anlage ueber den Treiber, Trigger ueber den Bus. Die Laufzeit selbst ist
smartheat_runtime.app (identisch mit dem HA-Add-on)."""
import dataclasses
import logging
import time

from smartheat_core import wallclock
from smartheat_gateway import config as gateway_config
from smartheat_gateway.bus import Bus
from smartheat_gateway.drivers import registry
from smartheat_gateway.paths import Paths
from smartheat_gateway.raum import RaumPublisher
from smartheat_gateway.signals import REF_ROOM_MEAN, REF_ROOM_TARGET, GatewaySignalSource
from smartheat_gateway.sinks import BusNotifySink, BusStatusSink
from smartheat_gateway.target_store import TargetStore
from smartheat_gateway.texts import SHG_TEXTS
from smartheat_gateway.triggers import BusTriggerSource
from smartheat_gateway.zigbee import CAP_BATTERY, CAP_BATTERY_LOW, ZigbeeMirror
from smartheat_runtime import options
from smartheat_runtime.app import Loaded, RestoreParts, StartFailure
from smartheat_runtime.options import ConfigError
from smartheat_runtime.ports import NotifySink, SignalSource, StatusSink, TriggerSource
from smartheat_runtime.roles import ALL_ROLES, REQUIRED_ROLES_BY_LEVER_SET, ChannelManifest, ManifestError
from smartheat_runtime.runtime_config import BATTERY_LOW_FLAG, BATTERY_PERCENT, BatteryRef, BootInfo
from smartheat_runtime.texts import HostTexts
from smartheat_runtime.worker import RegulationWorker

logger = logging.getLogger(__name__)

# Stabile Fehlerklassen fuer den Meldezustand (G2 3.1): bei gleicher Klasse pusht dieselbe Meldung nicht neu.
KEY_CONFIG = "konfiguration"
KEY_MANIFEST = "manifest"
KEY_DRIVER = "treiber"
BATTERY_POLL_SECONDS = 0.2

NOT_CONFIGURED_HINT = "Gateway ist noch nicht eingerichtet - die Einrichtung laeuft im SmartHeat-Portal."


def build_manifest(driver, gateway: "gateway_config.GatewayConfig") -> ChannelManifest:
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
    def __init__(self, paths: Paths, bus: Bus, *, clock=time.monotonic, devices_wait_seconds: float = 10.0,
                 driver_threads: bool = True) -> None:
        self._paths, self._bus, self._clock = paths, bus, clock
        self._devices_wait = devices_wait_seconds
        self._driver_threads = driver_threads
        self.raw = gateway_config.load_raw(paths)
        self.mirror = ZigbeeMirror(bus, clock)
        self.mirror.start()
        self._signals = GatewaySignalSource(bus, self.mirror)
        self.signals: SignalSource = self._signals
        self.status_sink: StatusSink = BusStatusSink(bus)
        self.notify_sink: NotifySink = BusNotifySink(bus, wallclock.now)
        self.texts: HostTexts = SHG_TEXTS
        self.driver = None
        self.raum: RaumPublisher | None = None
        self._store: TargetStore | None = None
        self._gateway: gateway_config.GatewayConfig | None = None
        self._runtime_config = None

    def boot_info(self) -> BootInfo:
        boot = gateway_config.boot_info(self.raw, self._paths)
        if not boot.configured and not boot.signed_off:
            logger.info(NOT_CONFIGURED_HINT)
        return boot

    def wait_until_ready(self) -> None:
        while not self._bus.wait_connected(5):
            logger.info("Lokaler Bus noch nicht erreichbar, warte ...")
        self._paths.runtime_dir.mkdir(parents=True, exist_ok=True)

    def sign_off_parts(self) -> RestoreParts | None:
        try:
            safety = options.local_safety(self.raw)
            spec = dict(self.raw.get("driver") or {})
            if self.raw.get(options.POLL_INTERVAL_OPTION) is not None:
                spec["parameter"] = {
                    **(spec.get("parameter") or {}), "poll_seconds": self.raw[options.POLL_INTERVAL_OPTION],
                }
            driver = self._create_driver(spec)
            check_lever_set(driver, options.lever_set_id(self.raw))
        except (ConfigError, KeyError, TypeError, ValueError) as error:
            logger.error("Abmelden ohne Zuruecksetzen, Konfiguration ungueltig: %s", self._redacted(str(error)))
            return None
        self._activate(driver)
        return RestoreParts(driver, safety)

    def load(self) -> Loaded:
        try:
            runtime, gateway = gateway_config.parse(self.raw, self._paths)
        except (ConfigError, KeyError) as error:
            raise StartFailure(self._redacted(str(error)), KEY_CONFIG) from error
        try:
            driver = self._create_driver(gateway.driver_spec())
        except (KeyError, TypeError, ValueError) as error:
            raise StartFailure(
                self._redacted(f"Treiber '{gateway.driver_id}' nicht startbar: {error!r}"), KEY_DRIVER,
            ) from error
        try:
            check_lever_set(driver, runtime.lever_set_id)
            manifest = build_manifest(driver, gateway)
        except ConfigError as error:
            raise StartFailure(self._redacted(str(error)), KEY_CONFIG) from error
        except ManifestError as error:
            raise StartFailure(self._redacted(str(error)), KEY_MANIFEST) from error
        self._activate(driver)
        store = TargetStore(self._paths.room_target, gateway.room_target_start, self._clock,
                            lambda: wallclock.now().isoformat(), on_change=self._raum_changed)
        self._signals.bind(driver, store, gateway.room_sensors)
        runtime = dataclasses.replace(runtime, battery_refs=self._battery_refs(gateway))
        self._store, self._gateway, self._runtime_config = store, gateway, runtime
        self.raum = RaumPublisher(self._bus, self.signals, store, lambda: wallclock.now().isoformat())
        return Loaded(config=runtime, manifest=manifest, binding=driver)

    def trigger_source(self, manifest: ChannelManifest, worker: RegulationWorker) -> TriggerSource:
        assert self._store is not None and self._gateway is not None and self._runtime_config is not None
        gateway = self._gateway
        sensor_ieees = tuple(ref.split(":")[1] for ref in gateway.room_sensors if ref.startswith("zigbee:"))
        return BusTriggerSource(
            self._bus, worker, self.mirror, self._store, thermostat=gateway.thermostat, sensor_ieees=sensor_ieees,
            daily_trigger_time=self._runtime_config.daily_trigger_time,
        )

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

    def _battery_refs(self, gateway) -> tuple[BatteryRef, ...]:
        ieees = [ref.split(":")[1] for ref in gateway.room_sensors if ref.startswith("zigbee:")]
        if gateway.thermostat:
            ieees.append(gateway.thermostat)
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

    def _redacted(self, text: str) -> str:
        return gateway_config.redact(text, self.raw)
