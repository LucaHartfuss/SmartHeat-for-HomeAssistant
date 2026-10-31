"""Handgriffe der Bewohner an der Welt: Raumtemperatur setzen, Wunsch am Thermostat aendern. Beides geht wie in HA
ueber einen Zustandswechsel, den der (Fake-)WebSocket als Trigger an das Add-on meldet; verarbeitet wird er in der
naechsten Runde. Gemischt in `welt.Anlage` ein (braucht `ha`, `trigger`)."""
import io
import os

from anlagen import RAUMFUEHLER, THERMOSTAT

import heizungsbruecke.__main__ as main_module
from smartheat_device import device, identity
from smartheat_device.prozess import Neustart

ROOM_TARGET = THERMOSTAT


class Handgriffe:
    def _zustandswechsel(self, entity: str, wert, user: str | None, attribut: str | None = None) -> None:
        """Zustand (oder Attribut) von `entity` wechselt auf `wert`; HA meldet es als Trigger."""
        if attribut is None:
            self.ha.states[entity] = wert
            to_state = {"state": str(wert), "attributes": {}, "context": {"user_id": user, "parent_id": None}}
        else:
            self.ha.states[f"{entity}::{attribut}"] = wert
            to_state = {"state": str(self.ha.states[entity]), "attributes": {attribut: wert},
                        "context": {"user_id": user, "parent_id": None}}
        self.trigger[-1].kwargs["on_trigger_event"]({"platform": "state", "entity_id": entity, "attribute": attribut,
                                                     "to_state": to_state})

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
        self._zustandswechsel(ROOM_TARGET, wert, "bewohner", "temperature")

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

    def laufen_ueber_neustarts(self, sekunden: float, takt: float = 5) -> None:
        """Wie `laufen`; fordert der Prozess dabei einen Neustart an, startet die Welt neu und die Zeit vergeht weiter."""
        for _ in range(int(sekunden // takt)):
            try:
                self.runde(takt)
            except Neustart:
                self.neu_starten()

    def meldetexte(self) -> list[str]:
        """Alle Texte, die der Bewohner in HA sieht: offene Meldungen und Push, in der Reihenfolge ihres Erscheinens."""
        return [m[2] for m in self.ha.meldungen if m[0] in ("dauerhaft", "push")]

    def zurueckgenommen(self) -> list[str]:
        """IDs der offenen Meldungen, die HA wieder entfernt wurden."""
        return [m[1] for m in self.ha.meldungen if m[0] == "zurueckgenommen"]

    def wanduhr_vor(self, sekunden: float) -> None:
        """Die Wanduhr springt um `sekunden` vor (Zeitsynchronisation nach langem Stillstand); die monotone Uhr laeuft
        unveraendert weiter."""
        self.uhr.wand_versatz += sekunden

    def neu_starten(self) -> None:
        """Der Prozess startet neu (wie `exec` nach prozess.Neustart): Geraetekern und Laufzeit auf denselben Dateien,
        neuer Link, neue Verbindung zu HA. Fordert der neue Start selbst einen Neustart an (nach dem Zuruecksetzen
        folgt das Aufraeumen), startet die Welt noch einmal, wie der Supervisor es taete."""
        for _ in range(3):
            try:
                self._starten_im_neuen_prozess()
                return
            except Neustart:
                continue
        raise AssertionError("der Prozess startet immer wieder neu")

    def _starten_im_neuen_prozess(self) -> None:
        trigger_vorher = len(self.trigger)
        geraet, status = self._prozess()
        self.bridge, _ = main_module.start(geraet, self.device, self.ha, status, clock=self.uhr)
        self.bridge.worker.run_pending()
        self._verbinden()
        if len(self.trigger) > trigger_vorher:  # im Ruhezustand (abgemeldet, Abo beendet) gibt es keinen Trigger
            self.trigger[-1].kwargs["on_connected"]()
        self.runde()

    def geraete_id(self) -> str:
        """Die Geraete-ID, die auch die Integration kennt (Teil des Befehls `entfernen <geraet_id>`)."""
        return identity.load_or_create(self.daten / device.DEVICE_DIR).device_id

    def stdin_sendet(self, zeile: bytes) -> None:
        """Die Integration schreibt `zeile` an das stdin des Add-ons (Pipe); der Lesethread stellt den Befehl in das
        Postfach des Geraetekerns, verarbeitet wird er in der naechsten Runde."""
        lesen, schreiben = os.pipe()
        quelle = io.FileIO(lesen, "rb")
        thread = main_module.stdin_anhaengen(self._geraet, self.device, quelle, self.ha)
        with os.fdopen(schreiben, "wb") as ende:
            ende.write(zeile)
        thread.join(5)
        assert not thread.is_alive()
        quelle.close()
