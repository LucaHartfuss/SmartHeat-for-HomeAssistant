"""Inventur senden (Spec 5b 2 und 3.4): der Host liefert die Daten (Kandidaten je Rolle, Gateway: Treiber-Ergebnis
und Zigbee-Fuehler), das Paket teilt sie in Nachrichten unter der Nachrichtengrenze (AN-10: 120 KB, Reserve fuer den
Umschlag). Listen werden in Reihenfolge ueber die Teile verteilt, alles andere steht im ersten Teil: der Dienst haengt
Listen in Teil-Reihenfolge aneinander, sonst gilt der erste Teil (Plan S2). Hoechstens TEILE_MAX Teile."""
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
    teile = [{key: value for key, value in daten.items() if not isinstance(value, list)}]
    if groesse(teile[0]) > limit:
        raise InventurZuGross("die Einzelwerte der Inventur sind zu gross")
    for key, values in daten.items():
        if not isinstance(values, list):
            continue
        if not values:
            teile[0][key] = []
            continue
        for item in values:
            if groesse({key: [item]}) > limit:
                raise InventurZuGross(f"ein Eintrag in {key} ist zu gross")
            letzter = teile[-1]
            kandidat = {**letzter, key: [*letzter.get(key, []), item]}
            if groesse(kandidat) <= limit:
                teile[-1] = kandidat
            else:
                teile.append({key: [item]})
    if len(teile) > TEILE_MAX:
        raise InventurZuGross(f"{len(teile)} Teile, erlaubt {TEILE_MAX}")
    return teile


def nachrichten(command_id: str | None, daten: dict) -> list[dict]:
    teile = teilen(daten)
    kopf = {"command_id": command_id} if command_id else {}
    return [{"schema": wire.SCHEMA, **kopf, "teil": nummer, "teile": len(teile), "daten": teil}
            for nummer, teil in enumerate(teile, start=1)]
