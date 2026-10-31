"""Handgriffe der Bewohner an der Welt: Raumtemperatur setzen, Wunsch am Thermostat aendern. Beides geht wie in HA
ueber einen Zustandswechsel, den der (Fake-)WebSocket als Trigger an das Add-on meldet; verarbeitet wird er in der
naechsten Runde. Gemischt in `welt.Anlage` ein (braucht `ha`, `trigger`)."""
from anlagen import RAUMFUEHLER

ROOM_TARGET = "sensor.room_target"


class Handgriffe:
    def _zustandswechsel(self, entity: str, wert, user: str | None) -> None:
        self.ha.states[entity] = wert
        to_state = {"state": str(wert), "attributes": {}, "context": {"user_id": user, "parent_id": None}}
        self.trigger[-1].kwargs["on_trigger_event"]({"platform": "state", "entity_id": entity, "to_state": to_state})

    def raum(self, wert: float) -> None:
        """Der Raumfuehler zeigt `wert` (gemeldet wie jeder Zustandswechsel)."""
        self._zustandswechsel(RAUMFUEHLER, wert, None)

    def wunsch(self, wert: float) -> None:
        """Ein Bewohner stellt am Thermostat `wert` ein (nach der Entprellung von HA: 10 s stabil)."""
        self._zustandswechsel(ROOM_TARGET, wert, "bewohner")

    def laufen(self, sekunden: float, takt: float = 5) -> None:
        """Die Zeit vergeht in Runden zu `takt` Sekunden (Geraetetakt und Worker laufen mit)."""
        for _ in range(int(sekunden // takt)):
            self.runde(takt)

    def meldetexte(self) -> list[str]:
        """Alle Texte, die der Bewohner in HA sieht: offene Meldungen und Push, in der Reihenfolge ihres Erscheinens."""
        return [m[2] for m in self.ha.meldungen if m[0] in ("dauerhaft", "push")]

    def zurueckgenommen(self) -> list[str]:
        """IDs der offenen Meldungen, die HA wieder entfernt wurden."""
        return [m[1] for m in self.ha.meldungen if m[0] == "zurueckgenommen"]
