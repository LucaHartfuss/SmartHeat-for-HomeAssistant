"""Bausteine fuer Laufzeit- und Szenariotests des Gateways ab 0.6.0 (Plan 5b D2): Geraetekern mit Fake-Link (der Zugang
liegt schon vor, kein Bootstrap), Gateway-Host, Laufzeit. Ein gewollter Neustart (SystemExit 0) baut wie Compose einen
neuen Prozess (neuer Zigbee-Spiegel, Geraetekern, Link und Laufzeit) auf denselben Dateien; der neue Link ist erst nach
connect() verbunden. start_runtime folgt runtime_main.main(): erst das Aufraeumen, dann der Geraetekern."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from configs import SENSOR, THERMOSTAT
from fake_ca import GeraeteCa
from fake_z2m import FakeZigbee2Mqtt
from fakes import FakeBus
from geraet_fakes import FakeLink, KeinBootstrap, zugang_ablegen

from smartheat_device.device import Device
from smartheat_gateway import befehle, runtime_main
from smartheat_gateway.geraet_host import GatewayDeviceHost
from smartheat_gateway.paths import Paths
from smartheat_gateway.zigbee import ZigbeeMirror

BERLIN = ZoneInfo("Europe/Berlin")
START = datetime(2026, 1, 15, 8, 0, tzinfo=BERLIN)


class FrozenWall:
    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = (self._now.astimezone(ZoneInfo("UTC")) + timedelta(seconds=seconds)).astimezone(BERLIN)


class GatewayWorld:
    def __init__(self, data_dir, monkeypatch, clock) -> None:
        self.paths = Paths(data_dir)
        self.host_dir = data_dir.parent / "host"
        self.clock = clock
        self.wall = FrozenWall(START)
        self.bus = FakeBus()
        self.z2m = FakeZigbee2Mqtt(self.bus)
        self.z2m.bridge(online=True)
        self.z2m.add_sensor(SENSOR)
        self.z2m.add_thermostat(THERMOSTAT)
        self.z2m.report(SENSOR, temperature=20.0, battery=90)
        self.ca = GeraeteCa()
        self.zugang = zugang_ablegen(self.paths.device_dir, self.ca)
        self.links: list[FakeLink] = []
        self.restarts = 0
        self.result = self.host = self.geraet = self.device = None
        monkeypatch.setenv("SHG_ALIVE_FILE", str(data_dir / "alive"))
        monkeypatch.setattr("smartheat_core.wallclock._now", self.wall.now)
        # abo.restart_process: conftest (_kein_prozessneustart) sichert das Original, attach leitet um.

    def _link(self, *args):
        self.links.append(FakeLink(*args))
        return self.links[-1]

    @property
    def link(self) -> FakeLink:
        return self.links[-1]

    def _wall(self) -> float:
        return self.wall.now().timestamp()

    def start_runtime(self):
        runtime_main.aufraeumen(self.paths)  # wie main(): bevor der Geraetekern startet
        mirror = ZigbeeMirror(self.bus, self.clock)
        mirror.start()
        self.geraet = GatewayDeviceHost(self.paths, mirror, host_dir=self.host_dir, clock=self.clock, wall=self._wall)
        self.device = Device(self.geraet, self.paths.data, "https://accounts.example.test", clock=self.clock,
                             wall=self._wall, bootstrap_client=lambda ident: KeinBootstrap(), link_factory=self._link,
                             befehl_fehler=befehle.fehler_uebersetzen)
        self.geraet.verbinden(self.device)
        runtime_main.software_soll_nachholen(self.geraet, self.device)  # wie main(): bevor der Geraete-Thread laeuft
        self.device.schritt()  # baut den Link (Zugang liegt vor)
        self.result, self.host = runtime_main.start(self.paths, self.bus, mirror, self.geraet, self.device,
                                                    clock=self.clock, driver_threads=False)
        self.restarts += 1
        # Der Fuehler meldet live (retained Werte aus dem Broker-Replay gelten nicht als frisch, Plan G2b-1).
        self.z2m.report(SENSOR, temperature=20.0, battery=90)
        return self.result

    def run(self) -> None:
        """Geraetekern und Worker abarbeiten; ein gewollter Neustart startet einen neuen Prozess wie Compose."""
        for _ in range(5):
            try:
                self.device.schritt()
                self.result.worker.run_pending()
                self.device.schritt()
                return
            except SystemExit as stop:
                assert stop.code in (0, None)
                self.start_runtime()
        raise AssertionError("Laufzeit startet staendig neu")

    def advance(self, seconds: float) -> None:
        self.clock.advance(seconds)
        self.wall.advance(seconds)
        if self.host is not None and self.host.driver is not None:
            self.host.driver.poll_once()
        self.run()

    def connect(self) -> None:
        self.link.verbinden()
        self.run()

    def server(self, name: str, body: dict) -> None:
        self.link.empfangen(name, body)
        self.run()

    def konfiguration_senden(self, inhalt: dict, version: int) -> None:
        self.server("down/config", {"schema": 1, "version": version, **inhalt})

    def bedienung_senden(self, inhalt: dict, version: int) -> None:
        self.server("down/operation", {"schema": 1, "version": version, **inhalt})

    def answer(self, levers: dict, status: str = "ok", reason=None) -> None:
        seq = self.link.of("up/snapshot")[-1]["seq"]
        self.server("down/setpoints", {"schema": 4, "seq": seq, "ts": "2026-01-15T00:00:00+01:00", "status": status,
                                       "reason": reason, "levers": levers, "learned": {}})

    def status(self) -> dict | None:
        return self.geraet.zustand.status if self.geraet is not None else None
