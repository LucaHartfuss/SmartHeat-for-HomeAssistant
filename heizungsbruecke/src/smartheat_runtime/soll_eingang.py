"""Soll-Eingang (Spec 5b 3.2/3.3, Spec 5c 1.3), hostneutral: Bedienung vom Server (portal), Eingabe an der Soll-Quelle
(thermostat; HA: Soll-Entity) und lokale Eingabe (lokal; HA: Ereignis smartheat_wunsch) gehen als EV_TARGET_INPUT in den
Worker. Der Soll-Speicher schreibt, der Debouncer beendet die Eingabe (Gateway 10 s; HA 0 s, dort entprellt der
HA-Trigger mit for: 10 s). Erst dann geht ein Wunsch einmal an den Geraetekern (begrenzt auf den Bedienbereich); der
begrenzte Wert gilt sofort und wird an die Soll-Quelle zurueckgeschrieben, danach EV_LOCAL_CHECK(room_target_fired).
Eine lokale Eingabe gleicht die Soll-Quelle zusaetzlich an. Soll-Quelle aus (Modus off oder ein Soll bis
FROSTSCHUTZ_BIS): kein Wunsch, Meldung ueber soll_quelle_aus, eine neue Bedienung wird nicht geschrieben; wieder an:
das geltende Soll geht an die Quelle, sofern sie etwas anderes zeigt (Echo-Fenster, offener Schreibbefehl). Die
Quelle selbst (Zigbee, HA-Entity) liefert der Host ueber SollQuelle."""
from collections.abc import Callable
from typing import Protocol

from smartheat_runtime.debounce import Debouncer
from smartheat_runtime.runtime import EV_LOCAL_CHECK
from smartheat_runtime.soll_speicher import SOURCE_LOKAL, SOURCE_PORTAL, TargetStore
from smartheat_runtime.worker import Event, RegulationWorker

EV_TARGET_INPUT = "shg_target_input"
EV_SOLL_MODUS = "shg_soll_modus"
DEBOUNCE_KIND = "shg_target_debounce"
#: Spec 5b 3.2: ein Soll bis zu diesem Wert ist die Frostschutz-Stellung der Soll-Quelle, kein Bedienwunsch.
FROSTSCHUTZ_BIS = 7.0
MODUS_AUS = "off"


class SollQuelle(Protocol):
    def schreiben(self, value: float) -> bool:
        """Soll an die Quelle schreiben; False, wenn sie nicht schreibbar ist (dann kein Echo-Fenster)."""
        ...

    def angezeigt(self) -> float | None:
        """Was die Quelle gerade zeigt; None = unbekannt."""
        ...


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
        self._debouncer = Debouncer(worker, DEBOUNCE_KIND, entprellen_s, self._entprellt)
        worker.register(EV_TARGET_INPUT, self._on_target_input)
        worker.register(EV_SOLL_MODUS, self._on_mode)

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
                self._wunsch_verwerfen()  # Soll-Quelle aus ist kein Wunsch (Spec 5b 3.2): auch ein offener nicht
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

    def _on_mode(self, event: Event) -> None:
        self._setze_aus("modus", event.data.get("mode") == MODUS_AUS)

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
            if not self._store.apply_lokal(value):
                return
            self._wunsch_offen = self._lokal_offen = True
            self._vor_wunsch = None
            self._herkunft = (herkunft, durch)
        else:
            if isinstance(value, int | float) and not isinstance(value, bool) and value <= FROSTSCHUTZ_BIS:
                self._setze_aus("frost", True)
                return
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
        self._worker.post_coalesced(EV_LOCAL_CHECK, room_target_fired=True)
