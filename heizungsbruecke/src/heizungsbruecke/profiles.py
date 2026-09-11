from dataclasses import dataclass, replace

_COMMON_REQUIRED_ROLES = (
    "room_actual", "room_target", "curve_current", "offset_current",
    "room_day_avg", "room_night_avg", "heat_limit", "dat", "dart",
)

PROFILE_LABELS: dict[str, str] = {
    "weishaupt_waermepumpe_fussbodenheizung": "Weishaupt Waermepumpe (Fussbodenheizung)",
    "vaillant_gastherme_heizkoerper": "Vaillant Gastherme (Heizkoerper)",
}

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


class UnknownProfileError(ValueError):
    pass


def required_roles_for(profile_id: str) -> tuple[str, ...]:
    try:
        return REQUIRED_ROLES_BY_PROFILE[profile_id]
    except KeyError:
        raise UnknownProfileError(f"Unbekanntes profile: {profile_id}") from None


def resolve_local_clamps(profile_id: str, options: dict) -> LocalClamps:
    """Loest die vier lokalen Sicherheits-Clamps auf: ein in `options` explizit
    gesetzter (nicht-falsy) Wert gewinnt immer als Override, sonst greift der
    Profil-Default. Ohne Profil-Default (Profil noch nicht aktiviert, siehe
    LOCAL_CLAMP_DEFAULTS) muessen alle vier Felder explizit gesetzt sein.
    """
    defaults = LOCAL_CLAMP_DEFAULTS.get(profile_id)

    if defaults is None:
        # Ohne Profil-Default: akzeptiere alle Felder die in options vorhanden sind
        # (auch falsy Werte wie 0.0)
        overrides = {
            field: options[field]
            for field in ("curve_min", "curve_max", "offset_min", "offset_max")
            if field in options
        }
        missing = {"curve_min", "curve_max", "offset_min", "offset_max"} - set(overrides)
        if missing:
            raise UnknownProfileError(
                f"Profil '{profile_id}' hat keine lokalen Clamp-Defaults, und folgende "
                f"Pflichtfelder fehlen in der Add-on-Konfiguration: {', '.join(sorted(missing))}"
            )
        return LocalClamps(**overrides)

    # Mit Profil-Default: ignoriere falsy Werte (0.0 ist Sentinel fuer "nicht gesetzt")
    overrides = {
        field: options[field]
        for field in ("curve_min", "curve_max", "offset_min", "offset_max")
        if options.get(field)
    }
    return replace(defaults, **overrides)
