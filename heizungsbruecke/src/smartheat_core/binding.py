"""Binding-Schnittstelle (Spec 5.4, Plan 2 P2-2): wie ein Client die Hebel einer Anlage liest und schreibt. Die
Beschreibung (BindingDescription) ist reine Daten; die Umsetzung (PlantBinding) liegt beim Client (heute
heizungsbruecke.ha_binding ueber Home Assistant, spaeter direkt im SmartHeat-Gateway)."""
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Protocol

from smartheat_core.levers import (
    LEVERS,
    VAILLANT_VRC720,
    VIESSMANN_VICARE,
    WEISHAUPT_WWP,
    WEISHAUPT_WWP_BASIS,
    LeverSet,
)
from smartheat_core.write_budget import MAX_PER_DAY

ENERGY_TOTAL = "total"
ENERGY_DAILY = "daily"


@dataclass(frozen=True)
class BindingDescription:
    """steps: Schrittweite der Anlage je Hebel (inkl. client-abgeleiteter); enforce_tolerance: Abweichung, ab der
    Durchsetzen schreibt; settle_seconds: so lange nach einem eigenen Schreiben kann die Anlage noch den alten Wert
    zeigen (Hersteller-Cloud); restore_originals: Hebel, die beim Ende der Regelung auf ihren Ursprungswert
    zurueckgehen (P2-5); optional_restore: Hebel, die im Wiederherstellungspunkt fehlen duerfen -- ohne Wert bleiben
    sie waehrend eines Boosts unangetastet und sind fuer den gespeicherten Rueckfallpunkt nicht noetig;
    prepared_lever: Hebel, dessen Schreiben eine vorbereitete Anlage braucht (Vaillant: Zone auf Manuell);
    labels/preparation_label: Anzeigenamen in Kundenmeldungen.

    Plan 3b (Standardwerte = Vaillant, unveraendert):
    preparation_resets_setpoint: True, wenn die Vorbereitung den Sollwert des prepared_lever unbrauchbar macht und er
    danach ohne Quota-Check geschrieben werden muss (Vaillant: Zonen-Umschaltung); False bei Weishaupt/Viessmann (der
    Sollwert ist ein eigenes Register und bleibt).
    settle_min_seconds/default_poll_seconds: Wartezeit = max(2 x Abfrageintervall + 60 s, settle_min_seconds), siehe
    with_poll_interval; beide None = settle_seconds ist fest (Vaillant 2100 s).
    daily_write_limit: physische Schreibvorgaenge je Tag ueber alle Hebel (Weishaupt 10, EEPROM); None = kein
    globales Tagesbudget. enforce_per_day: Durchsetzungs-Versuche je Hebel(-gruppe) und Tag.
    write_groups: Hebel, die zusammen geschrieben werden und als EIN Schreibvorgang zaehlen (Viessmann setCurve).
    lifetime_hint_at: ab so vielen gezaehlten Schreibvorgaengen ein Hinweis (EEPROM-Lebensdauer); None = nicht zaehlen.
    aux_originals: Hilfswerte der Anlage, die vor dem ersten Schreiben gemerkt und beim Ende zurueckgestellt werden
    (Betriebsart, Komfort-/Absenk-Sollwert); ihre Namen sind Manifest-Rollen des Bindings.
    energy_counters: ENERGY_TOTAL (monoton wachsende Zaehler) oder ENERGY_DAILY (Tageszaehler, Mitternacht = 0).
    readonly_levers: Hebel ausserhalb des Hebelsatzes, die nur gelesen und im Snapshot als `readonly` gemeldet werden
    (Weishaupt-Basis: Steigung fuer die eingefrorene Steigung des Servers, Spec 3.3/2)."""

    lever_set: LeverSet
    steps: Mapping[str, float]
    enforce_tolerance: Mapping[str, float]
    settle_seconds: float
    restore_originals: tuple[str, ...] = ()
    optional_restore: tuple[str, ...] = ()
    prepared_lever: str | None = None
    labels: Mapping[str, str] = field(default_factory=dict)
    preparation_label: str = ""
    preparation_resets_setpoint: bool = True
    settle_min_seconds: float | None = None
    default_poll_seconds: float | None = None
    daily_write_limit: int | None = None
    enforce_per_day: int = MAX_PER_DAY
    write_groups: tuple[tuple[str, ...], ...] = ()
    lifetime_hint_at: int | None = None
    aux_originals: tuple[str, ...] = ()
    energy_counters: str = ENERGY_TOTAL
    readonly_levers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        name = self.lever_set.id
        all_levers = set(self.lever_set.levers + self.lever_set.client_derived)
        for lever in sorted(all_levers):
            if lever not in self.steps:
                raise ValueError(f"Binding {name}: keine Schrittweite fuer {lever}")
            if lever not in self.enforce_tolerance:
                raise ValueError(f"Binding {name}: keine Toleranz fuer {lever}")
            if lever not in self.labels:
                raise ValueError(f"Binding {name}: kein Anzeigename fuer {lever}")
        for attribute in ("restore_originals", "optional_restore"):
            unknown = sorted(set(getattr(self, attribute)) - set(self.lever_set.levers))
            if unknown:
                raise ValueError(f"Binding {name}: {attribute} nennt fremde Hebel {', '.join(unknown)}")
        if self.prepared_lever is not None and self.prepared_lever not in self.lever_set.levers:
            raise ValueError(f"Binding {name}: prepared_lever {self.prepared_lever} gehoert nicht zum Hebelsatz")
        if self.settle_seconds <= 0:
            raise ValueError(f"Binding {name}: Wartezeit {self.settle_seconds} muss positiv sein")
        if (self.settle_min_seconds is None) != (self.default_poll_seconds is None):
            raise ValueError(f"Binding {name}: settle_min_seconds und default_poll_seconds nur zusammen")
        for attribute in ("daily_write_limit", "lifetime_hint_at"):
            value = getattr(self, attribute)
            if value is not None and value < 1:
                raise ValueError(f"Binding {name}: {attribute} {value} muss mindestens 1 sein")
        if self.enforce_per_day < 1:
            raise ValueError(f"Binding {name}: enforce_per_day {self.enforce_per_day} muss mindestens 1 sein")
        grouped: set[str] = set()
        for group in self.write_groups:
            if len(group) < 2 or not set(group) <= set(self.lever_set.levers) or set(group) & grouped:
                raise ValueError(f"Binding {name}: Schreibgruppe {group} ungueltig")
            grouped |= set(group)
        if self.energy_counters not in (ENERGY_TOTAL, ENERGY_DAILY):
            raise ValueError(f"Binding {name}: energy_counters {self.energy_counters!r} unbekannt")
        unknown = sorted(set(self.readonly_levers) - set(LEVERS))
        overlap = sorted(set(self.readonly_levers) & all_levers)
        if unknown or overlap:
            raise ValueError(f"Binding {name}: readonly_levers {', '.join(unknown + overlap)} ungueltig")


