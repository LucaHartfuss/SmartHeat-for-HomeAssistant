"""Energie-Normalisierung (Hersteller-Abstraktion). Der Server rechnet mit monoton wachsenden
Zaehlerstaenden je Kanal (generic_energy_log.cumulative_value). Alle derzeitigen Bindings (Vaillant/myVAILLANT,
Weishaupt, Viessmann) liefern Tageszaehler ("daily", Rueckgang auf 0 um Mitternacht): daraus fuehrt der
Kern je Kanal eine monoton wachsende Summe. Fuer einen Hersteller mit durchlaufender Summe bleibt "total" (Werte
unveraendert durchgereicht, kein Zustand).

Einheit: die Werte kommen in der Zieleinheit des Kanals an (telemetry rechnet Wh in kWh um).
Der Zustand traegt sie je Kanal mit; ein Zustand mit anderer oder ohne Einheit (aus 1.0.0: Wh ohne Einheit) beginnt neu,
Summen verschiedener Einheiten laufen nie ineinander.

Regel "daily" je Kanal mit Zustand {"raw": letzter Rohwert, "sum": Summe, "einheit": Zieleinheit}:
- erster Wert ohne Zustand: Summe = Rohwert (Basis; der Server rechnet nur Differenzen);
- Rohwert >= letzter: Summe += Differenz;
- Rohwert < RESET_FRACTION x letzter: Tagesruecksetzung, Summe += Rohwert;
- sonst (kleiner Rueckgang, Messrauschen): Summe bleibt, der Rohwert wird uebernommen.
Ein nicht lesbarer oder nicht endlicher Rohwert kommt hier nicht an (telemetry laesst den Kanal weg); sein Zustand
bleibt. Rein: Zustand rein, Zustand raus."""
import math
from collections.abc import Mapping

from smartheat_core.binding import ENERGY_DAILY, ENERGY_TOTAL
from smartheat_core.bindungsparameter import EINHEITEN

# Annahme bis zur Inventur: ein Tageszaehler faellt um Mitternacht auf (nahe) 0, Messrauschen ist viel kleiner.
# Grenze: myVAILLANT aktualisiert die Energiesensoren nur beim Neuladen der Integration ; setzt ein
# Neuladen den Zaehler mitten am Tag zurueck, kann die Summe Verbrauch des Tages vor dem Neuladen unterzaehlen.
RESET_FRACTION = 0.5


def umrechnen(wert: float, einheit: str) -> tuple[float, str]:
    """(Wert in der Zieleinheit, Zieleinheit): Wh -> kWh, kWh und m³ unveraendert. KeyError bei
    einer fremden Einheit."""
    ziel, faktor = EINHEITEN[einheit]
    return wert * faktor, ziel


def _step(previous: Mapping[str, float | str] | None, raw: float) -> dict[str, float]:
    if previous is None:
        return {"raw": raw, "sum": raw}
    last, total = float(previous["raw"]), float(previous["sum"])
    if raw >= last:
        return {"raw": raw, "sum": total + raw - last}
    if raw < RESET_FRACTION * last:
        return {"raw": raw, "sum": total + raw}
    return {"raw": raw, "sum": total}


def normalize(
    kind: str, state: Mapping[str, Mapping[str, float | str]], raw: Mapping[str, float], einheiten: Mapping[str, str],
) -> tuple[dict[str, float], dict[str, dict[str, float | str]]]:
    """(zu sendende Werte je Kanal, neuer Zustand). raw in der Zieleinheit, einheiten: Kanal -> Zieleinheit. TOTAL:
    Rohwerte unveraendert, Zustand unveraendert. DAILY: ein Zustand mit anderer (oder ohne, aus 1.0.0) Einheit
    beginnt neu -- Summen verschiedener Einheiten laufen nie ineinander."""
    if kind == ENERGY_TOTAL:
        return dict(raw), {channel: dict(entry) for channel, entry in state.items()}
    if kind != ENERGY_DAILY:
        raise ValueError(f"unbekannte Zaehlerart {kind!r}")
    new_state = {channel: dict(entry) for channel, entry in state.items()}
    sent: dict[str, float] = {}
    for channel, value in raw.items():
        if not math.isfinite(value):
            continue
        previous = state.get(channel)
        if previous is not None and previous.get("einheit") != einheiten[channel]:
            previous = None
        new_state[channel] = {**_step(previous, value), "einheit": einheiten[channel]}
        sent[channel] = round(float(new_state[channel]["sum"]), 6)
    return sent, new_state
