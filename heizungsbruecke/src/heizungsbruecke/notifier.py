"""Meldungen an den Kunden (Spec TP6, 3.4): Push an alle konfigurierten Notify-Dienste, bei
kritischen Anlaessen zusaetzlich eine HA-persistent_notification, die beim Rueckwechsel auf "ok"
wieder verschwindet. Gemeldet wird nur bei einem Zustandswechsel je Schluessel. Der Zustand liegt
in BridgeState.notify_states (backup.json) und uebersteht Neustarts: kein Meldungssturm, wenn das
Add-on mit demselben Fehler erneut startet."""
import logging
import re

logger = logging.getLogger(__name__)

STATE_OK = "ok"
TITLE = "SmartHeat"


def notification_id(key: str) -> str:
    """Stabile ID je Schluessel: eine neue Meldung ersetzt die vorige, statt sich zu stapeln."""
    return "smartheat_" + re.sub(r"[^a-z0-9_]", "_", key.lower())


class Notifier:
    def __init__(self, store, ha_api, services: list[str]) -> None:
        self._store = store
        self._ha_api = ha_api
        self._services = list(services)

    def state(self, key: str) -> str:
        return self._store.state.notify_states.get(key, STATE_OK)

    def seed(self, key: str, state: str) -> None:
        """Uebernimmt einen anderswo persistierten Zustand (failsafe_state.json) ohne Meldung,
        wenn der Schluessel noch fehlt: sonst ginge nach einem Update oder einem gescheiterten
        Schreiben die Entwarnung verloren."""
        if state == STATE_OK or key in self._store.state.notify_states:
            return
        try:
            self._store.update(notify_states={**self._store.state.notify_states, key: state})
        except Exception:
            logger.exception("Meldezustand '%s' konnte nicht gespeichert werden", key)

    def notify(self, key: str, state: str, message: str, *, critical: bool) -> bool:
        """True, wenn gemeldet wurde. Jeder Kanal ist best effort; ein Schreibfehler des
        Zustands verhindert die Meldung nicht (sie kaeme sonst nie an)."""
        if state == self.state(key):
            return False
        states = dict(self._store.state.notify_states)
        if state == STATE_OK:
            states.pop(key, None)
        else:
            states[key] = state
        try:
            self._store.update(notify_states=states)
        except Exception:
            logger.exception("Meldezustand '%s' konnte nicht gespeichert werden, Meldung geht trotzdem raus", key)
        if state == STATE_OK:
            logger.info(message)
        else:
            logger.warning(message)
        for service in self._services:
            try:
                self._ha_api.send_notification(service, message)
            except Exception:
                logger.warning("Push-Benachrichtigung '%s' an %s konnte nicht gesendet werden", key, service)
        if critical:
            self._update_persistent(key, state, message)
        return True

    def _update_persistent(self, key: str, state: str, message: str) -> None:
        nid = notification_id(key)
        try:
            if state == STATE_OK:
                self._ha_api.dismiss_persistent_notification(nid)
            else:
                self._ha_api.create_persistent_notification(TITLE, message, nid)
        except Exception:
            logger.warning("HA-Benachrichtigung '%s' konnte nicht aktualisiert werden", key)
