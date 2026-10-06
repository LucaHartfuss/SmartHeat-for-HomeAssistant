from smartheat_runtime.roles import ALL_ROLES, REQUIRED_ROLES_BY_LEVER_SET, ChannelManifest, ManifestError

# Attribut der Zonen-Wunschtemperatur einer Climate-Entity (ha_api.get_state liest "entity::attribut").
CLIMATE_TARGET_ATTRIBUTE = "temperature"


def entity_ref(role: str, value: str) -> str:
    """Referenz, unter der die Bruecke die Rolle liest: eine Climate-Zone als Parallelverschiebung
    wird ueber ihr Attribut `temperature` gelesen."""
    if role == "shift_current" and value.startswith("climate.") and "::" not in value:
        return f"{value}::{CLIMATE_TARGET_ATTRIBUTE}"
    return value


def build_manifest(
    options: dict, derived_entity_ids: dict[str, str] | None = None, lever_set_id: str = "vaillant_vrc720",
) -> ChannelManifest:
    derived_entity_ids = derived_entity_ids or {}
    refs = {}
    for role in ALL_ROLES:
        if role in derived_entity_ids:
            refs[role] = derived_entity_ids[role]
            continue
        value = options.get(f"entity_{role}")
        if value:
            refs[role] = entity_ref(role, value)

    missing = [role for role in REQUIRED_ROLES_BY_LEVER_SET[lever_set_id] if role not in refs]
    if missing:
        raise ManifestError(f"Pflicht-Rollen fehlen in der Add-on-Konfiguration: {', '.join(missing)}")

    return ChannelManifest(refs=refs)
