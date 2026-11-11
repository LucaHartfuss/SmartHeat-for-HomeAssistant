"""Hausmodell-Kern der Abnahme: Haeuser, Wetter, Sonne und der Anlagenschritt (Hysterese, Warmwasserpausen,
Vorlauf mindestens Raum-Soll, Thermostatventil, Brennerzaehler, Raumgleichung). Nur Standardbibliothek: der Kern kennt
weder den Server noch eine Datenbank und kann so als Daten und Rechenvorschrift in andere Pruefwerkzeuge wandern.

Haus erster Ordnung, Heizkoerper ~ dT^n mit leistungsabhaengiger Spreizung, innere Gewinne, Sonne jeden zweiten Tag
(1,5 kW Spitze), Fuehlerrauschen 0,1 K, optional P-Thermostatventil. Der Erzeuger ist hier ein Datensatz
(`Potenzgesetz`), der die Rechenfolge des Servermodells (`PowerLawModel.flow`) Operation fuer Operation nachbildet; die
Abbildung vom Servermodell auf ihn steht in `simulation.py`."""
import math
import random
from dataclasses import dataclass
from datetime import timedelta
from typing import NamedTuple

TARGET = 20.5
STEP = timedelta(minutes=5)
DT = STEP.total_seconds()
STEPS_PER_DAY = int(timedelta(days=1) / STEP)
HYSTERESIS_K = 1.3
DHW_HOURS = ((7.0, 7.5), (19.0, 19.5))
SUN_PEAK = 1500.0
NOISE_K = 0.1
DAYS = 60


@dataclass(frozen=True)
class House:
    h: float = 150.0  # W/K
    gains: float = 300.0  # W
    k_radiator: float = 38.8  # W/K^n
    exponent: float = 1.3
    mass_flow_cp: float = 500.0  # W/K, Spreizung = P / (2 mcp) um die mittlere Heizkoerpertemperatur
    tau_h: float = 40.0
    valve_offset_k: float | None = None  # P-Thermostatventil: voll offen bis Soll + Offset - 0,5, zu bei + 0,5
    # House.soll ist das Raum-Soll der Physik (Ventil, Bilanz, Bedarf); der Parameter target von anlage_schritt ist
    # davon getrennt der Mindestvorlauf, den die Regelung vorgibt.
    soll: float = TARGET  # Raum-Soll, auf das Bilanztemperatur, Bedarf und Ventil bezogen sind

    @property
    def capacity(self) -> float:
        return self.h * self.tau_h * 3600

    @property
    def balance_temperature(self) -> float:
        return self.soll - self.gains / self.h

    def radiator(self, flow_temperature: float, room: float) -> float:
        if flow_temperature <= room:
            return 0.0
        power = self.k_radiator * (flow_temperature - room) ** self.exponent
        for _ in range(4):  # Fixpunkt fuer die Spreizung, kontrahiert schnell (Ableitung ~0,15)
            mean = flow_temperature - power / (2 * self.mass_flow_cp)
            power = self.k_radiator * max(mean - room, 0.0) ** self.exponent
        return power

    def required_flow(self, outdoor: float) -> float:
        power = max(self.h * (self.soll - outdoor) - self.gains, 0.0)
        return self.soll + (power / self.k_radiator) ** (1 / self.exponent) + power / (2 * self.mass_flow_cp)


# H1 Bilanztemperatur 18,5 Grad, H2 15,5 Grad, H3 17,5 Grad mit flacher Kurve.
HOUSES = {"H1": House(), "H2": House(gains=750.0, k_radiator=42.0), "H3": House(gains=450.0, k_radiator=60.0)}

# Fussbodenheizung: Estrich mit grosser Speichermasse (tau 90 h), Flaechenheizung ~ dT^1,1 mit
# kleiner Spreizung; Bedarf 33 Grad Vorlauf bei -10 Grad.
FBH_HOUSE = House(h=150.0, gains=300.0, k_radiator=340.0, exponent=1.1, mass_flow_cp=850.0, tau_h=90.0)


def weather(kind: str, days: int = DAYS, seed: int = 0) -> list[float]:
    """Tagesmittel der Aussentemperatur: zyklisch 4 +- 8 (10 Tage), Zufall (AR(1) um 6, -10..17), Uebergang
    12 +- 4 (9 Tage). Der Tagesgang +-5 K kommt in outdoor_at dazu."""
    rng = random.Random(seed)
    if kind == "zyklisch":
        return [4 + 8 * math.sin(2 * math.pi * day / 10) for day in range(days)]
    if kind == "uebergang":
        return [12 + 4 * math.sin(2 * math.pi * day / 9) for day in range(days)]
    if kind == "zufall":
        mean, result = 6.0, []
        for _ in range(days):
            mean = min(max(0.75 * mean + 0.25 * 6.0 + rng.gauss(0, 3.0), -10.0), 17.0)
            result.append(mean)
        return result
    raise ValueError(kind)


