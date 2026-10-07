"""Hebel-Pipeline (Hersteller-Abstraktion, Spec 5.2, Plan 2 P2-1/P2-2/P2-5; Nachfolger von heizungsbruecke/override.py):
welche Hebelwerte auf der Anlage stehen sollen, und das Schreiben dorthin ueber das Binding. Rein: Anlage (Binding),
Speicher (store) und Uhr kommen von aussen.

Sollwert-Zeile (Vorrang von oben nach unten):
    Notfall-Boost aktiv -> lokales Maximum der Hebel aus LocalSafety.emergency_boost_levers (*)
    Comfort-Boost aktiv -> LocalSafety.comfort_boost (*)
    sonst               -> Wiederherstellungspunkt restore_point (nur vorhandene Hebel)
    (*) ein Hebel aus BindingDescription.optional_restore nur, wenn der Wiederherstellungspunkt einen Wert fuer ihn
        hat; sonst bleibt er waehrend des Boosts unangetastet (Vaillant: Heizgrenze, TP12h -- ohne Punkt koennte das
        Boost-Ende sie nicht zuruecknehmen).

Ursprungswerte (P2-5): die Hebel aus BindingDescription.restore_originals merkt capture_originals vor dem ersten eigenen
Schreiben; restore_and_clear (Abo-Ende, Abmelden) stellt sie wieder her. Plan 3b: zusaetzlich die Hilfswerte aus
BindingDescription.aux_originals (Betriebsart, Komfort-/Absenk-Soll), gemerkt vor der ersten Vorbereitung bzw. dem ersten
Schreiben des prepared_lever und nach den Hebeln zurueckgestellt.

Geschrieben wird nur bei tatsaechlicher Aenderung (Quota-Check, jeder Schreibvorgang kann ein Cloud-Aufruf sein), jeder
Wert auf die lokalen Grenzen begrenzt und auf die Schrittweite der Anlage gerundet. Client-abgeleitete Hebel
(Mindestvorlauf) gehoeren zu keiner Zeile, werden aber auch hier geschrieben (write_lever).

Plan 3b: Jeder physische Schreibvorgang (binding.physical_writes; eine Schreibgruppe zaehlt einmal) zaehlt in das
Tagesbudget TOTAL (nur mit BindingDescription.daily_write_limit) und in den Lebensdauerzaehler (nur mit
lifetime_hint_at). Das Tagesbudget begrenzt Serverwerte, Durchsetzen, Vorbereitung und Mindestvorlauf; Boost-Start/-Ende
und Wiederherstellung sind ausgenommen, zaehlen aber mit (Sicherheit und Rueckweg vor Schonung). Ein wegen des Budgets
nicht geschriebener Serverwert bleibt im Wiederherstellungspunkt und in deferred_levers und wird mit write_deferred
nachgeholt."""
import logging
import math
import time
from collections.abc import Callable, Mapping

from smartheat_core import write_budget
from smartheat_core.binding import PlantBinding
from smartheat_core.clamping import clamp, target_value
from smartheat_core.safety import LocalSafety

logger = logging.getLogger(__name__)

_ROW_EMERGENCY = "emergency"
_ROW_COMFORT = "comfort"
_ROW_RESTORE = "restore"
_ROW_LOG = {
    _ROW_EMERGENCY: "Notfall-Boost aktiv: Sollwerte auf Maximalwerte gesetzt",
    _ROW_COMFORT: "Boost aktiv: Sollwerte auf Boost-Werte gesetzt",
    _ROW_RESTORE: "Boost beendet: Wiederherstellungspunkt geschrieben (sofern vorhanden)",
}

# Ergebnis von _ensure_restore_point: der aktuelle Punkt ist gesichert, oder (nur Notfall-Boost) der zuletzt
# gespeicherte aeltere Punkt gilt (N6).
_POINT_CURRENT = "current"
_POINT_SAVED = "saved"


# Plan 3b: Meldeschluessel der nicht kritischen Hinweise (abschaltbar, Plan 3c).
BUDGET_KEY = "schreibbudget"
LIFETIME_KEY = "schreibzaehler"
LIFETIME_STEP = 10000
BUDGET_MESSAGE = (
    "SmartHeat: Das Tageslimit von {limit} Schreibvorgängen an der Heizung ist erreicht. Neue Werte werden "
    "gespeichert und morgen geschrieben (Schonung des Gerätespeichers)."
)
LIFETIME_MESSAGE = (
    "SmartHeat: Bisher wurden {count} Werte in die Heizung geschrieben. Der Hersteller nennt eine Grenze von "
    "100.000 Schreibvorgängen auf Lebensdauer des Reglers; bitte den Fachbetrieb informieren."
)


