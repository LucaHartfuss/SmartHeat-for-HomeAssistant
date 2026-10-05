"""Status- und Meldungs-Sinks des HA-Hosts (Spec SHG 3.2): Status als HA-Ereignis `smartheat_status` an die
Integration, Meldungen als Push an die Notify-Dienste und als persistent_notification. Ereignisname, Titel und
Benachrichtigungs-IDs sind Cross-Repo-Vertrag mit der Integration (Contract-Check 14)."""
import logging
import re

logger = logging.getLogger(__name__)

EVENT_TYPE = "smartheat_status"
TITLE = "SmartHeat"


def notification_id(key: str) -> str:
    """Stabile ID je Schluessel: eine neue Meldung ersetzt die vorige, statt sich zu stapeln."""
    return "smartheat_" + re.sub(r"[^a-z0-9_]", "_", key.lower())


class HaStatusSink:
    def __init__(self, ha_api) -> None:
        self._ha_api = ha_api

    def publish(self, event: dict) -> None:
        self._ha_api.fire_event(EVENT_TYPE, event)


class HaNotifySink:
    def __init__(self, ha_api, services: list[str]) -> None:
        self._ha_api = ha_api
        self._services = list(services)

    def push(self, key: str, message: str) -> None:
        for service in self._services:
            try:
                self._ha_api.send_notification(service, message)
            except Exception:
                logger.warning("Push-Benachrichtigung '%s' an %s konnte nicht gesendet werden", key, service)

    def show(self, key: str, message: str) -> None:
        self._ha_api.create_persistent_notification(TITLE, message, notification_id(key))

    def withdraw(self, key: str) -> None:
        self._ha_api.dismiss_persistent_notification(notification_id(key))
