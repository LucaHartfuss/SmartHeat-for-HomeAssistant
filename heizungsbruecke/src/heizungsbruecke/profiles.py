from dataclasses import dataclass

_COMMON_REQUIRED_ROLES = (
    "room_actual", "room_target", "curve_current", "offset_current",
    "room_day_avg", "room_night_avg", "heat_limit", "dat", "dart",
)

REQUIRED_ROLES_BY_PROFILE: dict[str, tuple[str, ...]] = {
    "weishaupt_waermepumpe_fussbodenheizung": _COMMON_REQUIRED_ROLES,
    "vaillant_gastherme_heizkoerper": _COMMON_REQUIRED_ROLES,
}


@dataclass(frozen=True)
class LocalClamps:
    curve_min: float
    curve_max: float
    offset_min: float
    offset_max: float


@dataclass(frozen=True)
class BoostDefaults:
    """`threshold_k` ist seit der Boost-Neudefinition (Design-Spec Phase 5, Punkt 15)
    die ANKUNFTS-Schwelle (wie nah am -- ggf. neuen -- Zielwert Boost sich selbst
    beendet), NICHT mehr die Ausloese-Schwelle. Ausgeloest wird Boost jetzt
    ausschliesslich durch eine Erhoehung von room_target zwischen zwei Ticks, siehe
    heizungsbruecke.boost.decide_boost.
    """
    threshold_k: float
    curve_value: float
    offset_value: float


# Nur Profile mit einem Eintrag hier sind fuer Kunden im config.yaml-Dropdown
# waehlbar (siehe schema.profile). Gleiche Werte wie serverseitig in
# heizungsserver/src/heizungsserver/generic/profiles.py -- kein geteilter Code
# zwischen den Repos, gleiche profile_id-Namenskonvention (bestehendes Muster,
# siehe REQUIRED_ROLES_BY_PROFILE oben).
LOCAL_CLAMP_DEFAULTS: dict[str, LocalClamps] = {
    "vaillant_gastherme_heizkoerper": LocalClamps(
        curve_min=0.4, curve_max=1.5, offset_min=20.0, offset_max=30.0,
    ),
}


LOCAL_BOOST_DEFAULTS: dict[str, BoostDefaults] = {
    "vaillant_gastherme_heizkoerper": BoostDefaults(
        threshold_k=0.5, curve_value=1.5, offset_value=30.0,
    ),
}


class UnknownProfileError(ValueError):
    """Deckt jeden Fehlschlag der Profil-/Clamp-Aufloesung ab: unbekannte profile_id,
    fehlende Pflichtfelder ohne Profil-Default, oder ein aufgeloestes Clamp-Ergebnis
    mit invertiertem Bereich (curve_min > curve_max bzw. offset_min > offset_max).
    """


def required_roles_for(profile_id: str) -> tuple[str, ...]:
    try:
        return REQUIRED_ROLES_BY_PROFILE[profile_id]
    except KeyError:
        raise UnknownProfileError(f"Unbekanntes profile: {profile_id}") from None


def resolve_boost_defaults(profile_id: str) -> BoostDefaults:
    try:
        return LOCAL_BOOST_DEFAULTS[profile_id]
    except KeyError:
        raise UnknownProfileError(f"Profil '{profile_id}' hat keine Boost-Defaults hinterlegt") from None


def _check_clamp_invariants(clamps: LocalClamps, profile_id: str) -> None:
    """Prueft die aufgeloesten (gemergten) Clamps, nicht die rohen Einzelwerte -- ein
    Teil-Override kann diese Invarianten allein brechen, auch wenn Profil-Default und
    Override jeweils fuer sich plausibel aussehen (z.B. curve_min per Override auf 2.0
    bei unveraendertem Default curve_max=1.5).
    """
    if clamps.curve_min > clamps.curve_max:
        raise UnknownProfileError(
            f"Profil '{profile_id}': aufgeloester curve_min ({clamps.curve_min}) ist "
            f"groesser als curve_max ({clamps.curve_max})"
        )
    if clamps.offset_min > clamps.offset_max:
        raise UnknownProfileError(
            f"Profil '{profile_id}': aufgeloester offset_min ({clamps.offset_min}) ist "
            f"groesser als offset_max ({clamps.offset_max})"
        )


def resolve_local_clamps(profile_id: str) -> LocalClamps:
    try:
        clamps = LOCAL_CLAMP_DEFAULTS[profile_id]
    except KeyError:
        raise UnknownProfileError(f"Profil '{profile_id}' hat keine lokalen Clamp-Defaults hinterlegt") from None
    _check_clamp_invariants(clamps, profile_id)
    return clamps
