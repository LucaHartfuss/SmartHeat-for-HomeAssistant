"""Hebel-Vokabular und Hebelsaetze (Spec 2). Gegenstueck: heizungsserver.generic.plants (Contract-Check 4)."""
from dataclasses import dataclass

LEVERS = ("curve", "room_setpoint", "level", "heat_limit", "min_flow")


@dataclass(frozen=True)
class LeverSet:
    """Benannte Menge Hebel, die ein Binding bedient. `levers` sendet der Server, `client_derived` leitet der Client
    selbst ab (Mindestvorlauf = Raum-Soll)."""

    id: str
    levers: tuple[str, ...]
    client_derived: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.levers:
            raise ValueError(f"Hebelsatz {self.id}: keine Hebel")
        unknown = sorted(set(self.levers + self.client_derived) - set(LEVERS))
        if unknown:
            raise ValueError(f"Hebelsatz {self.id}: unbekannte Hebel: {', '.join(unknown)}")
        both = sorted(set(self.levers) & set(self.client_derived))
        if both:
            raise ValueError(f"Hebelsatz {self.id}: sowohl gesendet als auch abgeleitet: {', '.join(both)}")


VAILLANT_VRC720 = LeverSet("vaillant_vrc720", ("curve", "room_setpoint", "heat_limit"), ("min_flow",))

# Hersteller-Abstraktion Plan 3b: gleiche IDs und Hebel wie heizungsserver.generic.plants (Contract-Checks 4 und 40).
WEISHAUPT_WWP = LeverSet("weishaupt_wwp", ("curve", "room_setpoint", "heat_limit"))
WEISHAUPT_WWP_BASIS = LeverSet("weishaupt_wwp_basis", ("room_setpoint",))
VIESSMANN_VICARE = LeverSet("viessmann_vicare", ("curve", "level", "room_setpoint"))

LEVER_SETS: dict[str, LeverSet] = {
    lever_set.id: lever_set for lever_set in (VAILLANT_VRC720, WEISHAUPT_WWP, WEISHAUPT_WWP_BASIS, VIESSMANN_VICARE)
}
