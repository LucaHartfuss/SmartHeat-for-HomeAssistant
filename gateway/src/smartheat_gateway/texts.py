"""Kundentexte des Gateways (Spec SHG G2 2.1); Gegenstueck zu HA_TEXTS."""
from smartheat_runtime.texts import HostTexts

SHG_TEXTS = HostTexts(
    access_denied=(
        "SmartHeat: Der Server lehnt die Zugangsdaten ab, die Heizkurve wird nicht mehr angepasst. "
        "Bitte das Gateway im SmartHeat-Portal neu anmelden."
    ),
    storage_failed=(
        "SmartHeat: Der Datenträger des Gateways ist nicht beschreibbar (voll oder schreibgeschützt). "
        "Die Heizungsregelung pausiert, die Anlage behält ihre letzten Werte."
    ),
    delivery_prefix="SmartHeat-Gateway",
    relogin_log_rejected=(
        "MQTT-Anmeldung abgelehnt, der Server kennt diese Zugangsdaten nicht mehr - Gateway im Portal neu anmelden."
    ),
    relogin_log_status=(
        "MQTT-Anmeldung vom Broker abgelehnt, Abo-Status ist aber '%s' - Zugangsdaten pruefen (ggf. Gateway im Portal "
        "neu anmelden)."
    ),
    source_unavailable_log="Signalquelle nicht erreichbar",
    accounts_url_missing_hint="bitte das Gateway im SmartHeat-Portal neu einrichten",
)