WEATHER = {
    "zyklisch": ("zyklisch", 0), "zufall1": ("zufall", 1), "zufall2": ("zufall", 2), "uebergang": ("uebergang", 0),
}


def outdoor_at(means: list[float], day: int, hour: float) -> float:
    return means[day] + 5.0 * math.sin(2 * math.pi * (hour - 9) / 24)


def sun_at(day: int, hour: float, sun: bool) -> float:
    if not sun or day % 2 or not 8 <= hour <= 16:
        return 0.0
    return SUN_PEAK * math.sin(math.pi * (hour - 8) / 8)


@dataclass(frozen=True)
class Potenzgesetz:
    """Erzeuger als Daten: Vorlauf(T; c, S) = a * (c * max(S - T, 0))^b + S, in derselben Rechenfolge wie das
    Potenzgesetz des Servers."""
    a: float
    b: float

    def flow(self, curve: float, shift: float, outdoor: float, target: float | None = None) -> float:
        return self.a * (curve * max(shift - outdoor, 0.0)) ** self.b + shift


@dataclass
class Anlagenzustand:
    """Veraenderlicher Zustand der simulierten Anlage: Raumtemperatur, Heizbetrieb (Hysterese) und die Zaehler des
    Brenners (Startwerte wie ein bereits gelaufener Erzeuger)."""
    room: float
    heating: bool = False
    burner_hours: float = 5000.0
    burner_starts: int = 20000
    burner_was_on: bool = False


@dataclass(frozen=True)
class Betriebsflags:
    """Was die Anlage in diesem Schritt tut: liefert = False heisst, die Therme liefert trotz Anforderung keine
    Waerme; warmwasser_brenner = der Brenner laeuft in den Warmwasserpausen auch ohne Heizbetrieb."""
    liefert: bool = True
    warmwasser_brenner: bool = False


class Anlagenschritt(NamedTuple):
    flow: float  # Vorlauf-Soll der Anlage in diesem Schritt (0 ohne Heizbetrieb oder in der Warmwasserpause)
    geliefert: bool  # Therme liefert Heizwaerme (Anforderung und keine Stoerung)
    brenner: bool  # Brenner laeuft (Heizwaerme oder Warmwasser im Sommerbetrieb)
    leistung: float  # Heizleistung des Heizkoerpers nach Ventil in W
    gemessen: float | None  # Raumfuehler mit Rauschen; None ohne Zufallsfolge


def anlage_schritt(
    zustand: Anlagenzustand, house: House, erzeuger: Potenzgesetz, hebel: tuple[float, float, float],
    outdoor: float, sonne: float, hour: float, target: float, betrieb_flags: Betriebsflags,
    rng: random.Random | None,
) -> Anlagenschritt:
    """Ein 5-min-Schritt der Anlage und des Hauses; veraendert zustand. hebel = (Steigung, Verschiebung, Heizgrenze),
    sonne = Einstrahlung in W. rng: die Zufallsfolge fuer das Fuehlerrauschen; sie wird genau einmal je Schritt
    gezogen, ohne rng bleibt gemessen None."""
    curve, shift, heat_limit = hebel
    if zustand.heating and outdoor >= heat_limit:
        zustand.heating = False
    elif not zustand.heating and outdoor < heat_limit - HYSTERESIS_K:
        zustand.heating = True
    hot_water = any(low <= hour < high for low, high in DHW_HOURS)
    flow = max(erzeuger.flow(curve, shift, outdoor), target) if zustand.heating and not hot_water else 0.0
    geliefert = flow > 0 and betrieb_flags.liefert
    power = house.radiator(flow, zustand.room) if geliefert else 0.0
    burner_on = geliefert or (hot_water and betrieb_flags.warmwasser_brenner)
    if burner_on:
        zustand.burner_hours += DT / 3600
        if not zustand.burner_was_on:
            zustand.burner_starts += 1
    zustand.burner_was_on = burner_on
    if house.valve_offset_k is not None and power > 0:
        power *= min(max(house.soll + house.valve_offset_k + 0.5 - zustand.room, 0.0), 1.0)
    zustand.room += (power - house.h * (zustand.room - outdoor) + house.gains + sonne) * DT / house.capacity
    gemessen = None if rng is None else zustand.room + rng.gauss(0, NOISE_K)
    return Anlagenschritt(flow, geliefert, burner_on, power, gemessen)
