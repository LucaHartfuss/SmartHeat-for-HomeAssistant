"""Status-Entity des Add-ons (Spec TP6 3.6), gesetzt per POST /api/states. Der Wizard der
Integration wartet darauf und erkennt am Attribut setup_id, dass der Status zu seinem Lauf gehoert.
Solche Zustaende ueberleben keinen HA-Neustart; deshalb wird der letzte Zustand bei jedem
(Wieder-)Verbinden des WS-Trigger-Clients erneut gesetzt (republish)."""
import logging
import re

logger = logging.getLogger(__name__)

# Muss zu `version` in config.yaml passen (tests/test_config_yaml.py).
ADDON_VERSION = "0.19.0"

STATUS_STARTET = "startet"
STATUS_BEREIT = "bereit"
STATUS_KONFIGURATIONSFEHLER = "konfigurationsfehler"

# Attributnamen; die Integration definiert dieselben Konstanten in const.py (Contract-Check).
STATUS_ATTR_SETUP_ID = "setup_id"
STATUS_ATTR_GRUND = "grund"


def status_entity_id(tenant_id: str) -> str:
    """Gleiche Regel wie const.status_entity_id der Integration (Contract-Check)."""
    return f"sensor.smartheat_{re.sub(r'[^a-z0-9_]', '_', tenant_id.lower())}_status"


class StatusReporter:
    def __init__(self, ha_api, tenant_id: str, setup_id: str | None) -> None:
        self._ha_api = ha_api
        self.entity_id = status_entity_id(tenant_id)
        self._setup_id = setup_id if isinstance(setup_id, str) and setup_id else None
        self.state: str | None = None
        self._grund: str | None = None

    def set(self, state: str, grund: str | None = None) -> None:
        self.state, self._grund = state, grund
        self.republish()

    def republish(self) -> None:
        if self.state is None:
            return
        attributes = {"friendly_name": "SmartHeat Status", "addon_version": ADDON_VERSION}
        if self._setup_id:
            attributes[STATUS_ATTR_SETUP_ID] = self._setup_id
        if self._grund:
            attributes[STATUS_ATTR_GRUND] = self._grund
        try:
            self._ha_api.set_state(self.entity_id, self.state, attributes)
        except Exception:
            logger.warning("Status-Entity %s konnte nicht auf '%s' gesetzt werden", self.entity_id, self.state)
