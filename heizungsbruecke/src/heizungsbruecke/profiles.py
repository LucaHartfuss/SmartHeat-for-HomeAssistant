from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class LocalClamps:
    curve_min: float
    curve_max: float
    offset_min: float
    offset_max: float


@dataclass(frozen=True)
class BoostDefaults:
    """`threshold_k` ist seit der Boost-Neudefinition die ANKUNFTS-Schwelle (wie nah
    am -- ggf. neuen -- Zielwert Boost sich selbst
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
# zwischen den Repos, gleiche profile_id-Namenskonvention.
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


@dataclass(frozen=True)
class WindowDefaults:
    """Fenstergrenzen fuer den taeglichen vollen Snapshot-Publish sowie die Tag-/
    Nachtmittel-Berechnung des Referenzraums.
    Werte identisch zu den serverseitigen TriggerWindows in heizungsserver/src/
    heizungsserver/generic/profiles.py -- kein geteilter Code zwischen den Repos,
    gleiches Muster wie LOCAL_CLAMP_DEFAULTS/LOCAL_BOOST_DEFAULTS oben. Tag- und
    Nachtfenster muessen gleich gross sein (siehe _check_window_invariants): beide
    werden vom selben rollierenden statistics-Sensor gelesen (derived_sensors.py),
    ein einzelner max_age_hours-Wert bedient beide Ablesungen.
    """

    daily_trigger_time: str
    day_avg_window_start: str
    day_avg_window_end: str
    night_avg_window_start: str
    night_avg_window_end: str


class UnknownProfileError(ValueError):
    """Deckt jeden Fehlschlag der Profil-/Clamp-Aufloesung ab: unbekannte profile_id,
    fehlende Pflichtfelder ohne Profil-Default, oder ein aufgeloestes Clamp-Ergebnis
    mit invertiertem Bereich (curve_min > curve_max bzw. offset_min > offset_max).
    """


def resolve_boost_defaults(profile_id: str) -> BoostDefaults:
    try:
        return LOCAL_BOOST_DEFAULTS[profile_id]
    except KeyError:
        raise UnknownProfileError(f"Profil '{profile_id}' hat keine Boost-Defaults hinterlegt") from None


LOCAL_WINDOW_DEFAULTS: dict[str, WindowDefaults] = {
    "vaillant_gastherme_heizkoerper": WindowDefaults(
        daily_trigger_time="12:00",
        day_avg_window_start="14:00", day_avg_window_end="17:00",
        night_avg_window_start="04:00", night_avg_window_end="07:00",
    ),
}


# Dupliziertes lokales Gegenstueck zu Profile.telemetry_capabilities.energy_channels
# in heizungsserver/generic/profiles.py -- kein geteilter Code zwischen den Repos,
# gleiches Muster wie LOCAL_CLAMP_DEFAULTS.
KPI_ENERGY_CHANNELS_BY_PROFILE: dict[str, tuple[str, ...]] = {
    "vaillant_gastherme_heizkoerper": (
        "electrical_heating", "electrical_dhw",
        "primary_heating", "primary_dhw",
        "thermal_heating", "thermal_dhw",
    ),
}


def _parse_hhmm_minutes(value: str) -> int:
    parsed = datetime.strptime(value, "%H:%M")
    return parsed.hour * 60 + parsed.minute


def window_size_hours(start: str, end: str) -> float:
    return (_parse_hhmm_minutes(end) - _parse_hhmm_minutes(start)) / 60.0


def _check_window_invariants(windows: WindowDefaults, profile_id: str) -> None:
    day_size = window_size_hours(windows.day_avg_window_start, windows.day_avg_window_end)
    night_size = window_size_hours(windows.night_avg_window_start, windows.night_avg_window_end)
    if day_size <= 0:
        raise UnknownProfileError(
            f"Profil '{profile_id}': Tagesfenster ({windows.day_avg_window_start}-"
            f"{windows.day_avg_window_end}) ist nicht positiv"
        )
    if night_size <= 0:
        raise UnknownProfileError(
            f"Profil '{profile_id}': Nachtfenster ({windows.night_avg_window_start}-"
            f"{windows.night_avg_window_end}) ist nicht positiv"
        )
    if day_size != night_size:
        raise UnknownProfileError(
            f"Profil '{profile_id}': Tagesfenster ({day_size}h) und Nachtfenster "
            f"({night_size}h) muessen gleich gross sein (gemeinsamer statistics-Sensor)"
        )


def resolve_window_defaults(profile_id: str) -> WindowDefaults:
    try:
        windows = LOCAL_WINDOW_DEFAULTS[profile_id]
    except KeyError:
        raise UnknownProfileError(f"Profil '{profile_id}' hat keine Fenster-Defaults hinterlegt") from None
    _check_window_invariants(windows, profile_id)
    return windows


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
