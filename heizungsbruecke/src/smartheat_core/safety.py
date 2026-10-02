"""Lokale Sicherheitswerte je (Hebelsatz x Verteilsystem) (Spec 5.3, Regel 4). Bleiben bewusst auf dem Client und
kommen nie vom Server. Die Server-Bereiche jedes aktiven Profils muessen innerhalb liegen (Contract-Check 2); die
Verteilsysteme spiegeln VERTEILSYSTEME_MIT_LOKALEN_SICHERHEITSWERTEN im Server (Contract-Check 1). Fehlen Werte,
verweigert der Client den Start, bis der Nutzer Werte freigibt."""
from collections.abc import Mapping
from dataclasses import dataclass

from smartheat_core.levers import LEVER_SETS


@dataclass(frozen=True)
class LocalSafety:
    """ranges: Bereich je Hebel des Hebelsatzes einschliesslich der client-abgeleiteten (Mindestvorlauf).
    comfort_boost: Comfort-Boost-Werte je Hebel; leer = kein Comfort-Boost (Fussbodenheizung, Spec 6.3).
    emergency_boost_levers: der Notfall-Boost setzt diese Hebel auf ihr lokales Maximum.
    arrival_threshold_k: Ankunftsschwelle des Comfort-Boosts und Austrittsschwelle des Notfall-Boosts -- nicht die
    Ausloese-Schwelle (der Comfort-Boost startet nur bei einer Erhoehung des Raum-Solls, boost.decide_boost)."""

    ranges: Mapping[str, tuple[float, float]]
    comfort_boost: Mapping[str, float]
    emergency_boost_levers: tuple[str, ...]
    arrival_threshold_k: float


LOCAL_SAFETY: dict[tuple[str, str], LocalSafety] = {
    # Vaillant x Heizkoerper: Regel 4, Nutzer-Freigaben 2026-09-29 (Steigung, Wunschtemperatur, Mindestvorlauf,
    # Boost), 2026-09-30 (Heizgrenze) und 2026-10-01 (Heizgrenze bis 23 °C).
    ("vaillant_vrc720", "Heizkoerper"): LocalSafety(
        ranges={
            "curve": (0.4, 1.5), "room_setpoint": (15.0, 25.0), "heat_limit": (5.0, 23.0), "min_flow": (20.0, 30.0),
        },
        comfort_boost={"curve": 1.5, "room_setpoint": 25.0, "heat_limit": 23.0},
        emergency_boost_levers=("curve", "room_setpoint", "heat_limit"),
        arrival_threshold_k=0.5,
    ),
}


def verteilsysteme() -> tuple[str, ...]:
    return tuple(sorted({verteilsystem for _, verteilsystem in LOCAL_SAFETY}))


def check_invariants(safety: LocalSafety, lever_set_id: str, verteilsystem: str) -> None:
    label = f"Hebelsatz '{lever_set_id}', Verteilsystem '{verteilsystem}'"
    lever_set = LEVER_SETS.get(lever_set_id)
    if lever_set is None:
        raise ValueError(f"{label}: unbekannter Hebelsatz")
    needed = set(lever_set.levers + lever_set.client_derived)
    if set(safety.ranges) != needed:
        raise ValueError(f"{label}: Bereiche fuer {sorted(safety.ranges)} statt {sorted(needed)}")
    for lever, (low, high) in safety.ranges.items():
        if low > high:
            raise ValueError(f"{label}: {lever} Minimum {low} groesser als Maximum {high}")
    for lever, value in safety.comfort_boost.items():
        if lever not in lever_set.levers:
            raise ValueError(f"{label}: Comfort-Boost {lever} ist kein gesendeter Hebel")
        low, high = safety.ranges[lever]
        if not low <= value <= high:
            raise ValueError(f"{label}: Comfort-Boost {lever}={value} ausserhalb [{low}, {high}]")
    unknown = sorted(set(safety.emergency_boost_levers) - set(lever_set.levers))
    if unknown:
        raise ValueError(f"{label}: Notfall-Boost auf unbekannte Hebel {', '.join(unknown)}")
    if safety.arrival_threshold_k <= 0:
        raise ValueError(f"{label}: Ankunftsschwelle {safety.arrival_threshold_k} muss positiv sein")


def resolve_local_safety(lever_set_id: str, verteilsystem) -> LocalSafety:
    """Kein stiller Rueckfall: fehlende Werte sind ein Fehler. Der Text fuer ein unbekanntes Verteilsystem ist der des
    Konfigurationsfehlers bis Add-on 0.29.0 (Kundentext)."""
    if lever_set_id not in LEVER_SETS:
        raise ValueError(f"keine lokalen Sicherheitswerte für Hebelsatz '{lever_set_id}'")
    safety = LOCAL_SAFETY.get((lever_set_id, verteilsystem)) if isinstance(verteilsystem, str) else None
    if safety is None:
        raise ValueError(f"keine lokalen Sicherheitswerte für Verteilsystem '{verteilsystem}'")
    check_invariants(safety, lever_set_id, verteilsystem)
    return safety
