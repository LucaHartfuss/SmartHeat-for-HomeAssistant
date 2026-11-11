"""Simulations-Treiber: fuer E2E, Labor und Gleichheitstests; nie im Kundenkatalog.
Anlagenzustand in /data/sim/plant.json (schreibt nur die Laufzeit, writer=True; der Heizkreis n > 0 hat plant-<n>.json),
Fehlerinjektion in /data/sim/control.json (bei jeder Abfrage gelesen; `heizkreise: 2` macht auch den zweiten Kreis der
Erkennung schreibbar). Das Haus rechnet der Hausmodell-Kern (`smartheat_core/hausmodell.py`, Haus H1) in 5-min-Schritten
der monotonen Uhr; die Tageszeit bestimmt die Lieferung nicht (feste Stunde ausserhalb der Warmwasserpausen). Die
fachliche Zeit (`wall`, Vorgabe `zeitquelle.fachliche_epoche`) bestimmt nur den Tagesgang der Aussentemperatur und die
Wartezeit, bis ein geschriebener Hebel sichtbar wird; ein Sprung der Wanduhr ruehrt den Raum nicht an."""
import logging
import math
import random
import threading
import time
from collections.abc import Mapping

from smartheat_core import dateien, hausmodell
from smartheat_core.binding import BindingDescription
from smartheat_core.levers import LEVER_SETS
from smartheat_core.verfahren import VERFAHREN
from smartheat_core.zahlen import ist_endliche_zahl
from smartheat_core.zeitquelle import fachliche_epoche
from smartheat_gateway import safety_warnings
from smartheat_gateway.drivers.base import CACHE_MAX_POLLS, KIND_LOCAL, LEVER_ROLES, DriverError, erkennung_daten
from smartheat_gateway.paths import Paths
from smartheat_gateway.quota import QuotaExhausted

logger = logging.getLogger(__name__)

DEFAULTS = {
    "lever_set": "viessmann_vicare", "settle_delay": 0.0, "aussen_mittel": 5.0, "aussen_amplitude": 5.0,
    "rauschen": 0.0, "poll_seconds": 60.0, "raum_start": 20.0, "seed": 0,
}
START_LEVERS = {"curve": 1.0, "room_setpoint": 20.0, "level": 0.0, "heat_limit": 16.0, "min_flow": 20.0}
START_AUX = {"mode_select": "normal", "setpoint_comfort": 22.0, "setpoint_setback": 18.0}
# Bereiche und Erzeuger der simulierten Anlage (fuer die Erkennung; die lokalen Sicherheitswerte liegen in safety.py).
PLANT_RANGES = {
    "curve": (0.2, 3.5), "room_setpoint": (10.0, 30.0), "level": (-13.0, 40.0), "heat_limit": (10.0, 25.0),
    "min_flow": (15.0, 80.0),
}
GENERATOR = {"vaillant_vrc720": "gastherme", "viessmann_vicare": "gastherme", "weishaupt_wwp": "waermepumpe",
             "weishaupt_wwp_basis": "waermepumpe"}
# Schrittweiten der simulierten Anlage je Hebelsatz (Erkennung ohne Konfiguration); gleich den Bindungsparametern des
# Servers (Contract-Check (Vertrag V4)).
SIM_SCHRITTE = {
    "vaillant_vrc720": {"curve": 0.05, "room_setpoint": 0.5, "heat_limit": 0.1, "min_flow": 0.1},
    "weishaupt_wwp": {"curve": 0.05, "room_setpoint": 0.5, "heat_limit": 0.5},
    "weishaupt_wwp_basis": {"room_setpoint": 0.5},
    "viessmann_vicare": {"curve": 0.1, "level": 1.0, "room_setpoint": 1.0},
}
# Erzeuger je Hebelsatz als Potenzgesetz des Kerns. Vaillant hat die Werte des Servermodells; fuer Viessmann und Weishaupt
# gibt es dort noch keine gemessenen Werte, sie rechnen mit a = b = 1 (Platzhalterphysik).
ERZEUGER = {
    "vaillant_vrc720": hausmodell.Potenzgesetz(a=2.54, b=0.78),
    "viessmann_vicare": hausmodell.Potenzgesetz(a=1.0, b=1.0),
    "weishaupt_wwp": hausmodell.Potenzgesetz(a=1.0, b=1.0),
    "weishaupt_wwp_basis": hausmodell.Potenzgesetz(a=1.0, b=1.0),
}
KERN_HAUS = "H1"
KERN_STUNDE = 12.0  # ausserhalb der Warmwasserpausen: die Tageszeit darf die Lieferung nicht bestimmen
START_BRENNERSTUNDEN = 1000.0  # Betriebsstunden des simulierten Erzeugers
# Zahlenparameter: JSON liest NaN, Infinity und beliebig grosse Ganzzahlen; sie liessen die Abfrage ohne Pause laufen
# (Event.wait(NaN)) bzw. den Abfrage-Thread sterben und verfaelschten das Hausmodell.
ZAHL_PARAMETER = ("settle_delay", "aussen_mittel", "aussen_amplitude", "rauschen", "raum_start")
# Entscheidung 22: poll_seconds wie bei ViCare strenger (keine bool, kein Text) und mit Grenzen; unter 1 s lief die
# Abfrage fast ohne Pause, ueber einem Tag wirft Event.wait bei sehr grossen Werten OverflowError.
MIN_POLL_SECONDS = 1.0
MAX_POLL_SECONDS = 86400.0


