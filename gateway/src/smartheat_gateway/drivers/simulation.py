"""Simulations-Treiber (Spec SHG G2 4.3): fuer E2E, Labor und Gleichheitstests; nie im Kundenkatalog (G3 3.3).
Anlagenzustand in /data/sim/plant.json (schreibt nur die Laufzeit, writer=True), Fehlerinjektion in
/data/sim/control.json (bei jeder Abfrage gelesen). Hausmodell bewusst einfach, nicht physikalisch."""
import logging
import math
import random
import threading
import time
from collections.abc import Mapping

from smartheat_core.binding import BINDINGS, with_poll_interval
from smartheat_gateway.drivers.base import CACHE_MAX_POLLS, KIND_LOCAL, LEVER_ROLES, DriverError
from smartheat_gateway.files import read_json, write_json
from smartheat_gateway.paths import Paths
from smartheat_gateway.quota import QuotaExhausted

logger = logging.getLogger(__name__)

DEFAULTS = {
    "lever_set": "viessmann_vicare", "settle_delay": 0.0, "aussen_mittel": 5.0, "aussen_amplitude": 5.0,
    "rauschen": 0.0, "poll_seconds": 60.0, "raum_start": 20.0, "seed": 0,
}
START_LEVERS = {"curve": 1.0, "room_setpoint": 20.0, "level": 0.0, "heat_limit": 16.0, "min_flow": 20.0}
START_AUX = {"mode_select": "normal", "setpoint_comfort": 22.0, "setpoint_setback": 18.0}
# Bereiche und Erzeuger der simulierten Anlage (fuer probe; die lokalen Sicherheitswerte liegen in safety.py).
PLANT_RANGES = {
    "curve": (0.2, 3.5), "room_setpoint": (10.0, 30.0), "level": (-13.0, 40.0), "heat_limit": (10.0, 25.0),
    "min_flow": (15.0, 80.0),
}
GENERATOR = {"vaillant_vrc720": "gastherme", "viessmann_vicare": "gastherme", "weishaupt_wwp": "waermepumpe",
             "weishaupt_wwp_basis": "waermepumpe"}
TAU_SECONDS = 3 * 3600


