"""Meldungen an den Kunden (Spec TP6, 3.4): Push (HA: an alle konfigurierten Notify-Dienste), bei
kritischen Anlaessen zusaetzlich eine offene Meldung (HA: persistent_notification), die beim
Rueckwechsel auf "ok" wieder verschwindet. Gemeldet wird nur bei einem Zustandswechsel je Schluessel. Der Zustand liegt
in BridgeState.notify_states (backup.json) und uebersteht Neustarts: kein Meldungssturm, wenn das
Add-on mit demselben Fehler erneut startet.

HA haelt persistent_notifications nur im Speicher. Deshalb bleibt der Text jeder offenen kritischen
Meldung in BridgeState.notify_messages, und republish_persistent() legt sie nach einem HA-Neustart
neu an -- ohne erneuten Push.

Die sieben Hinweis-Kategorien (HINT_CATEGORIES, Option notify_hints_off) lassen sich einzeln
abschalten: der Zustand wird weiter verfolgt und geloggt (Grundlage der Hinweise im Status), nur
der Push entfaellt. Kritische Meldungen sind nie abschaltbar."""
import logging

from smartheat_runtime.ports import NotifySink

logger = logging.getLogger(__name__)

STATE_OK = "ok"

# Abschaltbare Hinweis-Kategorien (Spec TP7 3.4). Muss zum config.yaml-Schema und zu const.py der
# Integration passen (Contract-Check).
HINT_CATEGORIES = (
    "raumfuehler", "batterie", "manueller_eingriff", "quellwechsel", "therme", "schreibbudget", "schreibzaehler",
)


def category(key: str) -> str:
    """Kategorie eines Meldeschluessels: der Teil vor ":" (raumfuehler:<entity> -> raumfuehler)."""
    return key.partition(":")[0]


class Notifier:
    def __init__(self, store, sink: NotifySink, hints_off=()) -> None:
        self._store = store
        self._sink = sink
        self._hints_off = frozenset(hints_off)

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

    def notify(self, key: str, state: str, message: str, *, critical: bool, silent_ok: bool = False) -> bool:
        """True bei einem Zustandswechsel. Jeder Kanal ist best effort; ein Schreibfehler des
        Zustands verhindert die Meldung nicht (sie kaeme sonst nie an). Ohne Push (nur Log):
        abgeschaltete Hinweis-Kategorien und, mit silent_ok, der Wechsel auf "ok"."""
        if state == self.state(key):
            return False
        states = dict(self._store.state.notify_states)
        messages = dict(self._store.state.notify_messages)
        if state == STATE_OK:
            states.pop(key, None)
        else:
            states[key] = state
        if critical and state != STATE_OK:
            messages[key] = message
        else:
            messages.pop(key, None)
        try:
            self._store.update(notify_states=states, notify_messages=messages)
        except Exception:
            logger.exception("Meldezustand '%s' konnte nicht gespeichert werden, Meldung geht trotzdem raus", key)
        if state == STATE_OK:
            logger.info(message)
        else:
            logger.warning(message)
        muted = (not critical and category(key) in self._hints_off) or (silent_ok and state == STATE_OK)
        if not muted:
            self._sink.push(key, message)
        if critical:
            self._update_persistent(key, state, message)
        return True

    def refresh_persistent(self, key: str, message: str) -> None:
        """Legt die HA-Benachrichtigung eines unveraenderten, nicht-"ok" Zustands mit dem
        aktuellen Text erneut an (ersetzt die vorige je Schluessel), ohne Push. Fuer einen
        Startfehler, der bei jedem Start erneut auftritt: nach einem Host-Neustart fehlt die
        Benachrichtigung sonst, weil HA sie nicht speichert."""
        state = self.state(key)
        if state == STATE_OK:
            return
        if self._store.state.notify_messages.get(key) != message:
            try:
                self._store.update(notify_messages={**self._store.state.notify_messages, key: message})
            except Exception:
                logger.exception("Meldetext '%s' konnte nicht gespeichert werden", key)
        self._update_persistent(key, state, message)

    def republish_persistent(self) -> None:
        """Legt die HA-Benachrichtigungen aller offenen kritischen Meldungen neu an, ohne Push
        (bei jedem (Wieder-)Verbinden mit HA). Ein Schluessel ohne gespeicherten Text (per seed()
        uebernommen) wird uebersprungen."""
        messages = self._store.state.notify_messages
        for key, state in self._store.state.notify_states.items():
            if state != STATE_OK and key in messages:
                self._update_persistent(key, state, messages[key])

    def clear_all(self, keep=()) -> None:
        """Abmelden (Spec TP7 3.3): alle offenen HA-Benachrichtigungen ausser `keep` entfernen und
        ihre Meldezustaende leeren, ohne Push."""
        states = self._store.state.notify_states
        for key, state in states.items():
            if state != STATE_OK and key not in keep:
                self._update_persistent(key, STATE_OK, "")
        kept_states = {key: state for key, state in states.items() if key in keep}
        kept_messages = {key: text for key, text in self._store.state.notify_messages.items() if key in keep}
        try:
            self._store.update(notify_states=kept_states, notify_messages=kept_messages)
        except Exception:
            logger.exception("Meldezustaende konnten beim Abmelden nicht geleert werden")

    def _update_persistent(self, key: str, state: str, message: str) -> None:
        try:
            if state == STATE_OK:
                self._sink.withdraw(key)
            else:
                self._sink.show(key, message)
        except Exception:
            logger.warning("Offene Meldung '%s' konnte nicht aktualisiert werden", key)