class WriteBudgetExhausted(Exception):
    """Das Tagesbudget der physischen Schreibvorgaenge (BindingDescription.daily_write_limit) ist erreicht."""

    def __init__(self, lever: str, limit: int) -> None:
        self.lever = lever
        super().__init__(f"{lever}: Tageslimit von {limit} Schreibvorgaengen erreicht")


class DeviceWriteError(Exception):
    """Ein Wert konnte nicht auf die Anlage geschrieben werden (z. B. Hersteller-Cloud gestoert)."""

    def __init__(self, lever: str, entity_id: str, cause: BaseException) -> None:
        self.lever = lever
        self.entity_id = entity_id
        super().__init__(f"{lever} ({entity_id}): {str(cause)[:200]}")


def _row(comfort: bool, emergency: bool) -> str:
    if emergency:
        return _ROW_EMERGENCY
    if comfort:
        return _ROW_COMFORT
    return _ROW_RESTORE


def _is_finite_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class LeverPipeline:
    def __init__(
        self, store, binding: PlantBinding, safety: LocalSafety, *, clock: Callable[[], float] = time.monotonic,
        notify: Callable[[str, str, str], None] | None = None,
    ) -> None:
        self._store = store
        self._binding = binding
        self._description = binding.description
        self._safety = safety
        self._clock = clock
        # Nicht kritischer Hinweis (Schluessel, Zustand, Text); im Add-on Notifier.notify(..., critical=False).
        self._notify = notify
        # Zeitpunkt (clock) und Wert des letzten erfolgreichen eigenen Schreibens je Hebel, nur Laufzeit: Durchsetzen
        # und Quota-Check vertrauen dem Read erst nach settle_seconds.
        self._last_write_at: dict[str, float] = {}
        self._last_written: dict[str, float] = {}
        # Ein Schreibvorgang kurz VOR einem Neustart ist unbekannt, die Anlage kann ihn noch settle_seconds lang
        # verbergen (settled).
        self._started_at = clock()
        # Zeitpunkt (clock) der letzten eigenen, erfolgreichen Vorbereitung (Zonen-Umstellung), nur Laufzeit.
        self._prepared_at: float | None = None

    @property
    def safety(self) -> LocalSafety:
        return self._safety

    @property
    def binding(self) -> PlantBinding:
        return self._binding

    def settled(self, lever: str) -> bool:
        """True, wenn ein Read dieses Hebels nicht mehr hinter einem eigenen Schreibvorgang herhinken kann: das letzte
        eigene Schreiben -- ohne eines seit dem Start der Start selbst -- liegt laenger als settle_seconds zurueck.
        Eine Regel fuer Quota-Check (_matches_the_device) und Durchsetzen."""
        last = self._last_write_at.get(lever, self._started_at)
        return self._clock() - last > self._description.settle_seconds

    def last_written(self, lever: str) -> float | None:
        """Letzter eigener Schreibwert dieses Hebels seit dem Start (nur Laufzeit), sonst None."""
        return self._last_written.get(lever)

    def set_boosts(self, comfort: bool, emergency: bool) -> tuple[bool, bool]:
        """Setzt die Boost-Flags und schreibt die Werte der neuen Sollwert-Zeile, falls sie sich aendert. Ein neu
        startender Boost braucht vorher einen gespeicherten Wiederherstellungspunkt, sonst wird er abgelehnt; ein
        laufender Boost wird nie abgelehnt. Ein neuer Notfall-Boost darf zusaetzlich auf dem zuletzt gespeicherten,
        aelteren Punkt starten, wenn der aktuelle nicht speicherbar ist (N6). Wirft das Schreiben, bleiben die Flags
        unveraendert. Nach einem Fehlschlag bremst das Schreibbudget: dann bleiben die Flags ohne Schreiben stehen,
        zurueckgegeben werden die bisherigen."""
        self.capture_originals()
        state = self._store.state
        starting_comfort = comfort and not state.boost_active
        starting_emergency = emergency and not state.emergency_boost_active
        if starting_comfort or starting_emergency:
            point = self._ensure_restore_point(allow_saved_fallback=starting_emergency)
            if point != _POINT_CURRENT and starting_comfort:
                logger.warning("Kein Wiederherstellungspunkt, Boost ausgesetzt - naechster Check versucht es erneut")
                comfort = False
            if point is None and starting_emergency:
                logger.warning(
                    "Kein Wiederherstellungspunkt, Notfall-Boost ausgesetzt - naechster Check versucht es erneut"
                )
                emergency = False
        new_row = _row(comfort, emergency)
        if new_row != _row(state.boost_active, state.emergency_boost_active):
            key, rule = (
                (write_budget.BOOST_END, write_budget.RETURN_STAIRCASE) if new_row == _ROW_RESTORE
                else (write_budget.BOOST_START, write_budget.RETRY_QUOTA)
            )
            if not write_budget.may_attempt(self._store, key, rule, self._clock()):
                logger.info("Sollwert-Zeile '%s' zurueckgestellt: Schreibbudget nach einem Fehlschlag", new_row)
                return state.boost_active, state.emergency_boost_active
            self._budgeted_write(key, self._row_values(new_row))
            logger.warning(_ROW_LOG[new_row])
        self._store.update(boost_active=comfort, emergency_boost_active=emergency)
        return comfort, emergency

    def _budgeted_write(
        self, key: str, values: dict, *, aux: Mapping[str, str | float] | None = None, require_aux: bool = True,
    ) -> None:
        """Boost-Start/-Ende und Wiederherstellung: vom Tagesbudget ausgenommen (exempt), zaehlen aber mit. `aux`:
        Hilfs-Ursprungswerte, die nach den Hebeln zurueckgestellt werden (nur Wiederherstellung)."""
        try:
            self._write(values, exempt=True, require_aux=require_aux)
            if aux:
                # Zielwerte der Hebel, nicht der Read: der zeigt einen eigenen Schreibvorgang erst nach settle_seconds.
                # Uebersprungen (Quota-Check) heisst, die Anlage steht schon darauf; gescheitert, kommt man nicht hierher.
                targets = {
                    lever: self._target(lever, value) for lever, value in values.items() if self._binding.has(lever)
                }
                self._restore_aux(aux, targets)
        except Exception:
            write_budget.record_attempt(self._store, key, self._clock())
            raise
        write_budget.record_success(self._store, key)

    def apply_server_values(self, levers: Mapping[str, float]) -> None:
        """Speichert die (begrenzten) Serverwerte als Wiederherstellungspunkt, bevor geschrieben wird, und schreibt sie
        nur, wenn kein Boost laeuft; sonst uebernimmt sie das Boost-Ende. Vorher werden offene Ursprungswerte gemerkt."""
        self.capture_originals()
        clamped = {}
        for lever in self._description.lever_set.levers:
            if lever not in levers:
                continue
            value = levers[lever]
            minimum, maximum = self._range(lever)
            clamped[lever] = clamp(value, minimum, maximum)
            if clamped[lever] != value:
                logger.warning(
                    "Wert fuer Hebel '%s' geclampt: empfangen=%s, geschrieben=%s (Bereich [%s, %s])",
                    lever, value, clamped[lever], minimum, maximum,
                )
        self._store.update(restore_point={**self._store.state.restore_point, **clamped})
        logger.info("Serverwerte gespeichert: %s", clamped)
        state = self._store.state
        if state.boost_active or state.emergency_boost_active:
            logger.info("Serverwerte waehrend aktivem (Notfall-)Boost nur gespeichert, die Anlage bleibt auf den Boost-Werten.")
            return
        try:
            self._write(clamped)
        except WriteBudgetExhausted:
            self._defer(clamped)

    def _defer(self, values: Mapping[str, float]) -> None:
        """Tageslimit erreicht: alle Hebel dieser Serverantwort bleiben im Wiederherstellungspunkt und werden mit
        write_deferred nachgeholt (ein schon geschriebener ueberspringt dann der Quota-Check); Durchsetzen wertet diese
        Hebel nicht als Eingriff."""
        pending = [lever for lever in self._description.lever_set.levers if lever in values and self._binding.has(lever)]
        deferred = tuple(dict.fromkeys((*self._store.state.deferred_levers, *pending)))
        logger.warning("Tageslimit erreicht: %s erst nach dem Tageswechsel", ", ".join(pending))
        try:
            self._store.update(deferred_levers=deferred)
        except Exception as error:
            logger.warning("Zurueckgestellte Hebel nicht gespeichert (%s), gelten bis zum Neustart", error)

    def write_deferred(self) -> None:
        """Holt wegen des Tagesbudgets zurueckgestellte Serverwerte nach (Takt EV_HEALTH, vor dem Durchsetzen). Nur in
        der Zeile des Wiederherstellungspunkts; waehrend eines Boosts uebernimmt das Boost-Ende. Wirft nie."""
        deferred = self._store.state.deferred_levers
        if not deferred or self._current_row() != _ROW_RESTORE or self.daily_budget_reached():
            return
        values = self._row_values(_ROW_RESTORE)
        try:
            self._write({lever: values[lever] for lever in deferred if lever in values})
        except WriteBudgetExhausted:
            return
        except Exception:
            logger.exception("Zurueckgestellte Serverwerte nicht geschrieben, naechster Versuch im naechsten Takt")
            return
        self._clear_deferred(deferred)

    def restore_and_clear(self, always_restore: bool) -> bool:
        """Abo-Fristende/Abmelden: beide Boosts beenden und den Wiederherstellungspunkt schreiben (bei always_restore
        auch ohne laufenden Boost). Die Hebel aus restore_originals gehen auf ihren Ursprungswert, auch ohne laufenden
        Boost, wenn der Wiederherstellungspunkt davon abweicht; ist der Ursprungswert unbekannt, bleibt der Hebel auf
        dem Wiederherstellungspunkt (Log). False, wenn das Schreiben scheitert: die Flags bleiben, der Aufrufer
        versucht es erneut. Scheitert danach nur das Speichern, gilt die Wiederherstellung als erfolgt. Plan 3b: danach
        die Hilfs-Ursprungswerte (aux_originals), die anschliessend geleert werden."""
        state = self._store.state
        originals: dict[str, float] = {}
        differs = False
        for lever in self._description.restore_originals:
            if not self._binding.has(lever):
                continue
            original = state.originals.get(lever)
            current = state.restore_point.get(lever)
            if original is None:
                if current is not None:
                    logger.warning("Ursprungswert von %s unbekannt: bleibt auf dem gelernten Wert %s", lever, current)
                continue
            originals[lever] = original
            if current is not None and abs(current - original) > self._description.steps[lever] / 2:
                differs = True
        aux = dict(state.aux_originals)
        if not (always_restore or state.boost_active or state.emergency_boost_active or differs or aux):
            return True
        if not write_budget.may_attempt(self._store, write_budget.RESTORE, write_budget.RETURN_STAIRCASE, self._clock()):
            logger.info("Wiederherstellung zurueckgestellt: Schreibbudget nach einem Fehlschlag")
            return False
        values = {**self._row_values(_ROW_RESTORE), **originals}
        try:
            # Rueckweg vor Vollstaendigkeit: ohne gemerkte Hilfswerte wird trotzdem zurueckgestellt (require_aux=False).
            self._budgeted_write(write_budget.RESTORE, values, aux=aux, require_aux=False)
        except Exception:
            logger.exception(
                "Zuletzt gelernte Werte konnten nicht wiederhergestellt werden - Boost-Flags "
                "bleiben gesetzt, es wird erneut versucht"
            )
            return False
        try:
            self._store.update(
                boost_active=False, emergency_boost_active=False,
                restore_point={**self._store.state.restore_point, **originals},
                # Hilfswerte sind zurueckgestellt: ein weiterer Aufruf (naechster Start, Fristende) schreibt nur noch
                # abweichende Hebel (Quota-Check) und stellt ein Binding mit Hilfswerten nicht mehr um (_write); ein
                # neues Abo merkt sie neu.
                aux_originals={},
            )
        except Exception:
            logger.exception("Zuletzt gelernte Werte wiederhergestellt, Boost-Flags konnten aber nicht gespeichert werden")
        return True

    def _row_values(self, row: str) -> dict:
        point = self._store.state.restore_point

        def included(lever: str) -> bool:
            return lever not in self._description.optional_restore or point.get(lever) is not None

        if row == _ROW_EMERGENCY:
            return {
                lever: self._range(lever)[1] for lever in self._safety.emergency_boost_levers if included(lever)
            }
        if row == _ROW_COMFORT:
            return {lever: value for lever, value in self._safety.comfort_boost.items() if included(lever)}
        # Reihenfolge des Hebelsatzes, nicht die Einfuegereihenfolge des Punkts: Durchsetzen geht expected_values
        # der Reihe nach durch (wie bis 0.29.0: Steigung, Wunschtemperatur, Heizgrenze).
        return {
            lever: point[lever] for lever in self._description.lever_set.levers if point.get(lever) is not None
        }

    def _matches_the_device(self, lever: str, target: float) -> bool:
        """True, wenn ein Schreiben ueberfluessig waere (Quota-Check). Der Read muss innerhalb eines halben Schritts am
        Ziel liegen; haben WIR den Hebel innerhalb von settle_seconds selbst geschrieben, muss zusaetzlich unser
        letzter Schreibwert schon dem Ziel entsprechen (A -> B -> A, waehrend die Anlage noch B zeigt). Ein Lesefehler
        oder ein nicht auswertbarer Read heisst "schreiben". Meldet die Anlage keinen Sollwert (None, ruhende Zone),
        zaehlt nur der eigene letzte Schreibwert seit dem Start (B-TP11-2)."""
        try:
            current = self._binding.read(lever)
        except Exception:
            return False
        step = self._description.steps[lever]
        if current is None:
            last_written = self._last_written.get(lever)
            return last_written is not None and abs(last_written - target) <= step / 2
        if not _is_finite_number(current) or abs(current - target) > step / 2:
            return False
        if self.settled(lever):
            return True
        last_written = self._last_written.get(lever)
        return last_written is not None and abs(last_written - target) <= step / 2

    def _target(self, lever: str, value: float) -> float:
        minimum, maximum = self._range(lever)
        return target_value(value, minimum, maximum, self._description.steps[lever])

    def _range(self, lever: str) -> tuple[float, float]:
        """Lokale Grenzen, verengt auf den Wertebereich der Anlage (binding.limits). Erweitert nie; ohne Schnittmenge
        gelten die lokalen Grenzen (das Schreiben scheitert dann sichtbar an der Anlage, nie ein Wert ausserhalb der
        lokalen Grenzen)."""
        low, high = self._safety.ranges[lever]
        try:
            device = self._binding.limits(lever)
        except Exception:
            logger.exception("Wertebereich der Anlage fuer %s nicht lesbar", lever)
            device = None
        if device is None:
            return low, high
        narrowed = (max(low, device[0]), min(high, device[1]))
        if narrowed[0] > narrowed[1]:
            logger.warning(
                "Wertebereich der Anlage fuer %s %s liegt ausserhalb der lokalen Grenzen [%s, %s]", lever, device, low, high,
            )
            return low, high
        return narrowed

    def _write_lever(self, lever: str, value: float, *, force: bool = False, exempt: bool = False) -> float:
        """Schreibt EINEN Hebel, aber nur bei tatsaechlicher Aenderung, es sei denn `force=True` (direkt nach einer
        Vorbereitung zeigt der Read garantiert noch den alten Sollwert). Ein uebersprungener Schreibvorgang merkt sich
        weder Zeitpunkt noch Wert. Wirft DeviceWriteError, ohne `exempt` auch WriteBudgetExhausted."""
        target = self._target(lever, value)
        if not force and self._matches_the_device(lever, target):
            return target
        if not exempt:
            self._check_daily_budget(lever)
        before = self._binding.physical_writes
        try:
            self._binding.write(lever, target)
        except Exception as error:
            raise DeviceWriteError(lever, self._binding.ref(lever), error) from error
        finally:
            self._count_physical(self._binding.physical_writes - before)
        self._last_write_at[lever] = self._clock()
        self._last_written[lever] = target
        return target

    def _write_group(self, members: list[str], values: Mapping[str, float], *, exempt: bool) -> None:
        """Schreibgruppe (Viessmann setCurve): weicht ein Mitglied ab, werden alle geschrieben (Reihenfolge des
        Hebelsatzes) und zaehlen als EIN Schreibvorgang. Scheitert ein spaeteres Mitglied, schreibt der naechste
        Versuch wieder alle."""
        targets = {lever: self._target(lever, values[lever]) for lever in members}
        if all(self._matches_the_device(lever, target) for lever, target in targets.items()):
            return
        if not exempt:
            self._check_daily_budget(members[0])
        before = self._binding.physical_writes
        try:
            for lever, target in targets.items():
                try:
                    self._binding.write(lever, target)
                except Exception as error:
                    raise DeviceWriteError(lever, self._binding.ref(lever), error) from error
                self._last_write_at[lever] = self._clock()
                self._last_written[lever] = target
        finally:
            self._count_physical(min(self._binding.physical_writes - before, 1))

    def _group(self, lever: str) -> tuple[str, ...]:
        return next((group for group in self._description.write_groups if lever in group), (lever,))

    def _write(self, values: Mapping[str, float], *, exempt: bool = False, require_aux: bool = True) -> None:
        """Vorbereitung -> Hebel in der Reihenfolge des Hebelsatzes (Vaillant: Betriebsart -> Steigung ->
        Parallelverschiebung -> Heizgrenze), nur zugeordnete Hebel. Muss die Anlage fuer prepared_lever erst
        vorbereitet werden, geschieht das VOR dem ersten Hebel; macht die Vorbereitung den Sollwert unbrauchbar
        (preparation_resets_setpoint, Vaillant), wird prepared_lever danach ohne Quota-Check geschrieben (force), weil
        der Read garantiert noch den alten Sollwert zeigt. Schreibgruppen (write_groups) gehen zusammen. Vor dem
        prepared_lever muessen die Hilfs-Ursprungswerte gemerkt sein (require_aux; nur die Wiederherstellung schreibt
        auch ohne und bereitet ein Binding mit Hilfswerten dabei nicht vor)."""
        prepared_lever = self._description.prepared_lever
        switched = False
        if prepared_lever is not None and prepared_lever in values and self._binding.has(prepared_lever):
            if require_aux:
                self._require_aux(prepared_lever)
            # Ohne Pflicht zu Hilfswerten (nur die Wiederherstellung) nicht vorbereiten, wenn das Binding Hilfswerte hat:
            # die Betriebsart geht ueber restore_aux zurueck, ohne gemerkte Werte bliebe eine Umstellung fuer immer.
            # Vaillant (keine Hilfswerte) bereitet wie bisher vor.
            if self._binding.needs_preparation() and (require_aux or not self._description.aux_originals):
                switched = self.ensure_prepared(exempt=exempt, require_aux=require_aux)
        force_prepared = switched and self._description.preparation_resets_setpoint
        written: list[str] = []
        done: set[str] = set()
        for lever in self._description.lever_set.levers:
            if lever in done or lever not in values or not self._binding.has(lever):
                continue
            group = self._group(lever)
            members = [
                name for name in self._description.lever_set.levers
                if name in group and name in values and self._binding.has(name)
            ]
            done.update(members)
            if len(members) > 1:
                self._write_group(members, values, exempt=exempt)
            else:
                self._write_lever(lever, values[lever], force=force_prepared and lever == prepared_lever, exempt=exempt)
            written.extend(members)
        self._clear_deferred(written)

    def _clear_deferred(self, levers) -> None:
        deferred = self._store.state.deferred_levers
        remaining = tuple(lever for lever in deferred if lever not in levers)
        if remaining == deferred:
            return
        try:
            self._store.update(deferred_levers=remaining)
        except Exception as error:
            logger.warning("Zurueckgestellte Hebel nicht gespeichert (%s), gelten bis zum Neustart", error)

    def daily_budget_reached(self) -> bool:
        """True, wenn das Tagesbudget der physischen Schreibvorgaenge erreicht ist (nur mit daily_write_limit)."""
        limit = self._description.daily_write_limit
        if limit is None:
            return False
        entry = write_budget.get(self._store, write_budget.TOTAL)
        return write_budget.count_today(entry, write_budget.today()) >= limit

    def _check_daily_budget(self, lever: str) -> None:
        limit = self._description.daily_write_limit
        if limit is None or not self.daily_budget_reached():
            return
        if self._notify is not None:
            try:
                self._notify(BUDGET_KEY, write_budget.today(), BUDGET_MESSAGE.format(limit=limit))
            except Exception:
                logger.exception("Hinweis zum Tageslimit fehlgeschlagen")
        raise WriteBudgetExhausted(lever, limit)

    def _count_physical(self, count: int) -> None:
        """Zaehlt erfolgreiche physische Schreibvorgaenge (Tagesbudget, Lebensdauer). Best effort: ein Speicherfehler
        haelt den Stand im Speicher (StateStore.update aendert vor dem Schreiben)."""
        if count <= 0:
            return
        description = self._description
        if description.daily_write_limit is not None:
            entry = write_budget.get(self._store, write_budget.TOTAL)
            write_budget.put(
                self._store, write_budget.TOTAL, write_budget.added(entry, count, self._clock(), write_budget.today()),
            )
        if description.lifetime_hint_at is None:
            return
        total = self._store.state.lifetime_writes + count
        try:
            self._store.update(lifetime_writes=total)
        except Exception as error:
            logger.warning("Lebensdauerzaehler nicht gespeichert (%s), gilt bis zum Neustart", error)
        if total >= description.lifetime_hint_at and self._notify is not None:
            reached = description.lifetime_hint_at + (total - description.lifetime_hint_at) // LIFETIME_STEP * LIFETIME_STEP
            try:
                self._notify(LIFETIME_KEY, str(reached), LIFETIME_MESSAGE.format(count=f"{reached:,}".replace(",", ".")))
            except Exception:
                logger.exception("Hinweis zum Lebensdauerzaehler fehlgeschlagen")

    def expected_values(self) -> dict:
        """Sollwerte der aktuellen Zeile, so wie sie auf der Anlage stehen muessten (begrenzt und gerundet); nur
        vorhandene, zugeordnete Hebel."""
        return {
            lever: target_value(value, *self._range(lever), self._description.steps[lever])
            for lever, value in self._row_values(self._current_row()).items()
            if self._binding.has(lever)
        }

    def write_levers(self, levers: tuple[str, ...]) -> None:
        """Nur diese Hebel der aktuellen Zeile schreiben (Durchsetzen), Schreibgruppen vollstaendig. Wirft
        DeviceWriteError oder WriteBudgetExhausted."""
        values = self._row_values(self._current_row())
        wanted = {name for lever in levers for name in self._group(lever)}
        self._write({lever: values[lever] for lever in wanted if lever in values})

    def write_lever(self, lever: str, value: float) -> float:
        """Einen einzelnen Hebel schreiben (Mindestvorlauf, client-abgeleitet). Wirft DeviceWriteError oder
        WriteBudgetExhausted."""
        return self._write_lever(lever, value)

    def ensure_prepared(self, *, exempt: bool = False, require_aux: bool = True) -> bool:
        """Anlage vorbereiten (Vaillant: Zone auf Manuell). Direkt nach einer eigenen Umstellung zeigt HA den alten
        Modus bis zum naechsten Poll der Hersteller-Cloud: dann nicht erneut umstellen (Kontingent). Nach einer
        Umstellung ist der Sollwert des prepared_lever unbekannt: der letzte eigene Schreibwert darf ein folgendes
        Schreiben nicht mehr ueberspringen."""
        lever = self._description.prepared_lever
        if lever is None:
            return False
        if self._prepared_at is not None and self._clock() - self._prepared_at <= self._description.settle_seconds:
            return False
        if require_aux:
            self._require_aux(lever)
        if not exempt and self.daily_budget_reached():
            try:
                prepared = self._binding.is_prepared()
            except Exception as error:
                raise DeviceWriteError(lever, self._binding.ref(lever), error) from error
            if not prepared:
                self._check_daily_budget(lever)
        before = self._binding.physical_writes
        try:
            switched = self._binding.prepare()
        except Exception as error:
            raise DeviceWriteError(lever, self._binding.ref(lever), error) from error
        finally:
            self._count_physical(self._binding.physical_writes - before)
        if switched:
            self._prepared_at = self._clock()
            self._last_write_at[lever] = self._clock()
            self._last_written.pop(lever, None)
        return switched

    def prepare_start(self, start_value: float | None) -> None:
        """Start (Plan-Praezisierung 11, TP11): Anlage vorbereiten; meldet prepared_lever danach keinen brauchbaren
        Wert oder wurde gerade umgestellt, den Startwert schreiben und als Wiederherstellungspunkt speichern.
        Startwert: gespeicherter Punkt, sonst `start_value` (Raum-Soll). Wirft DeviceWriteError."""
        lever = self._description.prepared_lever
        if lever is None or not self._binding.has(lever) or not self._binding.needs_preparation():
            return
        switched = self.ensure_prepared()
        try:
            live = self._binding.read(lever)
        except Exception:
            live = None
        low, high = self._range(lever)
        forced = switched and self._description.preparation_resets_setpoint
        # Schon vorbereitet und ruhend (None) oder mit brauchbarem Wert: nichts schreiben, die naechste
        # Serverantwort setzt den Hebel.
        if not forced and (live is None or low <= live <= high):
            return
        point = self._store.state.restore_point
        start = point.get(lever) if point.get(lever) is not None else start_value
        if start is None:
            return
        written = self._write_lever(lever, start, force=forced)
        self._store.update(restore_point={**self._store.state.restore_point, lever: written})
        logger.warning("Startwert fuer %s geschrieben: %s", lever, written)

    def reseed_from_plant(self, lever: str) -> None:
        """Erster Start ohne gespeicherten prepared_lever (Neuinstallation, Update von vor TP11): ein vorhandener
        Wiederherstellungswert dieses Hebels stammt aus einer aelteren Regelung und wuerde sonst per Durchsetzen oder
        Boost-Ende geschrieben. Deshalb den Live-Wert (begrenzt, gerundet) uebernehmen; nicht lesbar: verwerfen.
        Waehrend eines Boosts steht die Anlage auf Boost-Werten: dann bleibt der Punkt. Wirft nur beim Speichern."""
        state = self._store.state
        if (
            lever not in self._description.lever_set.levers or not self._binding.has(lever)
            or state.boost_active or state.emergency_boost_active
        ):
            return
        try:
            live = self._binding.read(lever)
        except Exception as error:
            logger.warning("%s beim ersten Start nicht lesbar: %s", lever, error)
            live = None
        seeded = None
        if live is not None and _is_finite_number(live):
            seeded = target_value(live, *self._range(lever), self._description.steps[lever])
        logger.warning(
            "Erster Start: Wiederherstellungswert von %s %s -> %s (Anlagenwert)",
            lever, state.restore_point.get(lever), seeded,
        )
        point = {key: value for key, value in state.restore_point.items() if key != lever}
        if seeded is not None:
            point[lever] = seeded
        self._store.update(restore_point=point)

    def _current_row(self) -> str:
        state = self._store.state
        return _row(state.boost_active, state.emergency_boost_active)

    def _ensure_restore_point(self, *, allow_saved_fallback: bool) -> str | None:
        """Fehlende Werte des Wiederherstellungspunkts (erster Boost vor der ersten Serverantwort) einmalig von der
        Anlage lesen und speichern. None, wenn das nicht geht oder der Punkt noch nicht gespeichert ist -- ausser
        (allow_saved_fallback) auf dem zuletzt gespeicherten Punkt (_POINT_SAVED). Laeuft schon ein Boost, steht die
        Anlage auf Boost-Werten: dann wird nichts gelesen."""
        state = self._store.state
        point = state.restore_point
        missing = [
            lever for lever in self._description.lever_set.levers
            if self._binding.has(lever) and point.get(lever) is None
        ]
        if state.boost_active or state.emergency_boost_active:
            if missing:
                logger.info(
                    "Wiederherstellungspunkt fehlt (%s), die Anlage steht schon auf Boost-Werten - "
                    "nicht von der Anlage gelesen", ", ".join(missing),
                )
            return _POINT_CURRENT
        if not missing:
            if self._store.is_saved("restore_point"):
                return _POINT_CURRENT
            try:
                self._store.update(restore_point=dict(point))
            except Exception as error:
                logger.warning("Wiederherstellungspunkt nicht gespeichert (backup.json): %s", error)
                return self._saved_fallback() if allow_saved_fallback else None
            return _POINT_CURRENT
        try:
            live = {lever: self._binding.read(lever) for lever in missing}
        except Exception as error:
            logger.warning("Wiederherstellungspunkt nicht lesbar (%s): %s", ", ".join(missing), error)
            return None
        if not all(value is not None and _is_finite_number(value) for value in live.values()):
            logger.warning("Wiederherstellungspunkt ungueltig: %r", live)
            return None
        self._store.update(restore_point={**point, **live})
        logger.info("Wiederherstellungspunkt vor Boost gesichert: %s", live)
        return _POINT_CURRENT

    def capture_originals(self) -> None:
        """Die Hebel aus restore_originals vor dem ersten eigenen Schreiben als Ursprungswert merken (TP12h, P2-5). Nur
        einmal und nur solange es fuer den Hebel noch keinen Wiederherstellungswert gibt (sonst waere der Live-Wert
        schon ein gelernter). Ein Lesefehler ist kein Fehler: der naechste Aufruf versucht es erneut. Plan 3b: danach
        die Hilfs-Ursprungswerte (_capture_aux)."""
        self._capture_lever_originals()
        self._capture_aux()

    def _capture_aux(self) -> bool:
        """Hilfs-Ursprungswerte (aux_originals) einmal und vollstaendig merken; True, wenn sie bekannt sind oder das
        Binding keine hat. Ein Lesefehler ist kein Fehler: der naechste Aufruf versucht es erneut."""
        names = self._description.aux_originals
        if not names or self._store.state.aux_originals:
            return True
        try:
            values = self._binding.read_aux()
        except Exception as error:
            logger.warning("Ursprungswerte %s nicht lesbar, bleiben offen: %s", ", ".join(names), error)
            return False
        missing = [name for name in names if name not in values]
        if missing:
            logger.warning("Ursprungswerte %s fehlen, bleiben offen", ", ".join(missing))
            return False
        self._store.update(aux_originals={name: values[name] for name in names})
        logger.info("Ursprungswerte gemerkt: %s", values)
        return True

    def _require_aux(self, lever: str) -> None:
        """Vor dem ersten Schreiben, das Hilfswerte veraendern kann (Vorbereitung, prepared_lever), muessen sie gemerkt
        sein; sonst waere der spaeter gelesene Wert schon ein eigener. Wirft DeviceWriteError."""
        if not self._capture_aux():
            raise DeviceWriteError(
                lever, self._binding.ref(lever), RuntimeError("Ursprungswerte der Anlage nicht lesbar"),
            )

    def _restore_aux(self, values: Mapping[str, str | float], levers: Mapping[str, float]) -> None:
        before = self._binding.physical_writes
        try:
            self._binding.restore_aux(values, levers=levers)
        finally:
            self._count_physical(self._binding.physical_writes - before)

    def _capture_lever_originals(self) -> None:
        """Die Hebel aus restore_originals vor dem ersten eigenen Schreiben als Ursprungswert merken (TP12h, P2-5). Nur
        einmal und nur solange es fuer den Hebel noch keinen Wiederherstellungswert gibt (sonst waere der Live-Wert
        schon ein gelernter). Ein Lesefehler ist kein Fehler: der naechste Aufruf versucht es erneut."""
        for lever in self._description.restore_originals:
            state = self._store.state
            if (
                not self._binding.has(lever) or state.originals.get(lever) is not None
                or state.restore_point.get(lever) is not None
            ):
                continue
            try:
                live = self._binding.read(lever)
            except Exception as error:
                logger.warning("%s beim Start nicht lesbar, Ursprungswert bleibt offen: %s", lever, error)
                continue
            if live is None or not _is_finite_number(live):
                logger.warning("%s beim Start nicht numerisch (%r), Ursprungswert bleibt offen", lever, live)
                continue
            self._store.update(originals={**state.originals, lever: live})
            logger.info("Ursprungswert von %s gemerkt: %s", lever, live)

    def _saved_fallback(self) -> str | None:
        required = tuple(
            lever for lever in self._description.lever_set.levers if lever not in self._description.optional_restore
        )
        saved = self._store.saved_restore_point(required)
        if saved is None:
            return None
        self._store.revert_to_saved("restore_point")
        logger.warning(
            "Notfall-Boost auf dem zuletzt gespeicherten Wiederherstellungspunkt %s (neuer Punkt nicht speicherbar)",
            saved,
        )
        return _POINT_SAVED
