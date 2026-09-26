"""Lokale Sicherheitswerte (Clamps, Comfort-Boost) je Verteilsystem. Bleiben bewusst im
Add-on (Regel 4) und kommen nie vom Server. Die Schluessel spiegeln
VERTEILSYSTEME_MIT_LOKALEN_SICHERHEITSWERTEN im Server (generic/profiles.py), die
Server-Clamps jedes aktiven Profils muessen innerhalb dieser Werte liegen; beides prueft
tools/contract_check.py im Dev-Root. Fussbodenheizung hat bewusst noch keine Werte: das
Add-on verweigert dann den Start, bis der Nutzer Werte freigibt."""
from dataclasses import dataclass


@dataclass(frozen=True)
class LocalSafety:
    """`boost_threshold_k` ist die ANKUNFTS-Schwelle des Comfort-Boosts (wie nah am -- ggf.
    neuen -- Zielwert er sich selbst beendet), nicht die Ausloese-Schwelle: ausgeloest wird
    er ausschliesslich durch eine Erhoehung von room_target (boost.decide_boost)."""

    curve_min: float
    curve_max: float
    offset_min: float
    offset_max: float
    boost_threshold_k: float
    boost_curve_value: float
    boost_offset_value: float


LOCAL_SAFETY_BY_VERTEILSYSTEM: dict[str, LocalSafety] = {
    "Heizkoerper": LocalSafety(
        curve_min=0.4, curve_max=1.5, offset_min=20.0, offset_max=30.0,
        boost_threshold_k=0.5, boost_curve_value=1.5, boost_offset_value=30.0,
    ),
}


def _check_invariants(safety: LocalSafety, verteilsystem: str) -> None:
    if safety.curve_min > safety.curve_max:
        raise ValueError(
            f"Verteilsystem '{verteilsystem}': curve_min ({safety.curve_min}) ist groesser als "
            f"curve_max ({safety.curve_max})"
        )
    if safety.offset_min > safety.offset_max:
        raise ValueError(
            f"Verteilsystem '{verteilsystem}': offset_min ({safety.offset_min}) ist groesser als "
            f"offset_max ({safety.offset_max})"
        )


def resolve_local_safety(verteilsystem) -> LocalSafety:
    """Kein stiller Rueckfall: unbekanntes oder fehlendes Verteilsystem ist ein Fehler."""
    safety = LOCAL_SAFETY_BY_VERTEILSYSTEM.get(verteilsystem) if isinstance(verteilsystem, str) else None
    if safety is None:
        raise ValueError(f"keine lokalen Sicherheitswerte für Verteilsystem '{verteilsystem}'")
    _check_invariants(safety, verteilsystem)
    return safety
