"""Lokale Sicherheitswerte (Clamps, Comfort-Boost) je Verteilsystem. Bleiben bewusst im
Add-on (Regel 4) und kommen nie vom Server. Die Schluessel spiegeln
VERTEILSYSTEME_MIT_LOKALEN_SICHERHEITSWERTEN im Server (generic/profiles.py), die
Server-Clamps jedes aktiven Profils muessen innerhalb dieser Werte liegen; beides prueft
tools/contract_check.py im Dev-Root. Fussbodenheizung hat bewusst noch keine Werte: das
Add-on verweigert dann den Start, bis der Nutzer Werte freigibt."""
from dataclasses import dataclass


@dataclass(frozen=True)
class LocalSafety:
    """`boost_threshold_k` ist die ANKUNFTS-Schwelle des Comfort-Boosts, nicht die Ausloese-Schwelle
    (ausgeloest wird er ausschliesslich durch eine Erhoehung von room_target, boost.decide_boost).
    shift_* begrenzen die Parallelverschiebung (Zonen-Wunschtemperatur), min_flow_* die
    Mindestvorlauftemperatur (TP11, vom Nutzer freigegeben 2026-09-29, Regel 4)."""

    curve_min: float
    curve_max: float
    shift_min: float
    shift_max: float
    min_flow_min: float
    min_flow_max: float
    boost_threshold_k: float
    boost_curve_value: float
    boost_shift_value: float


LOCAL_SAFETY_BY_VERTEILSYSTEM: dict[str, LocalSafety] = {
    "Heizkoerper": LocalSafety(
        curve_min=0.4, curve_max=1.5, shift_min=15.0, shift_max=25.0, min_flow_min=20.0, min_flow_max=30.0,
        boost_threshold_k=0.5, boost_curve_value=1.5, boost_shift_value=25.0,
    ),
}


def _check_invariants(safety: LocalSafety, verteilsystem: str) -> None:
    if safety.curve_min > safety.curve_max:
        raise ValueError(
            f"Verteilsystem '{verteilsystem}': curve_min ({safety.curve_min}) ist groesser als "
            f"curve_max ({safety.curve_max})"
        )
    for min_key, max_key in (("shift_min", "shift_max"), ("min_flow_min", "min_flow_max")):
        min_value, max_value = getattr(safety, min_key), getattr(safety, max_key)
        if min_value > max_value:
            raise ValueError(
                f"Verteilsystem '{verteilsystem}': {min_key} ({min_value}) ist groesser als "
                f"{max_key} ({max_value})"
            )


def resolve_local_safety(verteilsystem) -> LocalSafety:
    """Kein stiller Rueckfall: unbekanntes oder fehlendes Verteilsystem ist ein Fehler."""
    safety = LOCAL_SAFETY_BY_VERTEILSYSTEM.get(verteilsystem) if isinstance(verteilsystem, str) else None
    if safety is None:
        raise ValueError(f"keine lokalen Sicherheitswerte für Verteilsystem '{verteilsystem}'")
    _check_invariants(safety, verteilsystem)
    return safety