class PlantBinding(Protocol):
    """Lesen wirft bei Lesefehlern; None heisst "Anlage meldet gerade keinen Sollwert" (z. B. ruhende Zone).
    write schreibt den schon begrenzten und gerundeten Wert und wirft, wenn er nicht ankommen kann. Die Vorbereitung
    (prepare) stellt die Anlage so, dass prepared_lever wirkt; True heisst "gerade umgestellt".

    Plan 3b: physical_writes zaehlt jeden erfolgreichen physischen Schreibvorgang des Bindings (auch Hilfswerte und
    Vorbereitung; die Pipeline liest die Differenz, Tagesbudget und Lebensdauer). read_aux liefert die Hilfswerte aus
    description.aux_originals (wirft bei Lesefehlern), restore_aux stellt sie in sicherer Reihenfolge zurueck und
    schreibt nur abweichende; `levers` sind die gerade zurueckgestellten Hebel-Zielwerte (der Read kann ihnen noch
    hinterherhinken), None = unbekannt. limits liefert den Wertebereich, den die Anlage fuer den Hebel annimmt (z. B.
    min/max der HA-Entity), None = unbekannt; die Pipeline schreibt in der Schnittmenge mit den lokalen Grenzen und
    erweitert sie nie."""

    description: BindingDescription
    physical_writes: int

    def has(self, lever: str) -> bool: ...
    def ref(self, lever: str) -> str: ...
    def read(self, lever: str) -> float | None: ...
    def read_or(self, lever: str, fallback: float | None) -> float | None: ...
    def write(self, lever: str, value: float) -> None: ...
    def needs_preparation(self) -> bool: ...
    def is_prepared(self) -> bool: ...
    def prepare(self) -> bool: ...
    def read_aux(self) -> dict[str, str | float]: ...
    def restore_aux(self, values: Mapping[str, str | float], levers: Mapping[str, float] | None = None) -> None: ...
    def limits(self, lever: str) -> tuple[float, float] | None: ...


# Vaillant VRC 720 ueber mypyllant (Spec 5.4): Schrittweiten der Anlage; Toleranzen = halber Anlagenschritt
# (Mindestvorlauf 0,1 > halber Schritt, Begruendung in enforce.py); 35 min Wartezeit (mypyllant fragt die Cloud alle
# 30 min ab, OWN_WRITE_SETTLE_SECONDS bis Add-on 0.29.0); nur die Heizgrenze geht beim Ende auf ihren Ursprungswert
# (Nutzer-Entscheidung 2026-09-30).
VAILLANT_MYPYLLANT = BindingDescription(
    lever_set=VAILLANT_VRC720,
    steps={"curve": 0.05, "room_setpoint": 0.5, "heat_limit": 0.1, "min_flow": 0.1},
    enforce_tolerance={"curve": 0.025, "room_setpoint": 0.25, "heat_limit": 0.05, "min_flow": 0.1},
    settle_seconds=2100,
    restore_originals=("heat_limit",),
    optional_restore=("heat_limit",),
    prepared_lever="room_setpoint",
    labels={
        "curve": "Heizkurve", "room_setpoint": "Wunschtemperatur der Zone", "heat_limit": "Heizgrenze",
        "min_flow": "Mindestvorlauftemperatur",
    },
    preparation_label="Betriebsart der Zone",
)

