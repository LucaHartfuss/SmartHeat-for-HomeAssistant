"""Energie-Normalisierung (Hersteller-Abstraktion Spec 6.5, Plan 3b). Der Server rechnet mit monoton wachsenden
Zaehlerstaenden je Kanal (generic_energy_log.cumulative_value). Vaillant liefert solche Summen ("total": unveraendert
durchgereicht, kein Zustand). Weishaupt und Viessmann liefern Tageszaehler ("daily", Rueckgang auf 0 um Mitternacht):
daraus fuehrt der Kern je Kanal eine monoton wachsende Summe.

Regel "daily" je Kanal mit Zustand {"raw": letzter Rohwert, "sum": Summe}:
- erster Wert ohne Zustand: Summe = Rohwert (Basis; der Server rechnet nur Differenzen);
- Rohwert >= letzter: Summe += Differenz;
- Rohwert < RESET_FRACTION x letzter: Tagesruecksetzung, Summe += Rohwert;
- sonst (kleiner Rueckgang, Messrauschen): Summe bleibt, der Rohwert wird uebernommen.
Ein nicht lesbarer oder nicht endlicher Rohwert kommt hier nicht an (telemetry laesst den Kanal weg); sein Zustand
bleibt. Rein: Zustand rein, Zustand raus."""
import math
from collections.abc import Mapping

from smartheat_core.binding import ENERGY_DAILY, ENERGY_TOTAL

# Annahme bis zur Inventur: ein Tageszaehler faellt um Mitternacht auf (nahe) 0, Messrauschen ist viel kleiner.
RESET_FRACTION = 0.5


def _step(previous: Mapping[str, float] | None, raw: float) -> dict[str, float]:
    if previous is None:
        return {"raw": raw, "sum": raw}
    last, total = previous["raw"], previous["sum"]
    if raw >= last:
        return {"raw": raw, "sum": total + raw - last}
    if raw < RESET_FRACTION * last:
        return {"raw": raw, "sum": total + raw}
    return {"raw": raw, "sum": total}


def normalize(
    kind: str, state: Mapping[str, Mapping[str, float]], raw: Mapping[str, float],
) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    """(zu sendende Werte je Kanal, neuer Zustand). TOTAL: Rohwerte unveraendert, Zustand unveraendert."""
    if kind == ENERGY_TOTAL:
        return dict(raw), {channel: dict(entry) for channel, entry in state.items()}
    if kind != ENERGY_DAILY:
        raise ValueError(f"unbekannte Zaehlerart {kind!r}")
    new_state = {channel: dict(entry) for channel, entry in state.items()}
    sent: dict[str, float] = {}
    for channel, value in raw.items():
        if not math.isfinite(value):
            continue
        new_state[channel] = _step(state.get(channel), value)
        sent[channel] = round(new_state[channel]["sum"], 6)
    return sent, new_state
