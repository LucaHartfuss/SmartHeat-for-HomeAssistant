"""Schreibbudgets bei mehreren Kreisen. Jeder Kreis hat seinen Zustand (`StateStore`, `runtime/kreis-<n>/backup.json`);
manche Zaehler gehoeren aber der ganzen Anlage, weil sie ein Kontingent des Geraets oder der Cloud abbilden, das alle
Kreise teilen: der Tagesgesamtzaehler der physischen Schreibvorgaenge (`writes:total`), die Zonenvorbereitung, die
Boost-Starts (Comfort und Notfall) und der Lebensdauerzaehler. Sie stehen in `runtime/anlage_budget.json`, einer
`AnlagenBudget` fuer alle Kreise. Alles andere (Durchsetzung je Hebelgruppe, Boost-Ende, Wiederherstellung) bleibt im
Kreis.

`BudgetStore` legt beide hinter die Schnittstelle des `StateStore`: `state.write_budget` und `state.lifetime_writes` sind
die zusammengesetzte Sicht, `update(write_budget=...)` und `update(lifetime_writes=...)` verteilen nach Schluessel.
`write_budget`, `pipeline` und `enforce` merken davon nichts. Das Kontingent der Cloud selbst fuehrt der Treiber in seiner
Datei (`quota/vicare_cloud.json`), die alle Kreise desselben Treibers schon teilen.

Die Anlagendatei kennt keine Uhr des vorigen Laufs (`last` faellt beim Laden weg, wie im Kreis). Zwei Kreise im selben
Prozess teilen sich eine `AnlagenBudget` (ein Lock je Aufruf); wer eine Eintragsfolge lesen-aendern-schreiben will, ruft
aus dem Worker-Thread seines Kreises."""
import logging
import threading
from dataclasses import replace
from pathlib import Path

from smartheat_core import write_budget
from smartheat_core.logtext import lesbar
from smartheat_runtime import backup_store
from smartheat_runtime.delivery import DeliveryState
from smartheat_runtime.state import BridgeState, StateStore, StorageError, parse_budget

logger = logging.getLogger(__name__)

#: Die Schluessel der ganzen Anlage; jeder andere Schluessel gehoert dem Kreis.
ANLAGE_SCHLUESSEL = frozenset({
    write_budget.TOTAL, write_budget.ZONE_PREPARE, write_budget.BOOST_START, write_budget.EMERGENCY_START,
})
_FELDER = ("write_budget", "lifetime_writes")


def _zaehler(raw) -> tuple[dict, int]:
    """(Schreibbudget, Lebensdauer) aus dem Inhalt der Datei; jeder Formfehler gilt als leer."""
    if not isinstance(raw, dict):
        return {}, 0
    budget = parse_budget(raw["write_budget"]) if "write_budget" in raw else {}
    lebensdauer = raw.get("lifetime_writes", 0)
    if isinstance(lebensdauer, bool) or not isinstance(lebensdauer, int) or lebensdauer < 0:
        lebensdauer = 0
    return budget or {}, lebensdauer


