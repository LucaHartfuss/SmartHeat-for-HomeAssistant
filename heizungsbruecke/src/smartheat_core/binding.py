"""Binding-Schnittstelle: wie ein Client die Hebel einer Anlage liest und schreibt. Die
Beschreibung (BindingDescription) ist reine Daten; die Umsetzung (PlantBinding) liegt beim Client (heute
heizungsbruecke.ha_binding ueber Home Assistant, spaeter direkt im SmartHeat-Gateway).
Die Werte kommen mit der Konfiguration (smartheat_core.bindungsparameter); der lokale Teil steht in
smartheat_core.verfahren."""
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from smartheat_core.levers import LEVERS, LeverSet
from smartheat_core.write_budget import MAX_PER_DAY

ENERGY_TOTAL = "total"
ENERGY_DAILY = "daily"


@dataclass(frozen=True)
class BindingDescription:
    """steps: Schrittweite der Anlage je Hebel (inkl. client-abgeleiteter); enforce_tolerance: Abweichung, ab der
    Durchsetzen schreibt; settle_seconds: so lange nach einem eigenen Schreiben kann die Anlage noch den alten Wert
    zeigen (Hersteller-Cloud); restore_originals: Hebel, die beim Ende der Regelung auf ihren Ursprungswert
    zurueckgehen; optional_restore: Hebel, die im Wiederherstellungspunkt fehlen duerfen -- ohne Wert bleiben
    sie waehrend eines Boosts unangetastet und sind fuer den gespeicherten Rueckfallpunkt nicht noetig;
    prepared_lever: Hebel, dessen Schreiben eine vorbereitete Anlage braucht (Vaillant: Zone auf Manuell);
    labels/preparation_label: Anzeigenamen in Kundenmeldungen.

    Standardwerte = Vaillant (unveraendert):
    preparation_resets_setpoint: True, wenn die Vorbereitung den Sollwert des prepared_lever unbrauchbar macht und er
    danach ohne Quota-Check geschrieben werden muss (Vaillant: Zonen-Umschaltung); False bei Weishaupt/Viessmann (der
    Sollwert ist ein eigenes Register und bleibt).
    daily_write_limit: physische Schreibvorgaenge je Tag ueber alle Hebel (Weishaupt 10, EEPROM); None = kein
    globales Tagesbudget. enforce_per_day: Durchsetzungs-Versuche je Hebel(-gruppe) und Tag.
    write_groups: Hebel, die zusammen geschrieben werden (Viessmann setCurve); gezaehlt wird je physischem Aufruf des
    Bindings.
    lifetime_hint_at: ab so vielen gezaehlten Schreibvorgaengen ein Hinweis (EEPROM-Lebensdauer); None = nicht zaehlen.
    aux_originals: Hilfswerte der Anlage, die vor dem ersten Schreiben gemerkt und beim Ende zurueckgestellt werden
    (Betriebsart, Komfort-/Absenk-Sollwert); ihre Namen sind Manifest-Rollen des Bindings.
    energy_counters: ENERGY_TOTAL (monoton wachsende Zaehler) oder ENERGY_DAILY (Tageszaehler, Mitternacht = 0).
    readonly_levers: Hebel ausserhalb des Hebelsatzes, die nur gelesen und im Snapshot als `readonly` gemeldet werden
    (Weishaupt-Basis: Steigung fuer die eingefrorene Steigung des Servers).
    schemata: Optionsschemata des Verfahrens vom Server (Weishaupt: praefixe, normal_bereich);
    energie: Kanal -> erwartete Einheit, ein Kanal ohne Eintrag wird nicht gesendet."""

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
    daily_write_limit: int | None = None
    enforce_per_day: int = MAX_PER_DAY
    write_groups: tuple[tuple[str, ...], ...] = ()
    lifetime_hint_at: int | None = None
    aux_originals: tuple[str, ...] = ()
    energy_counters: str = ENERGY_TOTAL
    readonly_levers: tuple[str, ...] = ()
    schemata: Mapping[str, object] = field(default_factory=dict)
    energie: Mapping[str, str] = field(default_factory=dict)

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

    physical_writes zaehlt jeden erfolgreichen physischen Schreibvorgang des Bindings (auch Hilfswerte und
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

    def aux_zulaessig(self, name: str, wert: str) -> bool:
        """Nimmt das Binding diesen Text als Hilfswert der Rolle an? Nur fuer Werte vom Server
        (ein fehlerhafter Server darf keine unbekannte Betriebsart setzen); Zahlen prueft die Pipeline."""
        ...
