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
Prozess teilen sich eine `AnlagenBudget` (ein Lock je Aufruf); wer ein Tageslimit pruefen und danach zaehlen will, haelt
`transaktion()` ueber die ganze Folge.

Auch ohne Transaktion verliert `update` keine fremde Zaehlung: `write_budget.put` liest die zusammengesetzte Sicht, aendert
einen Eintrag und reicht die ganze Sicht zurueck, darin die Anlagenschluessel in dem Stand, den der Thread gelesen hat.
`update` merkt sich je Thread die zuletzt ausgegebene Sicht und uebernimmt nur, was sich gegenueber ihr geaendert hat
(gesetzt, entfernt, bei Zaehlern der Zuwachs), und fuegt es unter dem Lock der Anlage in den aktuellen Stand ein."""
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from smartheat_core.logtext import lesbar
from smartheat_runtime import backup_store
from smartheat_runtime.delivery import DeliveryState
from smartheat_runtime.state import ANLAGE_SCHLUESSEL, BridgeState, StateStore, StorageError, _parse_budget

logger = logging.getLogger(__name__)

_FELDER = ("write_budget", "lifetime_writes")


def _zaehler(raw) -> tuple[dict, int]:
    """(Schreibbudget, Lebensdauer) aus dem Inhalt der Datei; jeder Formfehler gilt als leer."""
    if not isinstance(raw, dict):
        return {}, 0
    budget = _parse_budget(raw["write_budget"]) if "write_budget" in raw else {}
    lebensdauer = raw.get("lifetime_writes", 0)
    if isinstance(lebensdauer, bool) or not isinstance(lebensdauer, int) or lebensdauer < 0:
        lebensdauer = 0
    return budget or {}, lebensdauer


class AnlagenBudget:
    """Die Zaehler der ganzen Anlage in einer Datei. Wie der `StateStore` aendert `update` zuerst den Stand im Speicher;
    scheitert das Schreiben, bleibt er gueltig, `storage_failed` meldet es und `flush` holt es nach."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()
        self.vorhanden = path.exists()
        try:
            raw = backup_store.load_backup(path)
        except Exception as error:
            logger.warning("%s nicht lesbar, starte mit Standardwerten: %s", path, lesbar(str(error)))
            raw = {}
        self._write_budget, self._lifetime_writes = _zaehler(raw)
        self._gespeichert = self._inhalt()
        self._schmutzig = False

    @contextmanager
    def transaktion(self) -> Iterator[None]:
        """Haelt den Lock ueber eine ganze Folge aus Lesen, Aendern und Schreiben (derselbe Thread darf wiederholt)."""
        with self._lock:
            yield

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
        # Die Sicht der Anlage, die der Thread zuletzt gelesen oder selbst geschrieben hat (Grundlage der Aenderung).
        self._sicht = threading.local()
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

    def transaktion(self):
        """Lock der Anlage ueber eine Folge aus Lesen und Schreiben der Zaehler der Anlage (Tageslimit pruefen, schreiben,
        zaehlen; Eintrag lesen, aendern, zurueckschreiben). Der Zustand des Kreises hat nur einen Schreiber."""
        return self._anlage.transaktion()

    @property
    def state(self) -> BridgeState:
        kreis = self._kreis.state
        with self._anlage.transaktion():
            anlage_budget, lebensdauer = self._anlage.write_budget, self._anlage.lifetime_writes
        self._sicht.write_budget, self._sicht.lifetime_writes = anlage_budget, lebensdauer
        return replace(kreis, write_budget={**_ohne_anlage(kreis.write_budget), **anlage_budget},
                       lifetime_writes=lebensdauer)

    def update(self, **changes) -> None:
        """Verteilt `write_budget` nach Schluessel und gibt `lifetime_writes` an die Anlage; beide Orte werden versucht,
        der erste Schreibfehler geht danach durch. Von der Anlage geht nur durch, was sich gegenueber der Sicht des
        Threads geaendert hat (siehe Kopf der Datei)."""
        gesamt = changes.get("write_budget")
        lebensdauer = changes.pop("lifetime_writes", None)
        if gesamt is not None:
            changes["write_budget"] = _ohne_anlage(gesamt)
        fehler: StorageError | None = None
        if gesamt is not None or lebensdauer is not None:
            try:
                self._anlage_aendern(gesamt, lebensdauer)
            except StorageError as error:
                fehler = error
        if changes:
            try:
                self._kreis.update(**changes)
            except StorageError as error:
                fehler = fehler or error
        if fehler is not None:
            raise fehler

    def _anlage_aendern(self, gesamt: dict | None, lebensdauer: int | None) -> None:
        with self._anlage.transaktion():
            aktuell = self._anlage.write_budget
            gelesen = getattr(self._sicht, "write_budget", None)
            gelesen_lebensdauer = getattr(self._sicht, "lifetime_writes", None)
            aenderungen: dict = {}
            if gesamt is not None:
                neu = {key: entry for key, entry in gesamt.items() if key in ANLAGE_SCHLUESSEL}
                merged = _eingefuegt(aktuell, neu, aktuell if gelesen is None else gelesen)
                if merged != aktuell:
                    aenderungen["write_budget"] = merged
                self._sicht.write_budget = neu
            if lebensdauer is not None:
                merged_lebensdauer = _lebensdauer(self._anlage.lifetime_writes, lebensdauer, gelesen_lebensdauer)
                if merged_lebensdauer != self._anlage.lifetime_writes:
                    aenderungen["lifetime_writes"] = merged_lebensdauer
                self._sicht.lifetime_writes = lebensdauer
            if aenderungen:
                self._anlage.update(**aenderungen)

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


