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


class UnknownProfileError(ValueError):
    pass


def required_roles_for(profile_id: str) -> tuple[str, ...]:
    try:
        return REQUIRED_ROLES_BY_PROFILE[profile_id]
    except KeyError:
        raise UnknownProfileError(f"Unbekanntes profile: {profile_id}") from None
