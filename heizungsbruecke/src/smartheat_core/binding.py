"""Binding-Schnittstelle (Spec 5.4, Plan 2 P2-2): wie ein Client die Hebel einer Anlage liest und schreibt. Die
Beschreibung (BindingDescription) ist reine Daten; die Umsetzung (PlantBinding) liegt beim Client (heute
heizungsbruecke.ha_binding ueber Home Assistant, spaeter direkt im SmartHeat-Gateway)."""
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from smartheat_core.levers import VAILLANT_VRC720, LeverSet


@dataclass(frozen=True)
class BindingDescription:
    """steps: Schrittweite der Anlage je Hebel (inkl. client-abgeleiteter); enforce_tolerance: Abweichung, ab der
    Durchsetzen schreibt; settle_seconds: so lange nach einem eigenen Schreiben kann die Anlage noch den alten Wert
    zeigen (Hersteller-Cloud); restore_originals: Hebel, die beim Ende der Regelung auf ihren Ursprungswert
    zurueckgehen (P2-5); optional_restore: Hebel, die im Wiederherstellungspunkt fehlen duerfen -- ohne Wert bleiben
    sie waehrend eines Boosts unangetastet und sind fuer den gespeicherten Rueckfallpunkt nicht noetig;
    prepared_lever: Hebel, dessen Schreiben eine vorbereitete Anlage braucht (Vaillant: Zone auf Manuell);
    labels/preparation_label: Anzeigenamen in Kundenmeldungen."""

    lever_set: LeverSet
    steps: Mapping[str, float]
    enforce_tolerance: Mapping[str, float]
    settle_seconds: float
    restore_originals: tuple[str, ...] = ()
    optional_restore: tuple[str, ...] = ()
    prepared_lever: str | None = None
    labels: Mapping[str, str] = field(default_factory=dict)
    preparation_label: str = ""

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


class PlantBinding(Protocol):
    """Lesen wirft bei Lesefehlern; None heisst "Anlage meldet gerade keinen Sollwert" (z. B. ruhende Zone).
    write schreibt den schon begrenzten und gerundeten Wert und wirft, wenn er nicht ankommen kann. Die Vorbereitung
    (prepare) stellt die Anlage so, dass prepared_lever wirkt; True heisst "gerade umgestellt"."""

    description: BindingDescription

    def has(self, lever: str) -> bool: ...
    def ref(self, lever: str) -> str: ...
    def read(self, lever: str) -> float | None: ...
    def read_or(self, lever: str, fallback: float | None) -> float | None: ...
    def write(self, lever: str, value: float) -> None: ...
    def needs_preparation(self) -> bool: ...
    def is_prepared(self) -> bool: ...
    def prepare(self) -> bool: ...


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

BINDINGS: dict[str, BindingDescription] = {VAILLANT_MYPYLLANT.lever_set.id: VAILLANT_MYPYLLANT}
