"""Inventur senden (Spec 5b 2 und 3.4): der Host liefert die Daten (Kandidaten je Rolle, Gateway: Treiber-Ergebnis
und Zigbee-Fuehler), das Paket teilt sie in Nachrichten unter der Nachrichtengrenze (AN-10: 120 KB, Reserve fuer den
Umschlag). Listen werden in Reihenfolge ueber die Teile verteilt, alles andere steht im ersten Teil: der Dienst haengt
Listen in Teil-Reihenfolge aneinander, sonst gilt der erste Teil (Plan S2). Hoechstens TEILE_MAX Teile.
Gemessen wird kompakt mit ensure_ascii=False (UTF-8-Bytes); der Sender muss genau so serialisieren
(json.dumps(payload, separators=(",", ":"), ensure_ascii=False)), sonst gilt die Grenze nicht."""
import json

from smartheat_device import wire

TEILE_MAX = 8
RESERVE_BYTES = 1024
LIMIT_BYTES = wire.MAX_MESSAGE_BYTES - RESERVE_BYTES


class InventurZuGross(ValueError):
    """Die Inventur passt nicht in TEILE_MAX Nachrichten oder ein Eintrag allein ist zu gross."""


def groesse(value) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode())


def teilen(daten: dict, limit: int = LIMIT_BYTES) -> list[dict]:
    if groesse(daten) <= limit:
        return [dict(daten)]
    # Einzelwerte und leere Listen stehen von Anfang an im ersten Teil, damit er spaeter nicht ueber die Grenze waechst
    teile = [{key: value for key, value in daten.items() if not isinstance(value, list) or not value}]
    groessen = [groesse(teile[0])]  # laufende Bytezahl je Teil, ohne den Teil neu zu serialisieren
    if groessen[0] > limit:
        raise InventurZuGross("die Einzelwerte der Inventur sind zu gross")
    for key, values in daten.items():
        if not isinstance(values, list) or not values:
            continue
        kopf = groesse(key) + 1  # "key":
        for item in values:
            klein = groesse(item)
            if kopf + 2 + klein + 2 > limit:  # {"key":[item]}
                raise InventurZuGross(f"ein Eintrag in {key} ist zu gross")
            letzter = teile[-1]
            # ",item" an eine laufende Liste, sonst "[,]"key":[item]" als neuer Schluessel
            zuwachs = klein + 1 if key in letzter else kopf + 2 + klein + (1 if letzter else 0)
            if groessen[-1] + zuwachs <= limit:
                letzter.setdefault(key, []).append(item)
                groessen[-1] += zuwachs
            else:
                if len(teile) >= TEILE_MAX:
                    raise InventurZuGross(f"mehr als {TEILE_MAX} Teile")
                teile.append({key: [item]})
                groessen.append(kopf + 2 + klein + 2)
    return teile


def nachrichten(command_id: str | None, daten: dict) -> list[dict]:
    teile = teilen(daten)
    kopf = {"command_id": command_id} if command_id else {}
    return [{"schema": wire.SCHEMA, **kopf, "teil": nummer, "teile": len(teile), "daten": teil}
            for nummer, teil in enumerate(teile, start=1)]
