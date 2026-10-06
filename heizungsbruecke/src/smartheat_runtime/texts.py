"""Kundentexte und Log-Texte, die den Host nennen (Spec SHG G2 2.1). Standardwerte = die Texte des HA-Add-ons
(wortgleich mit abo.ACCESS_DENIED_MESSAGE und datentraeger.FAILED_MESSAGE, tests/test_texts.py); das Gateway liefert
eigene ueber Host.texts. Importiert nichts aus dem Paket (kein Zyklus)."""
from dataclasses import dataclass


@dataclass(frozen=True)
class HostTexts:
    # Meldung bei abgelehnten Zugangsdaten (abo._report_rejection).
    access_denied: str = (
        "SmartHeat: Der Server lehnt die Zugangsdaten ab, die Heizkurve wird nicht mehr angepasst. "
        "Home Assistant fordert zur erneuten Anmeldung bei SmartHeat auf (Einstellungen → Geräte & Dienste)."
    )
    # Meldung "Datentraeger nicht beschreibbar" (datentraeger.report).
    storage_failed: str = (
        "SmartHeat: Der Datenträger des Home-Assistant-Systems ist nicht beschreibbar (voll oder "
        "schreibgeschützt). Die Heizungsregelung pausiert, die Anlage behält ihre letzten Werte."
    )
    # Praefix der Zustell-Meldungen (delivery.notification_text).
    delivery_prefix: str = "Heizungsbrücke"
    # Logs bei abgelehnter MQTT-Anmeldung (abo._report_rejection); relogin_log_status hat einen Platzhalter.
    relogin_log_rejected: str = (
        "MQTT-Anmeldung abgelehnt, der Server kennt diese Zugangsdaten nicht mehr - SmartHeat-Integration neu anmelden."
    )
    relogin_log_status: str = (
        "MQTT-Anmeldung vom Broker abgelehnt, Abo-Status ist aber '%s' - Zugangsdaten pruefen (ggf. "
        "SmartHeat-Integration neu anmelden)."
    )
    # Log, wenn die Signalquelle nicht antwortet (battery, room_sensors).
    source_unavailable_log: str = "Home Assistant nicht erreichbar"
    # Hinweis, wenn accounts_api_base_url fehlt (options.resolve_accounts_api_base_url).
    accounts_url_missing_hint: str = "bitte die SmartHeat-Integration neu einrichten"


HA_TEXTS = HostTexts()
