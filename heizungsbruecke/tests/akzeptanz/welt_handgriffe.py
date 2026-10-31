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

    def ha_verbindet(self) -> None:
        """Die Verbindung zu HA steht (wieder): das Add-on liest Wunsch und Fuehler neu und prueft lokal, ob ein Tick
        faellig ist."""
        self.trigger[-1].kwargs["on_connected"]()

    def app_verstellt(self, entity: str, wert: float) -> None:
        """Die Hersteller-App stellt `entity` auf `wert` (die Cloud-Integration meldet es als Zustandswechsel ohne
        Bewohner)."""
        self._zustandswechsel(entity, wert, None)

    def wunsch(self, wert: float) -> None:
        """Ein Bewohner stellt am Thermostat `wert` ein (nach der Entprellung von HA: 10 s stabil)."""
        self._zustandswechsel(ROOM_TARGET, wert, "bewohner")

    def raum_unlesbar(self) -> None:
        """Der Raumfuehler meldet `unavailable` (gemeldet wie jeder Zustandswechsel)."""
        self._zustandswechsel(RAUMFUEHLER, "unavailable", None)

    def wanduhr_zurueck(self, sekunden: float) -> None:
        """Die Wanduhr springt um `sekunden` zurueck; die monotone Uhr laeuft unveraendert weiter."""
        self.uhr.wand_versatz -= sekunden

    def vergehen(self, sekunden: float, hebel: dict, fuehler: float, takt: float = 60) -> None:
        """Zeit vergeht in Runden zu `takt`: der Server antwortet jedes Mal mit `hebel`, der Raumfuehler meldet weiter
        `fuehler` (kalt, aendert sich nicht)."""
        ende = self.uhr() + sekunden
        while self.uhr() < ende:
            self.raum(fuehler)
            self.server_antwortet(hebel)
            self.runde(min(takt, ende - self.uhr()))

    def konfiguration_senden(self, version: int, inhalt: dict) -> None:
        """Der Server sendet eine Konfiguration (down/config); verarbeitet wird sie im naechsten Geraetetakt."""
        self.link.empfangen("down/config", {"schema": 1, "version": version, **inhalt})

    def ergebnis(self, version: int) -> dict:
        """Das Ergebnis (up/result) des Geraets zur Konfiguration `version`: ok und, wenn abgelehnt, der Grund."""
        treffer = [r for r in self.link.of("up/result") if r["dokument"] == "konfiguration" and r["version"] == version]
        assert treffer, f"kein Ergebnis zur Konfiguration {version}"
        return {"ok": treffer[-1]["ok"], "grund": treffer[-1].get("error", {}).get("grund")}

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