MAX_HEIZKREIS = 999  # wie der Kandidat der Erkennung (simulation.heizkreis.<n>, hoechstens drei Stellen)


def _pruefe_heizkreis(parameter: Mapping) -> None:
    kreis = parameter.get("heizkreis", 0)
    if isinstance(kreis, bool) or not isinstance(kreis, int) or not 0 <= kreis <= MAX_HEIZKREIS:
        raise ValueError(f"heizkreis muss eine ganze Zahl von 0 bis {MAX_HEIZKREIS} sein")


def _pruefe_zahlen(parameter: Mapping) -> None:
    poll = parameter["poll_seconds"]
    if not ist_endliche_zahl(poll) or not MIN_POLL_SECONDS <= poll <= MAX_POLL_SECONDS:
        raise ValueError(f"poll_seconds muss zwischen {MIN_POLL_SECONDS:g} und {MAX_POLL_SECONDS:g} s liegen")
    for key in ZAHL_PARAMETER:
        try:
            ok = math.isfinite(float(parameter[key]))
        except (TypeError, ValueError, OverflowError):
            ok = False
        if not ok:
            raise ValueError(f"{key} muss eine endliche Zahl sein")


class SimulationDriver:
    driver_id = "simulation"
    kind = KIND_LOCAL
    login_kind = None
    LEVER_SETS = tuple(SIM_SCHRITTE)
    # Parameter, die die Anlage bestimmen (registry.plant_id).
    # Achtung: Eine Aenderung aendert die plant_id jeder Installation; beim Update gilt das als andere Anlage und setzt den anlagenbezogenen Zustand zurueck.
    IDENTITY_PARAMETERS = ("lever_set", "heizkreis")

    def __init__(
        self, parameter: dict, paths: Paths, *, clock=time.monotonic, writer: bool = False, wall=fachliche_epoche,
        description: BindingDescription | None = None,
    ) -> None:
        self._p = {**DEFAULTS, **(parameter or {})}
        if self._p["lever_set"] not in self.LEVER_SETS:
            raise ValueError("Hebelsatz der Simulation unbekannt")
        _pruefe_zahlen(self._p)
        _pruefe_heizkreis(self._p)
        self._heizkreis: int = self._p.get("heizkreis", 0)
        self._paths = paths
        self._clock = clock
        self._wall = wall
        self._writer = writer
        self.poll_seconds = float(self._p["poll_seconds"])
        self.lever_set_id: str = self._p["lever_set"]
        self._description = description
        self.physical_writes = 0
        self._cache: dict[str, float | str] = {}
        self._cache_at: float | None = None
        self._zustand = hausmodell.Anlagenzustand(
            room=float(self._p["raum_start"]), burner_hours=START_BRENNERSTUNDEN)
        self._last_model: float | None = None
        self._rest = 0.0  # Sekunden seit dem letzten vollen Schritt, noch nicht gerechnet
        self._letzter: hausmodell.Anlagenschritt | None = None
        self._random = random.Random(self._p["seed"])
        self._lock = threading.RLock()  # Thread-Vertrag wie die Cloud-Treiber
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def description(self) -> BindingDescription:
        """Beschreibung aus der Konfiguration; Befehle ohne Konfiguration (Erkennung, Inventur,
        Vergessen) duerfen sie nicht brauchen."""
        if self._description is None:
            raise RuntimeError("Treiber ohne Konfiguration")
        return self._description

    # --- Anlage ---

    def _plant_datei(self):
        """Anlagenzustand dieses Heizkreises: plant.json fuer den ersten, plant-<n>.json fuer jeden weiteren."""
        return self._paths.sim_dir / ("plant.json" if self._heizkreis == 0 else f"plant-{self._heizkreis}.json")

    def _plant(self) -> dict:
        plant = dateien.lesen_json(self._plant_datei())
        if isinstance(plant, dict):
            return plant
        levers = {lever: START_LEVERS[lever] for lever in self._levers()}
        plant = {
            "hebel": levers, "vorher": {}, "geschrieben_um": {}, "hilfswerte": dict(START_AUX), "vorbereitet": False,
        }
        if self._writer:
            dateien.schreiben_json(self._plant_datei(), plant, modus=dateien.LESBAR)
        return plant

    def _save(self, plant: dict) -> None:
        assert self._writer, "nur die Laufzeit schreibt den Anlagenzustand"
        dateien.schreiben_json(self._plant_datei(), plant, modus=dateien.LESBAR)

    def _control(self) -> dict:
        control = dateien.lesen_json(self._paths.sim_dir / "control.json")
        return control if isinstance(control, dict) else {}

    def _levers(self) -> tuple[str, ...]:
        """Alle Hebel der Anlage: gesendete und nur gelesene."""
        lever_set = LEVER_SETS[self._p["lever_set"]]
        readonly = self._description.readonly_levers if self._description else ()
        return lever_set.levers + readonly

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
        wall = self._wall()
        outdoor = self._outdoor(wall)
        levers = {lever: self._visible(plant, lever) for lever in self._levers()}
        hebel, mindestvorlauf = self._kern_eingaenge(levers)
        # Ohne Liefer-Signal lernt der Server nicht; `keine_waerme` in der Steuerdatei bildet einen
        # Waermeausfall nach.
        flags = hausmodell.Betriebsflags(liefert=not control.get("keine_waerme"))
        erzeuger = ERZEUGER[self.lever_set_id]
        gemessen = self._rechne(now, wall, erzeuger, hebel, mindestvorlauf, flags)
        # Sollvorlauf aus den aktuellen Hebeln (wie der Kern ihn im Heizbetrieb bildet); geliefert wird, wenn die Anlage
        # weder ausgefallen ist noch im letzten Schritt ohne Heizbetrieb war.
        flow = max(erzeuger.flow(hebel[0], hebel[1], outdoor), mindestvorlauf)
        delivers = flags.liefert and (self._letzter is None or self._letzter.geliefert)
        cache: dict[str, float | str] = {LEVER_ROLES[lever]: value for lever, value in levers.items()}
        cache.update({key: plant["hilfswerte"].get(key, START_AUX[key]) for key in self.description.aux_originals})
        cache.update({
            "outdoor_temp": round(outdoor, 2), "flow_setpoint": round(flow, 1),
            "flow_temperature": round(flow, 1) if delivers else 29.5,
            "room_temperature": round(gemessen, 2), "generator_hours": round(self._zustand.burner_hours, 3),
        })
        self._cache, self._cache_at = cache, now

    def _kern_eingaenge(self, levers: Mapping[str, float]) -> tuple[tuple[float, float, float], float]:
        """Hebel der Anlage -> Eingaben des Kerns: (Steigung, Verschiebung, Heizgrenze) und Mindestvorlauf. Die
        Verschiebung ist das Raum-Soll (bei Viessmann plus Niveau), der Mindestvorlauf der Hebel `min_flow`, sonst das
        Raum-Soll; fehlt ein Hebel im Hebelsatz, gilt sein Startwert."""
        raum_soll = levers.get("room_setpoint", START_LEVERS["room_setpoint"])
        verschiebung = raum_soll + (levers.get("level", START_LEVERS["level"]) if self.lever_set_id == "viessmann_vicare" else 0.0)
        hebel = (levers.get("curve", START_LEVERS["curve"]), verschiebung, levers.get("heat_limit", START_LEVERS["heat_limit"]))
        return hebel, levers.get("min_flow", raum_soll)

    def _rechne(self, jetzt, wall, erzeuger, hebel, mindestvorlauf, flags) -> float:
        """Rechnet die vollen 5-min-Schritte seit der letzten Abfrage (der Rest bleibt fuer die naechste liegen, hoechstens
        ein Tag je Abfrage) und liefert den Fuehlerwert des Raums. Wie viele Schritte laufen, bestimmt allein die
        monotone Uhr; die Aussentemperatur jedes Schritts folgt dem Tagesgang zu dessen Ende auf der fachlichen Zeit
        (`wall` ist das Ende des Rechenfensters, der Rest liegt dahinter)."""
        if self._last_model is not None:
            self._rest += max(jetzt - self._last_model, 0.0)
        self._last_model = jetzt
        schritte = int(self._rest // hausmodell.DT)
        self._rest -= schritte * hausmodell.DT
        rauschen = float(self._p["rauschen"])
        ende_letzter = wall - self._rest
        gerechnet = min(schritte, hausmodell.STEPS_PER_DAY)
        for k in range(gerechnet):
            aussen = self._outdoor(ende_letzter - (gerechnet - 1 - k) * hausmodell.DT)
            self._letzter = hausmodell.anlage_schritt(
                self._zustand, hausmodell.HOUSES[KERN_HAUS], erzeuger, hebel, aussen, 0.0, KERN_STUNDE, mindestvorlauf, flags,
                self._random if rauschen else None)
        if not rauschen or self._letzter is None or self._letzter.gemessen is None:
            return self._zustand.room
        # Der Kern rauscht mit fester Staerke; `rauschen` skaliert diese Abweichung.
        return self._zustand.room + (self._letzter.gemessen - self._zustand.room) * rauschen / hausmodell.NOISE_K

    def _outdoor(self, wall: float) -> float:
        hours = (wall / 3600.0) % 24
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

    def aux_zulaessig(self, name: str, wert: str) -> bool:
        return name in self.description.aux_originals and isinstance(wert, str) and 0 < len(wert) <= 64

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
        roles = [LEVER_ROLES[lever] for lever in self._levers()] + list(VERFAHREN[("simulation", self._p["lever_set"])].aux_originals)
        roles += ["outdoor_temp", "flow_setpoint", "flow_temperature", "room_temperature", "generator_hours"]
        return {role: f"treiber:{role}" for role in roles}

    def read_signal(self, role: str) -> float | str:
        return self._fresh()[role]

    def login_begin(self, params: dict) -> dict:
        raise DriverError("login_nicht_noetig")

    def login_finish(self, params: dict) -> None:
        raise DriverError("login_nicht_noetig")

    def erkennung(self) -> dict:
        """Inventur stunden 0: zwei Heizkreise als Features, der erste (mit `heizkreise: 2` in der Steuerdatei auch der
        zweite) mit schreibbarer Heizkurve."""
        steuer = self._control()
        if steuer.get("token_expired"):
            raise DriverError("nicht_angemeldet")
        kreise = steuer.get("heizkreise")
        zweiter_schreibbar = type(kreise) is int and kreise == 2
        plant = self._plant()
        lever_set = self._p["lever_set"]
        hebel = {lever: {"wert": self._visible(plant, lever), "min": PLANT_RANGES[lever][0],
                         "max": PLANT_RANGES[lever][1], "schritt": SIM_SCHRITTE[lever_set][lever]}
                 for lever in LEVER_SETS[lever_set].levers}

        def feature(n: int, schreibbar: bool) -> dict:
            properties = {"schreibbar": {"value": schreibbar}, "erzeuger_typ": {"value": GENERATOR[lever_set]}}
            if schreibbar:
                properties |= {"hebel": {"value": hebel}, "signale": {"value": sorted(self.signals())}}
            return {"anlage": 0, "feature": f"simulation.heizkreis.{n}", "properties": properties, "commands": {}}

        kennung = {key: self._p[key] for key in ("lever_set", "settle_delay", "poll_seconds")}
        warnungen = safety_warnings.compute({"lever_set": lever_set, "hebel": hebel})
        return erkennung_daten(self.driver_id, [{"kennung": kennung, "sicherheitswarnungen": {"0": warnungen}}],
                               [feature(0, True), feature(1, zweiter_schreibbar)])

    def inventory_sample(self) -> dict:
        return {"hebel": dict(self._plant()["hebel"])}

    def inventory(self, hours: int, samples: list[dict]) -> dict:
        plant = self._plant()
        return {"driver_id": self.driver_id, "stunden": hours, "anlage": {
            "hebel": plant["hebel"], "hilfswerte": plant["hilfswerte"], "vorbereitet": plant["vorbereitet"],
        }, "proben": samples}

    def forget_credentials(self) -> None:
        """Die Simulation hat keine Zugangsdaten."""
