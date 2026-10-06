"""Datentraeger nicht beschreibbar (TP12b, AU-005): Meldung an den Kunden. Der Zustand selbst steht
in StateStore.storage_failed (nur im Speicher); gemeldet wird nach jedem Worker-Ereignis
(app._after_each), entwarnt beim ersten erfolgreichen Schreiben (StateStore.flush im Takt
EV_HEALTH). Die Zustellmaschine meldet einen Datentraeger-Datenfehler nicht selbst (delivery.py),
sonst kaeme dieselbe Stoerung doppelt."""
from smartheat_runtime.notifier import STATE_OK
from smartheat_runtime.texts import HA_TEXTS, HostTexts

KEY = "datentraeger"
STATE_FAILED = "nicht_beschreibbar"
FAILED_MESSAGE = (
    "SmartHeat: Der Datenträger des Home-Assistant-Systems ist nicht beschreibbar (voll oder "
    "schreibgeschützt). Die Heizungsregelung pausiert, die Anlage behält ihre letzten Werte."
)
OK_MESSAGE = "SmartHeat: Der Datenträger ist wieder beschreibbar, die Heizungsregelung läuft wieder."


def report(store, notifier, texts: HostTexts = HA_TEXTS) -> None:
    """Meldet nur bei einem Zustandswechsel (Notifier); kritisch, also Push und HA-Benachrichtigung."""
    if store.storage_failed:
        notifier.notify(KEY, STATE_FAILED, texts.storage_failed, critical=True)
    else:
        notifier.notify(KEY, STATE_OK, OK_MESSAGE, critical=True)
