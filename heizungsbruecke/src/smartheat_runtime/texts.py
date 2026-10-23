"""Kundentexte und Log-Texte, die den Host nennen (Spec SHG G2 2.1). Standardwerte = die Texte des HA-Add-ons; das Gateway liefert
eigene ueber Host.texts. Importiert nichts aus dem Paket (kein Zyklus)."""
from dataclasses import dataclass


@dataclass(frozen=True)
class HostTexts:
    # Meldung "Datentraeger nicht beschreibbar" (datentraeger.report).
    storage_failed: str = (
        "SmartHeat: Der Datenträger des Home-Assistant-Systems ist nicht beschreibbar (voll oder "
        "schreibgeschützt). Die Heizungsregelung pausiert, die Anlage behält ihre letzten Werte."
    )
    # Praefix der Zustell-Meldungen (delivery.notification_text).
    delivery_prefix: str = "Heizungsbrücke"
    # Log, wenn die Signalquelle nicht antwortet (battery, room_sensors).
    source_unavailable_log: str = "Home Assistant nicht erreichbar"


HA_TEXTS = HostTexts()
