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
    """Deckt jeden Fehlschlag der Profil-/Clamp-Aufloesung ab: unbekannte profile_id,
    fehlende Pflichtfelder ohne Profil-Default, oder ein aufgeloestes Clamp-Ergebnis
    mit invertiertem Bereich (curve_min > curve_max bzw. offset_min > offset_max).
    """


def required_roles_for(profile_id: str) -> tuple[str, ...]:
    try:
        return REQUIRED_ROLES_BY_PROFILE[profile_id]
    except KeyError:
        raise UnknownProfileError(f"Unbekanntes profile: {profile_id}") from None


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


def resolve_local_clamps(profile_id: str, options: dict) -> LocalClamps:
    """Loest die vier lokalen Sicherheits-Clamps auf.

    Mit Profil-Default (siehe LOCAL_CLAMP_DEFAULTS): ein in `options` explizit
    gesetzter *nicht-falsy* Wert gewinnt als Override, sonst greift der Profil-Default
    -- 0.0 zaehlt dabei als "nicht gesetzt" (config.yaml-Sentinel-Konvention).

    Ohne Profil-Default (Profil noch nicht aktiviert): es gibt keinen Default, auf den
    ein falsy Wert zurueckfallen koennte, also zaehlt allein die *Anwesenheit* in
    `options` -- 0.0 wird hier als echter, vom Betreiber gemeinter Wert uebernommen.
    Alle vier Felder muessen dabei explizit gesetzt sein.

    In beiden Faellen wird das aufgeloeste (gemergte) Ergebnis auf curve_min <=
    curve_max und offset_min <= offset_max geprueft, bevor es zurueckgegeben wird.
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
        clamps = LocalClamps(**overrides)
        _check_clamp_invariants(clamps, profile_id)
        return clamps

    # Mit Profil-Default: ignoriere falsy Werte (0.0 ist Sentinel fuer "nicht gesetzt").
    # Anders als im Zweig ohne Default ist hier ein Default vorhanden, auf den 0.0
    # eindeutig zurueckfallen kann -- die Praesenz-Pruefung dort ist daher absichtlich
    # keine "Vereinfachung", die man hier uebernehmen sollte: ohne Default gibt es
    # nichts, worauf 0.0 zurueckfallen koennte, also muss die Praesenz in `options`
    # allein entscheiden und 0.0 als echter Wert durchgereicht werden.
    overrides = {
        field: options[field]
        for field in ("curve_min", "curve_max", "offset_min", "offset_max")
        if options.get(field)
    }
    clamps = replace(defaults, **overrides)
    _check_clamp_invariants(clamps, profile_id)
    return clamps
