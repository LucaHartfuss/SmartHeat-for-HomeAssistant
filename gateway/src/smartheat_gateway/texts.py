"""Kundentexte des Gateways; Gegenstueck zu HA_TEXTS."""
from smartheat_runtime.texts import HostTexts

SHG_TEXTS = HostTexts(
    storage_failed=(
        "SmartHeat: Der Datenträger des Gateways ist nicht beschreibbar (voll oder schreibgeschützt). "
        "Die Heizungsregelung pausiert, die Anlage behält ihre letzten Werte."
    ),
    delivery_prefix="SmartHeat-Gateway",
    source_unavailable_log="Signalquelle nicht erreichbar",
)