class AnlagenBudget:
    """Die Zaehler der ganzen Anlage in einer Datei. Wie der `StateStore` aendert `update` zuerst den Stand im Speicher;
    scheitert das Schreiben, bleibt er gueltig, `storage_failed` meldet es und `flush` holt es nach."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self.vorhanden = path.exists()
        try:
            raw = backup_store.load_backup(path)
        except Exception as error:
            logger.warning("%s nicht lesbar, starte mit Standardwerten: %s", path, lesbar(str(error)))
            raw = {}
        self._write_budget, self._lifetime_writes = _zaehler(raw)
        self._gespeichert = self._inhalt()
        self._schmutzig = False

    @property
    def write_budget(self) -> dict:
        with self._lock:
            return dict(self._write_budget)

    @property
    def lifetime_writes(self) -> int:
        with self._lock:
            return self._lifetime_writes

    @property
    def storage_failed(self) -> bool:
        return self._schmutzig

    def _inhalt(self) -> dict:
        inhalt: dict = {}
        if self._write_budget:
            inhalt["write_budget"] = self._write_budget
        if self._lifetime_writes:
            inhalt["lifetime_writes"] = self._lifetime_writes
        return inhalt

    def update(self, *, write_budget: dict | None = None, lifetime_writes: int | None = None) -> None:
        """Wirft StorageError nach der Aenderung im Speicher, wenn das Schreiben scheitert."""
        with self._lock:
            if write_budget is not None:
                self._write_budget = dict(write_budget)
            if lifetime_writes is not None:
                self._lifetime_writes = lifetime_writes
            self._schreiben()

    def flush(self) -> None:
        with self._lock:
            if self._schmutzig:
                self._schreiben()

    def _schreiben(self) -> None:
        inhalt = self._inhalt()
        if inhalt == self._gespeichert and not self._schmutzig:
            return
        try:
            backup_store.save_backup(self._path, inhalt)
        except Exception as error:
            self._schmutzig = True
            raise StorageError(f"{self._path.name} nicht schreibbar: {error}") from error
        self._gespeichert, self._schmutzig = inhalt, False
        self.vorhanden = True


class BudgetStore:
    """Der Zustand eines Kreises samt den Zaehlern der Anlage, mit der Schnittstelle des `StateStore`."""

    def __init__(self, kreis: StateStore, anlage: AnlagenBudget) -> None:
        self._kreis = kreis
        self._anlage = anlage
        self._uebernehmen()

    def _uebernehmen(self) -> None:
        """Zaehler der Anlage in der Sicherung des Kreises (Bestand aus dem Stand vor den Kreisordnern) wandern einmal in
        die Anlagendatei, wenn es sie noch nicht gibt, und verschwinden aus dem Kreis. Eine vorhandene Anlagendatei
        gilt immer; ein Fehler beim Schreiben laesst den Kreis unberuehrt, der Versuch wiederholt sich beim naechsten Start."""
        state = self._kreis.state
        alt = {key: entry for key, entry in state.write_budget.items() if key in ANLAGE_SCHLUESSEL}
        if not alt and not state.lifetime_writes:
            return
        if not self._anlage.vorhanden:
            try:
                self._anlage.update(write_budget=alt, lifetime_writes=state.lifetime_writes)
            except StorageError:
                logger.warning("Zaehler der Anlage nicht uebernommen, der Versuch wiederholt sich beim naechsten Start")
                return
        try:
            self._kreis.update(write_budget=_ohne_anlage(state.write_budget), lifetime_writes=0)
        except StorageError:
            logger.warning("Zaehler der Anlage im Kreis nicht entfernt, sie gelten dort nicht (Anlagendatei geht vor)")

    @property
    def state(self) -> BridgeState:
        kreis = self._kreis.state
        return replace(kreis, write_budget={**_ohne_anlage(kreis.write_budget), **self._anlage.write_budget},
                       lifetime_writes=self._anlage.lifetime_writes)

    def update(self, **changes) -> None:
        """Verteilt `write_budget` nach Schluessel und gibt `lifetime_writes` an die Anlage; beide Orte werden versucht,
        der erste Schreibfehler geht danach durch."""
        anlage: dict = {}
        if "write_budget" in changes:
            gesamt = changes["write_budget"]
            anlage["write_budget"] = {key: entry for key, entry in gesamt.items() if key in ANLAGE_SCHLUESSEL}
            changes["write_budget"] = _ohne_anlage(gesamt)
        if "lifetime_writes" in changes:
            anlage["lifetime_writes"] = changes.pop("lifetime_writes")
        fehler: StorageError | None = None
        for ziel, aenderungen in ((self._anlage, anlage), (self._kreis, changes)):
            if not aenderungen:
                continue
            try:
                ziel.update(**aenderungen)
            except StorageError as error:
                fehler = fehler or error
        if fehler is not None:
            raise fehler

    def update_saved(self, **changes) -> None:
        _nur_kreis(changes)
        self._kreis.update_saved(**changes)

    def is_saved(self, *keys: str) -> bool:
        _nur_kreis(keys)
        return self._kreis.is_saved(*keys)

    def revert_to_saved(self, *keys: str) -> None:
        _nur_kreis(keys)
        self._kreis.revert_to_saved(*keys)

    def saved_restore_point(self, required: tuple[str, ...]) -> dict | None:
        return self._kreis.saved_restore_point(required)

    def set_delivery(self, delivery_state: DeliveryState) -> None:
        self._kreis.set_delivery(delivery_state)

    @property
    def storage_failed(self) -> bool:
        return self._kreis.storage_failed or self._anlage.storage_failed

    def flush(self) -> None:
        """Holt beide Orte nach; der erste Fehler geht danach durch."""
        fehler: StorageError | None = None
        for ziel in (self._anlage, self._kreis):
            try:
                ziel.flush()
            except StorageError as error:
                fehler = fehler or error
        if fehler is not None:
            raise fehler


def _ohne_anlage(budget: dict) -> dict:
    return {key: entry for key, entry in budget.items() if key not in ANLAGE_SCHLUESSEL}


def _nur_kreis(namen) -> None:
    if any(name in _FELDER for name in namen):
        raise ValueError("Zaehler der Anlage nur ueber update() aendern")


#: Der Zustand eines Kreises, wie die Laufzeit ihn fuehrt: allein oder mit den Zaehlern der Anlage.
ZustandsSpeicher = StateStore | BudgetStore
