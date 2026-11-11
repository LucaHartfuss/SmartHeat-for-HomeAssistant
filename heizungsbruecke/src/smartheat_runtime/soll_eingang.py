"""Soll-Eingang, hostneutral: Bedienung vom Server (portal), Eingabe an der Soll-Quelle
(thermostat; HA: Soll-Entity) und lokale Eingabe (lokal; HA: Ereignis smartheat_wunsch) gehen als EV_TARGET_INPUT in den
Worker. Der Soll-Speicher schreibt, der Debouncer beendet die Eingabe (Gateway 10 s; HA 0 s, dort entprellt der
HA-Trigger mit for: 10 s). Erst dann geht ein Wunsch einmal an den Geraetekern (begrenzt auf den Bedienbereich); der
begrenzte Wert gilt sofort und wird an die Soll-Quelle zurueckgeschrieben, danach EV_LOCAL_CHECK(room_target_fired).
Eine lokale Eingabe gleicht die Soll-Quelle zusaetzlich an. Soll-Quelle aus (Modus off oder ein Soll bis
FROSTSCHUTZ_BIS; ein unbrauchbarer Wert der Quelle setzt oder beendet das nicht): kein Wunsch, auch eine lokale
Eingabe wird verworfen (5c F/Q3), Meldung ueber soll_quelle_aus, eine neue Bedienung wird nicht geschrieben; wieder an:
das geltende Soll geht an die Quelle, sofern sie etwas anderes zeigt (Echo-Fenster, offener Schreibbefehl). Die
Quelle selbst (Zigbee, HA-Entity) liefert der Host ueber SollQuelle."""
import logging
from collections.abc import Callable
from typing import Protocol

from smartheat_core.zahlen import ist_endliche_zahl
from smartheat_runtime.debounce import Debouncer
from smartheat_runtime.plausibility import ROOM_TEMP_RANGE, is_plausible
from smartheat_runtime.runtime import EV_LOCAL_CHECK
from smartheat_runtime.soll_speicher import SOURCE_LOKAL, SOURCE_PORTAL, TargetStore
from smartheat_runtime.worker import Event, RegulationWorker

logger = logging.getLogger(__name__)

EV_TARGET_INPUT = "shg_target_input"
EV_SOLL_MODUS = "shg_soll_modus"
#: Meldet der Laufzeit jeden Wechsel von Soll-Quelle aus/an ({"aus": bool}); der Kaelteschutz rechnet dann ohne Soll.
EV_SOLL_QUELLE = "shg_soll_quelle"
#: Ende der Regelung (Abo-Ende, ausser Betrieb): die Soll-Quelle zeigt danach den gemerkten Wunsch, nie weniger.
EV_SOLL_ENDE = "shg_soll_ende"
#: Das Soll-Ende ist abgearbeitet (geschrieben, nicht noetig oder nicht Sache der Regelung): die Laufzeit loescht
#: den Merker "Soll-Ende offen". Ohne diese Meldung bleibt er offen und der naechste Start holt es nach.
EV_SOLL_ENDE_ERLEDIGT = "shg_soll_ende_erledigt"
#: Wiederholung eines Soll-Endes, das noch nicht gelang (Anzeige unbekannt oder Schreiben gescheitert).
EV_SOLL_ENDE_WIEDERHOLEN = "shg_soll_ende_wiederholen"
SOLL_ENDE_WIEDERHOLUNG_S = 60.0
DEBOUNCE_KIND = "shg_target_debounce"
#: Ein Soll bis zu diesem Wert ist die Frostschutz-Stellung der Soll-Quelle, kein Bedienwunsch.
FROSTSCHUTZ_BIS = 7.0
MODUS_AUS = "off"


class SollQuelle(Protocol):
    def schreiben(self, value: float) -> bool:
        """Soll an die Quelle schreiben; False, wenn sie nicht schreibbar ist (dann kein Echo-Fenster)."""
        ...

    def angezeigt(self) -> float | None:
        """Was die Quelle gerade zeigt; None = unbekannt."""
        ...

    def aus(self) -> bool:
        """Die Quelle zeigt „aus“ (Modus off): sie gehoert dem Kunden."""
        ...


def ende_schreiben(wunsch: float, quelle: SollQuelle, *,
                   geschrieben: Callable[[float], None] | None = None) -> bool:
    """Soll-Ende an der Quelle: sie zeigt danach den gemerkten Wunsch, nie weniger. True = erledigt (nichts zu tun oder
    geschrieben), False = spaeter erneut (Anzeige unbekannt oder Schreiben gescheitert). Bei unbekannter Anzeige wird
    nicht geschrieben: ein hoeherer Wert des Kunden koennte dort stehen. Zeigt die Quelle gleich viel oder mehr, bleibt
    sie; bei „aus“ oder einer Frostschutz-Stellung gehoert sie dem Kunden. `geschrieben` bekommt nach einem Schreiben
    den vorher angezeigten Wert."""
    if quelle.aus():
        return True
    angezeigt = quelle.angezeigt()
    if angezeigt is None:
        return False
    if angezeigt >= wunsch or angezeigt <= FROSTSCHUTZ_BIS:
        return True
    if not quelle.schreiben(wunsch):
        return False
    if geschrieben is not None:
        geschrieben(angezeigt)
    return True


