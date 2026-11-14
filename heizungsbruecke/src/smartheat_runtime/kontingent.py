"""Meldung zum erschoepften Abfragekontingent der Quelle (Gateway: Cloud-Treiber), nach dem Muster von
aussentemperatur. Eine Quelle mit Kontingent bietet `kontingent_meldung() -> str | None` (Kundentext, solange das
Kontingent erschoepft ist und die Daten veraltet sind, sonst None); die Quelle des HA-Hosts hat keins, dann gibt es nichts
zu pruefen. Nicht kritisch (nur Push, ein Wechsel je Ende der Sperre); der Schluessel ist Freitext (V1, device_checks des
Servers), kein Vertrag."""
from smartheat_runtime.notifier import STATE_OK

KEY = "kontingent"
STATE_ERSCHOEPFT = "erschoepft"
OK_MESSAGE = "SmartHeat: Das Abfragekontingent der Quelle ist wieder frei."


def check(rt) -> None:
    abfrage = getattr(rt.signals, "kontingent_meldung", None)
    if abfrage is None:
        return
    text = abfrage()
    if text is None:
        rt.notifier.notify(KEY, STATE_OK, OK_MESSAGE, critical=False)
        return
    rt.notifier.notify(KEY, STATE_ERSCHOEPFT, f"SmartHeat: {text}", critical=False)