# Weishaupt WWP ueber weishaupt_modbus (Spec 1.2, 5.4; Plan 3b): Schrittweiten aus der Integration (Heizkennlinie 0,05,
# Raumsoll 0,5, Sommer-Winter-Umschaltung 0,5), Toleranzen = halber Schritt; Abfrage alle 30 s (Standard), Wartezeit
# 2 x Intervall + 60 s, mindestens 120 s; EEPROM: 10 Schreibvorgaenge am Tag, Hinweis ab 50.000 (Grenze laut Weishaupt
# 100.000). Alle angefassten Hebel und Betriebsart/Komfort-/Absenk-Soll gehen beim Ende zurueck.
_WEISHAUPT_COMMON = {
    "settle_seconds": 120, "settle_min_seconds": 120, "default_poll_seconds": 30, "prepared_lever": "room_setpoint",
    "preparation_label": "Betriebsart", "preparation_resets_setpoint": False, "daily_write_limit": 10,
    "lifetime_hint_at": 50000, "aux_originals": ("mode_select", "setpoint_comfort", "setpoint_setback"),
    "energy_counters": ENERGY_DAILY,
}
WEISHAUPT_MODBUS = BindingDescription(
    lever_set=WEISHAUPT_WWP,
    steps={"curve": 0.05, "room_setpoint": 0.5, "heat_limit": 0.5},
    enforce_tolerance={"curve": 0.025, "room_setpoint": 0.25, "heat_limit": 0.25},
    restore_originals=("curve", "room_setpoint", "heat_limit"),
    labels={"curve": "Heizkennlinie", "room_setpoint": "Raumsolltemperatur Normal", "heat_limit": "Sommer-Winter-Umschaltung"},
    **_WEISHAUPT_COMMON,
)
# Rueckfall ohne Heizkennlinie/Sommer-Winter-Umschaltung (Spec 3.3, 10): nur das Raumsoll; die Steigung wird, falls
# gemappt, nur gelesen (readonly) -- die Heizgrenze nicht, weil ihr Bereich 3-30 / "Aus" die Plausibilitaet des Servers
# (5-25) verletzen und den ganzen Snapshot ablehnen lassen koennte.
WEISHAUPT_MODBUS_BASIS = BindingDescription(
    lever_set=WEISHAUPT_WWP_BASIS,
    steps={"room_setpoint": 0.5},
    enforce_tolerance={"room_setpoint": 0.25},
    restore_originals=("room_setpoint",),
    labels={"room_setpoint": "Raumsolltemperatur Normal"},
    readonly_levers=("curve",),
    **_WEISHAUPT_COMMON,
)
# Viessmann ueber HA-Core vicare (Spec 1.3, 5.4; Plan 3b): Neigung 0,1, Niveau 1 K, Programmtemperatur "normal" 1 K;
# Neigung und Niveau gehen ueber setCurve und zaehlen als ein Schreibvorgang; HA fragt alle 60 s ab, Wartezeit
# 2 x Intervall + 60 s, mindestens 180 s; Cloud-Kontingent: Durchsetzen 4 statt 6 am Tag; kein globales Tagesbudget.
VIESSMANN_VICARE_BINDING = BindingDescription(
    lever_set=VIESSMANN_VICARE,
    steps={"curve": 0.1, "level": 1.0, "room_setpoint": 1.0},
    enforce_tolerance={"curve": 0.05, "level": 0.5, "room_setpoint": 0.5},
    settle_seconds=180,
    settle_min_seconds=180,
    default_poll_seconds=60,
    restore_originals=("curve", "level", "room_setpoint"),
    prepared_lever="room_setpoint",
    labels={"curve": "Neigung der Heizkurve", "level": "Niveau der Heizkurve", "room_setpoint": "Raumtemperatur Normal"},
    preparation_label="Heizprogramm",
    preparation_resets_setpoint=False,
    enforce_per_day=4,
    write_groups=(("curve", "level"),),
    aux_originals=("mode_select",),
    energy_counters=ENERGY_DAILY,
)

BINDINGS: dict[str, BindingDescription] = {
    description.lever_set.id: description
    for description in (VAILLANT_MYPYLLANT, WEISHAUPT_MODBUS, WEISHAUPT_MODBUS_BASIS, VIESSMANN_VICARE_BINDING)
}


def with_poll_interval(description: BindingDescription, poll_seconds: float | None) -> BindingDescription:
    """Wirksame Beschreibung fuer das Abfrageintervall der Integration (Option poll_interval_seconds): Wartezeit nach
    eigenem Schreiben = max(2 x Intervall + 60 s, settle_min_seconds). Ohne Intervall gilt default_poll_seconds; eine
    Beschreibung mit fester Wartezeit (Vaillant) bleibt unveraendert."""
    if description.settle_min_seconds is None or description.default_poll_seconds is None:
        return description
    interval = description.default_poll_seconds if poll_seconds is None else poll_seconds
    return replace(description, settle_seconds=max(2 * interval + 60, description.settle_min_seconds))