def _eingefuegt(aktuell: dict, neu: dict, gelesen: dict) -> dict:
    """Der aktuelle Stand der Anlage mit den Aenderungen des Threads (`neu` gegenueber `gelesen`): ein anderer Eintrag
    wird gesetzt, ein fehlender entfernt, ein unveraenderter bleibt wie er jetzt ist."""
    ergebnis = dict(aktuell)
    for key in gelesen.keys() | neu.keys():
        if neu.get(key) == gelesen.get(key):
            continue
        if key not in neu:
            ergebnis.pop(key, None)
        else:
            ergebnis[key] = _eintrag_eingefuegt(aktuell.get(key), neu[key], gelesen.get(key))
    return ergebnis


def _eintrag_eingefuegt(aktuell: dict | None, neu: dict, gelesen: dict | None) -> dict:
    """Ein Zaehlereintrag des gleichen Tages uebernimmt den Zuwachs des Threads auf den aktuellen Stand (zwei Kreise
    zaehlen am selben Eintrag); sonst gilt der Eintrag des Threads. Die Limitmeldung des aktuellen Eintrags bleibt, wenn
    der Thread sie weder kannte noch gesetzt hat: sonst kaeme die Meldung des anderen Kreises am selben Tag doppelt."""
    tag = neu.get("day")
    if aktuell is None or aktuell.get("day") != tag or not _zaehler_eintrag(neu) or not _zaehler_eintrag(aktuell):
        return neu
    basis = gelesen["count"] if gelesen is not None and gelesen.get("day") == tag and _zaehler_eintrag(gelesen) else 0
    letzte = [x for x in (neu.get("last"), aktuell.get("last")) if isinstance(x, int | float)]
    eintrag = {**neu, "count": max(0, aktuell["count"] + neu["count"] - basis)}
    if letzte:
        eintrag["last"] = max(letzte)
    meldung = aktuell.get("limit_notified")
    if "limit_notified" not in neu and isinstance(meldung, str) and (gelesen is None or "limit_notified" not in gelesen):
        eintrag["limit_notified"] = meldung
    return eintrag


def _zaehler_eintrag(eintrag: dict) -> bool:
    zaehler = eintrag.get("count")
    return isinstance(zaehler, int) and not isinstance(zaehler, bool)


def _lebensdauer(aktuell: int, neu: int, gelesen: int | None) -> int:
    """Lebensdauerzaehler: ein Zuwachs gegenueber dem gelesenen Stand kommt auf den aktuellen Stand; ein Zuruecksetzen
    auf 0 und ein Schreiben ohne gelesenen Stand gelten absolut."""
    if gelesen is None or neu == 0:
        return neu
    return max(0, aktuell + neu - gelesen)


def _ohne_anlage(budget: dict) -> dict:
    return {key: entry for key, entry in budget.items() if key not in ANLAGE_SCHLUESSEL}


def _nur_kreis(namen) -> None:
    if any(name in _FELDER for name in namen):
        raise ValueError("Zaehler der Anlage nur ueber update() aendern")


#: Der Zustand eines Kreises, wie die Laufzeit ihn fuehrt: allein oder mit den Zaehlern der Anlage.
ZustandsSpeicher = StateStore | BudgetStore