class SimulationDriver:
    driver_id = "simulation"
    kind = KIND_LOCAL
    login_kind = None
    LEVER_SETS = tuple(BINDINGS)
    REJECTION_REASONS = ("hebel_fehlt",)

    def __init__(
        self, parameter: dict, paths: Paths, *, clock=time.monotonic, writer: bool = False, wall=time.time,
    ) -> None:
        self._p = {**DEFAULTS, **(parameter or {})}
        self._paths = paths
        self._clock = clock
        self._wall = wall
        self._writer = writer
        self.poll_seconds = float(self._p["poll_seconds"])
        self.quota = None
        self.description = with_poll_interval(BINDINGS[self._p["lever_set"]], self.poll_seconds)
        self.physical_writes = 0
        self._cache: dict[str, float | str] = {}
        self._cache_at: float | None = None
        self._room = float(self._p["raum_start"])
        self._last_model: float | None = None
        self._random = random.Random(self._p["seed"])
        self._lock = threading.RLock()  # Thread-Vertrag wie die Cloud-Treiber (Plan G4 Praezisierung 9)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- Anlage ---

    def _plant(self) -> dict:
        plant = read_json(self._paths.sim_dir / "plant.json")
        if isinstance(plant, dict):
            return plant
        levers = {lever: START_LEVERS[lever] for lever in self._levers()}
        plant = {
            "hebel": levers, "vorher": {}, "geschrieben_um": {}, "hilfswerte": dict(START_AUX), "vorbereitet": False,
        }
        if self._writer:
            write_json(self._paths.sim_dir / "plant.json", plant)
        return plant

    def _save(self, plant: dict) -> None:
        assert self._writer, "nur die Laufzeit schreibt plant.json"
        write_json(self._paths.sim_dir / "plant.json", plant)

    def _control(self) -> dict:
        control = read_json(self._paths.sim_dir / "control.json")
        return control if isinstance(control, dict) else {}

    def _levers(self) -> tuple[str, ...]:
        """Alle Hebel der Anlage: gesendete, client-abgeleitete (Vaillant: min_flow) und nur gelesene."""
        lever_set = self.description.lever_set
        return lever_set.levers + lever_set.client_derived + self.description.readonly_levers

    def _visible(self, plant: dict, lever: str) -> float:
        written = plant["geschrieben_um"].get(lever)
        if written is not None and self._wall() - written < float(self._p["settle_delay"]):
            return plant["vorher"].get(lever, plant["hebel"][lever])
        return plant["hebel"][lever]

    # --- Abfrage ---

    def poll_once(self) -> None:
        with self._lock:
            self._poll_locked()

    def _poll_locked(self) -> None:
        control = self._control()
        if control.get("offline") or control.get("quota_exhausted") or control.get("token_expired"):
            return  # kein Abruf: der Cache altert
        plant = self._plant()
        now = self._clock()
        outdoor = self._outdoor()
        levers = {lever: self._visible(plant, lever) for lever in self._levers()}
        equilibrium = (
            levers.get("room_setpoint", 20.0) + 4.0 * (levers.get("curve", 1.0) - 1.0) + 0.5 * levers.get("level", 0.0)
            - 0.1 * (15.0 - outdoor)
        )
        if self._last_model is not None:
            dt = max(now - self._last_model, 0.0)
            self._room += (equilibrium - self._room) * (1 - math.exp(-dt / TAU_SECONDS))
        self._last_model = now
        noise = self._random.gauss(0, float(self._p["rauschen"])) if self._p["rauschen"] else 0.0
        flow = (
            levers.get("room_setpoint", 20.0) + levers.get("curve", 1.0) * max(0.0, 20.0 - outdoor)
            + levers.get("level", 0.0)
        )
        cache: dict[str, float | str] = {LEVER_ROLES[lever]: value for lever, value in levers.items()}
        cache.update({key: plant["hilfswerte"].get(key, START_AUX[key]) for key in self.description.aux_originals})
        cache.update({
            "outdoor_temp": round(outdoor, 2), "flow_setpoint": round(flow, 1), "flow_temperature": round(flow, 1),
            "room_temperature": round(self._room + noise, 2),
        })
        self._cache, self._cache_at = cache, now

    def _outdoor(self) -> float:
        hours = (self._wall() / 3600.0) % 24
        swing = math.sin(2 * math.pi * (hours - 9) / 24)
        return float(self._p["aussen_mittel"]) + float(self._p["aussen_amplitude"]) * swing

    def _fresh(self) -> dict:
        if self._control().get("offline"):
            raise ConnectionError("Simulation offline")
        with self._lock:
            if self._cache_at is None or self._clock() - self._cache_at > CACHE_MAX_POLLS * self.poll_seconds:
                raise ValueError("Treiber-Cache veraltet")
            return self._cache

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="simulation-poll", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self.poll_once()
            except Exception:
                logger.exception("Simulation: Abfrage fehlgeschlagen")

    # --- PlantBinding ---

    def has(self, lever: str) -> bool:
        return lever in self._levers()

    def ref(self, lever: str) -> str:
        return f"treiber:{LEVER_ROLES[lever]}"

    def read(self, lever: str) -> float | None:
        return float(self._fresh()[LEVER_ROLES[lever]])

    def read_or(self, lever: str, fallback: float | None) -> float | None:
        try:
            return self.read(lever)
        except Exception as error:
            logger.warning("%s nicht lesbar (%s), verwende %s", lever, error, fallback)
            return fallback

    def write(self, lever: str, value: float) -> None:
        control = self._control()
        if control.get("offline"):
            raise ConnectionError("Simulation offline")
        if control.get("quota_exhausted"):
            raise QuotaExhausted("Kontingent erschöpft (Simulation)")
        if lever in control.get("write_fail", []):
            raise RuntimeError(f"Schreiben von {lever} abgelehnt (Simulation)")
        plant = self._plant()
        plant["vorher"][lever] = self._visible(plant, lever)
        plant["hebel"][lever] = value
        plant["geschrieben_um"][lever] = self._wall()
        self._save(plant)
        self.physical_writes += 1
        self.poll_once()

    def needs_preparation(self) -> bool:
        return self.description.prepared_lever is not None

    def is_prepared(self) -> bool:
        return bool(self._plant().get("vorbereitet"))

    def prepare(self) -> bool:
        if not self.needs_preparation() or self.is_prepared():
            return False
        plant = self._plant()
        plant["vorbereitet"] = True
        self._save(plant)
        self.physical_writes += 1
        return True

    def read_aux(self) -> dict[str, str | float]:
        cache = self._fresh()
        return {key: cache[key] for key in self.description.aux_originals}

    def limits(self, lever: str) -> tuple[float, float] | None:
        """Die Simulation hat keinen eigenen Wertebereich; es gelten die lokalen Grenzen."""
        return None

    def restore_aux(self, values: Mapping[str, str | float], levers: Mapping[str, float] | None = None) -> None:
        plant = self._plant()
        changed = {key: value for key, value in values.items()
                   if key in self.description.aux_originals and plant["hilfswerte"].get(key) != value}
        if changed:
            plant["hilfswerte"].update(changed)
            self._save(plant)
            self.physical_writes += len(changed)
            self.poll_once()

    # --- Treiber ---

    def signals(self) -> Mapping[str, str]:
        roles = [LEVER_ROLES[lever] for lever in self._levers()] + list(self.description.aux_originals)
        roles += ["outdoor_temp", "flow_setpoint", "flow_temperature", "room_temperature"]
        return {role: f"treiber:{role}" for role in roles}

    def read_signal(self, role: str) -> float | str:
        return self._fresh()[role]

    def login_begin(self, params: dict) -> dict:
        raise DriverError("login_nicht_noetig", "Die Simulation braucht keine Anmeldung.")

    def login_finish(self, params: dict) -> None:
        raise DriverError("login_nicht_noetig", "Die Simulation braucht keine Anmeldung.")

    def probe(self) -> dict:
        if self._control().get("token_expired"):
            raise DriverError("nicht_angemeldet", "Die Anmeldung beim Hersteller ist abgelaufen.")
        plant = self._plant()
        lever_set = self.description.lever_set.id
        public = {key: self._p[key] for key in ("lever_set", "settle_delay", "poll_seconds")}
        levers = {
            lever: {"wert": self._visible(plant, lever), "min": PLANT_RANGES[lever][0], "max": PLANT_RANGES[lever][1],
                    "schritt": self.description.steps[lever]}
            for lever in self.description.lever_set.levers
        }
        ok = {
            "kandidat_id": "sim-hk0", "anzeige": "Simulation · Heizkreis 0", "parameter": {**public, "heizkreis": 0},
            "erzeuger_typ": GENERATOR[lever_set], "lever_set": lever_set, "ablehnung": None, "hebel": levers,
            "signale": sorted(self.signals()), "sicherheitswarnungen": {}, "details": {"simulation": True},
        }
        rejected = {
            "kandidat_id": "sim-hk1", "anzeige": "Simulation · Heizkreis 1", "parameter": {**public, "heizkreis": 1},
            "erzeuger_typ": GENERATOR[lever_set], "lever_set": None,
            "ablehnung": {"grund": "hebel_fehlt", "text": "Heizkreis 1 hat keine schreibbare Heizkurve."},
            "hebel": {}, "signale": [], "sicherheitswarnungen": {}, "details": {"simulation": True},
        }
        return {"driver_id": self.driver_id, "kandidaten": [ok, rejected]}

    def inventory_sample(self) -> dict:
        return {"hebel": dict(self._plant()["hebel"])}

    def inventory(self, hours: int, samples: list[dict]) -> dict:
        plant = self._plant()
        return {"driver_id": self.driver_id, "stunden": hours, "anlage": {
            "hebel": plant["hebel"], "hilfswerte": plant["hilfswerte"], "vorbereitet": plant["vorbereitet"],
        }, "proben": samples}

    def forget_credentials(self) -> None:
        """Die Simulation hat keine Zugangsdaten."""