class SollEingang:
    """Nur im Worker-Thread."""

    def __init__(self, worker: RegulationWorker, store: TargetStore, quelle: SollQuelle | None, *,
                 wunsch: Callable[[float, str | None, str | None], float],
                 soll_quelle_aus: Callable[[bool, float | None], None], entprellen_s: float) -> None:
        self._worker, self._store, self._quelle = worker, store, quelle
        self._wunsch, self._soll_quelle_aus = wunsch, soll_quelle_aus
        self._aus: set[str] = set()  # Gruende: "modus", "frost"
        self._wunsch_offen = False
        self._lokal_offen = False
        self._herkunft: tuple[str | None, str | None] = (None, None)
        self._vor_wunsch: tuple[float, str] | None = None  # Soll und Quelle vor dem ersten offenen Wunsch
        self._unbrauchbar_gemeldet: set[str] = set()  # Fehlerarten mit WARNING seit dem letzten brauchbaren Wert
        self._ende_wartet = False  # ein Soll-Ende kam bei offenem Wunsch: nach dem Entprellen nachholen
        self._ende_geplant = False  # eine Wiederholung des Soll-Endes ist eingeplant (eine Kette)
        self._debouncer = Debouncer(worker, DEBOUNCE_KIND, entprellen_s, self._entprellt)
        worker.register(EV_TARGET_INPUT, self._on_target_input)
        worker.register(EV_SOLL_MODUS, self._on_mode)
        worker.register(EV_SOLL_ENDE, self._on_ende)
        worker.register(EV_SOLL_ENDE_WIEDERHOLEN, self._on_ende_wiederholen)

    @property
    def aus(self) -> bool:
        """Soll-Quelle aus (Modus off oder Frostschutz-Stellung): kein Wunsch, keine Bedienung an die Quelle."""
        return bool(self._aus)

    def _schreiben(self, value: float, veraltet: float | None = None) -> None:
        if self._quelle is not None and self._quelle.schreiben(value):
            self._store.note_own_write(veraltet=veraltet)

    def _setze_aus(self, grund: str, aus: bool) -> None:
        vorher = bool(self._aus)
        war_aus = grund in self._aus
        if aus:
            self._aus.add(grund)
        else:
            self._aus.discard(grund)
        if bool(self._aus) != vorher:
            if self._aus:
                self._wunsch_verwerfen()  # Soll-Quelle aus ist kein Wunsch: auch ein offener nicht
            self._worker.post(Event(EV_SOLL_QUELLE, {"aus": bool(self._aus)}))
            self._soll_quelle_aus(bool(self._aus), self._store.value if self._aus else None)
        if grund == "modus" and war_aus and not self._aus:
            self._wieder_an()

    def _wieder_an(self) -> None:
        """Modus off -> an (ohne Frostschutz): das geltende Soll an die Quelle. Die naechsten Meldungen mit dem alten
        Soll (cache_state) fallen in Echo-Fenster und offenen Schreibbefehl statt ein Wunsch zu werden; die Quelle zeigt
        wieder das Soll (auch nach einem verworfenen Drehwert). Zeigt sie das Soll schon, nichts."""
        if self._quelle is None:
            return
        angezeigt = self._quelle.angezeigt()
        if angezeigt == self._store.value:
            return
        self._schreiben(self._store.value, veraltet=angezeigt)

    def _wunsch_verwerfen(self) -> None:
        if self._wunsch_offen and self._vor_wunsch is not None:
            self._store.wiederherstellen(*self._vor_wunsch)
        self._wunsch_offen = self._lokal_offen = False
        self._vor_wunsch = None
        if self._ende_wartet:
            self._ende_erledigt()  # die Quelle ist aus: sie gehoert dem Kunden, das Soll-Ende ist nicht mehr Sache der Regelung

    def _on_ende(self, event: Event) -> None:
        """Ende der Regelung: die Soll-Quelle zeigt den gemerkten Wunsch (`TargetStore.value`), damit keine Betriebsart
        ihren Wert dort stehen laesst (`ende_schreiben`: nie absenken, nicht bei unbekannter Anzeige). Nichts zu tun
        bei Soll-Quelle aus (Modus off, Frostschutz-Stellung: die Quelle gehoert dem Kunden) und ohne Quelle. Bei einem
        offenen, noch nicht entprellten Wunsch (der Kunde dreht gerade; sein Wert gewinnt) wird es nach dem Entprellen
        nachgeholt. Erledigt meldet es EV_SOLL_ENDE_ERLEDIGT; bis dahin wiederholt es sich im Takt
        SOLL_ENDE_WIEDERHOLUNG_S."""
        if self._quelle is None or self._aus:
            self._ende_erledigt()
        elif self._wunsch_offen:
            self._ende_wartet = True
        else:
            self._ende_wartet = False
            self._ende_versuchen()

    def _on_ende_wiederholen(self, event: Event) -> None:
        self._ende_geplant = False
        self._on_ende(event)

    def _ende_versuchen(self) -> None:
        assert self._quelle is not None
        if ende_schreiben(self._store.value, self._quelle,
                          geschrieben=lambda veraltet: self._store.note_own_write(veraltet=veraltet)):
            self._ende_erledigt()
        elif not self._ende_geplant:
            self._ende_geplant = True
            self._worker.schedule(SOLL_ENDE_WIEDERHOLUNG_S, Event(EV_SOLL_ENDE_WIEDERHOLEN))

    def _ende_erledigt(self) -> None:
        self._ende_wartet = False
        self._worker.post(Event(EV_SOLL_ENDE_ERLEDIGT))

    def _on_mode(self, event: Event) -> None:
        """Ein Modus ohne Text (Zigbee2MQTT `system_mode: null`) ist keine Aussage: er schaltet weder an noch aus (fail
        closed; sonst loeste er nach „aus“ ein „wieder an“ aus)."""
        mode = event.data.get("mode")
        if not isinstance(mode, str):
            return
        self._setze_aus("modus", mode == MODUS_AUS)

    def _on_target_input(self, event: Event) -> None:
        value, source = event.data["value"], event.data["source"]
        herkunft, durch = event.data.get("herkunft"), event.data.get("durch")
        if source == SOURCE_PORTAL:
            vorher = self._store.value
            if not self._store.apply_portal(value):
                return
            self._wunsch_offen = self._lokal_offen = False  # die Bedienung des Servers gilt
            self._vor_wunsch = None
            if not self._aus and self._store.value != vorher:
                self._schreiben(self._store.value)
        elif source == SOURCE_LOKAL:
            if self._aus:
                logger.info("Lokaler Wunsch verworfen: die Soll-Quelle ist aus (Spec 5c F/Q3)")
                return
            if not self._store.apply_lokal(value):
                return
            self._wunsch_offen = self._lokal_offen = True
            self._vor_wunsch = None
            self._herkunft = (herkunft, durch)
        else:
            if ist_endliche_zahl(value) and value <= FROSTSCHUTZ_BIS:
                self._unbrauchbar_gemeldet.clear()
                self._setze_aus("frost", True)
                return
            if not is_plausible(value, ROOM_TEMP_RANGE):
                # Unbrauchbar (nicht endlich, Text, bool, None, ueber dem Raumbereich): aendert die Frostschutz-Stellung
                # nicht, ist kein Wunsch; ein einziger solcher Wert hebt die Stellung nicht auf.
                self._unbrauchbar_melden(value)
                return
            self._unbrauchbar_gemeldet.clear()
            self._setze_aus("frost", False)
            vor_wunsch = (self._store.value, self._store.source)
            if self._aus or not self._store.apply_thermostat(value):
                return
            if not self._wunsch_offen:
                self._vor_wunsch = vor_wunsch
            self._wunsch_offen = True
            self._lokal_offen = False
            self._herkunft = (herkunft, durch)
        self._debouncer.poke()

    def _unbrauchbar_melden(self, value: object) -> None:
        """Je Fehlerart eine WARNING bis zum naechsten brauchbaren Wert, danach DEBUG: das Gateway reicht jede
        Zigbee-Meldung mit occupied_heating_setpoint durch, ein dauerhaft unbrauchbarer Wert flutete sonst das Log.
        Ohne den Wert (Text aus HA oder Zigbee)."""
        if not isinstance(value, int | float) or isinstance(value, bool):
            art = f"kein Zahlenwert ({type(value).__name__})"
        elif not ist_endliche_zahl(value):
            art = "nicht endlich"
        else:
            art = "ausserhalb des Raumbereichs"
        if art in self._unbrauchbar_gemeldet:
            logger.debug("Wunschtemperatur vom Thermostat unbrauchbar (%s), verworfen", art)
            return
        self._unbrauchbar_gemeldet.add(art)
        logger.warning("Wunschtemperatur vom Thermostat unbrauchbar (%s), verworfen; weitere gleiche nur im DEBUG-Log "
                       "bis zu einem brauchbaren Wert", art)

    def _entprellt(self) -> None:
        if self._wunsch_offen:
            lokal = self._lokal_offen
            herkunft, durch = self._herkunft
            self._wunsch_offen = self._lokal_offen = False
            self._vor_wunsch = None
            try:
                begrenzt = self._wunsch(self._store.value, herkunft, durch)
            except ValueError:
                begrenzt = self._store.value
            if begrenzt != self._store.value:
                self._store.begrenzen(begrenzt)
                if not self._aus:
                    self._schreiben(begrenzt)
            elif lokal and not self._aus:
                self._schreiben(self._store.value)
        if self._ende_wartet and not self._wunsch_offen:
            self._worker.post(Event(EV_SOLL_ENDE))  # das Soll-Ende kam bei offenem Wunsch: jetzt mit dem Wert des Kunden
        self._worker.post_coalesced(EV_LOCAL_CHECK, room_target_fired=True)
