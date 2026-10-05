"""Durchsetzen statt Melden (TP11, Spec 5.3). Weicht die Anlage vom Sollstand ab -- Steigung,
Parallelverschiebung, Heizgrenze (Zeilen der Hebel-Pipeline), Mindestvorlauf (= Raum-Soll, derived.py) oder die
Zonen-Betriebsart --, hat jemand in der App oder in HA verstellt: SmartHeat schreibt den Sollstand
zurueck, meldet den Eingriff einmal (nicht kritisch, abschaltbar) und schickt die Werte aller Hebel des
Hebelsatzes als KPI mit dem naechsten Snapshot (Plan-Praezisierung 6).

Erst nach DETECTION_ROUNDS Runden in Folge und nie innerhalb von settle_seconds des Bindings
nach einem eigenen Schreiben dieses Hebels oder nach dem Start (mypyllant fragt die
Cloud nur alle 30 min ab, HA zeigt so lange den alten Wert; ein Schreiben kurz vor einem Neustart
ist unbekannt) -- dieselbe Regel wie der Quota-Check (LeverPipeline.settled). Eine solche noch nicht
eingeschwungene Abweichung ist "offen": sie wird weder durchgesetzt noch als Rueckkehr gewertet.

Ein Eingriff (manual_override: KPI-Werte "levers", "rollen" = verstellte Werte je Hebel, "signatur",
"gemeldet") laeuft bis zur Rueckkehr; nur ein neuer Hebel oder ein neuer Wert erweitert ihn. Gemeldet
wird er genau einmal, und erst wenn tatsaechlich zurueckgeschrieben wurde -- uebernimmt die Anlage das
Rueckschreiben nicht oder scheitert es teilweise, bleibt es bei dieser einen Meldung
(Plan-Praezisierung "Durchsetzungs-Meldung"). Kontingent-Schutz der Hersteller-Cloud (myVAILLANT
sperrt bei zu vielen Aufrufen ~2 h): je Hebel hoechstens ein Schreibversuch (auch ein gescheiterter)
pro RETRY_SECONDS und BindingDescription.enforce_per_day am Tag (Vaillant MAX_WRITES_PER_DAY, Viessmann 4; eine
Schreibgruppe wie Viessmann curve+level teilt ein Kontingent); danach bis zum naechsten Tag nur noch eine
Limit-Meldung. Plan 3b: Ist das Tagesbudget der Pipeline erreicht (Weishaupt), wird weder geschrieben noch gezaehlt;
wegen des Budgets zurueckgestellte Serverwerte (deferred_levers) sind offen, kein Eingriff, und werden zu Beginn jeder
Pruefung still nachgeholt (LeverPipeline.write_deferred). Ein offener Datenfehler pausiert die
Pruefung (die Anlage kann nach einem gescheiterten Schreiben legitim auf alten Werten stehen).
Meldet die Zone Wunschtemperatur 0 (heizt gerade nicht), ist das keine Abweichung."""
import logging
import math
from collections.abc import Callable
from datetime import date
from typing import Any, Protocol

from smartheat_core import derived, wallclock, write_budget
from smartheat_core.pipeline import WriteBudgetExhausted

logger = logging.getLogger(__name__)


class EnforceRuntime(Protocol):
    """Was Durchsetzen braucht: Zustand (store), Hebel-Pipeline mit Binding (override), Meldungen (notifier:
    notify(key, state, message, critical, silent_ok=False)), monotone Uhr (clock)."""

    store: Any
    override: Any
    notifier: Any
    clock: Callable[[], float]


KEY = "manueller_eingriff"
# Zustand "ok" eines Meldeschluessels; muss notifier.STATE_OK im Add-on entsprechen (tests/test_core_enforce.py).
STATE_OK = "ok"
DETECTION_ROUNDS = 2
RETRY_SECONDS = write_budget.INTERVAL_SECONDS
MAX_WRITES_PER_DAY = write_budget.MAX_PER_DAY
# Toleranzen (Abweichung, ab der durchgesetzt wird) und Anzeigenamen je Hebel: BindingDescription.enforce_tolerance
# bzw. labels (Vaillant: smartheat_core.binding.VAILLANT_MYPYLLANT). Spec 5.3 nennt fuer die Steigung 0,01; die
# Anlage stellt sie aber nur in Schritten von 0,05 dar, und der Quota-Check (LeverPipeline._matches_the_device)
# ueberspringt ein Schreiben innerhalb eines halben Schritts. Mit 0,01 wuerde ein (theoretischer) Zwischenwert als
# Eingriff erkannt, aber nie geschrieben und trotzdem gezaehlt/gemeldet. Deshalb gilt der halbe Anlagenschritt
# (0,025): jede Abweichung um mindestens einen Anlagenschritt wird wie mit 0,01 erkannt, und jede erkannte
# Abweichung fuehrt zu einem echten Schreibvorgang. Wunschtemperatur (0,25 = halber Schritt 0,5) und Mindestvorlauf
# (0,1 > halber Schritt 0,05) erfuellen das schon. Die Heizgrenze (Anlagenschritt 0,1) nimmt wie die Steigung den
# halben Anlagenschritt, 0,05.
ZONE_MODE = "zone_mode"
_EPSILON = 1e-9  # Gleitkomma-Rest (0.91 - 0.9) zaehlt nicht als Abweichung
MESSAGE = (
    "SmartHeat: Die Heizungseinstellung wurde in der App verstellt ({was}) und zurückgesetzt. "
    "Einstellungen bitte nur über das Raumthermostat."
)
LIMIT_MESSAGE = (
    "SmartHeat: Die Heizungseinstellung wird immer wieder verstellt ({was}). SmartHeat setzt sie heute "
    "nicht mehr zurück, um das Abrufkontingent der Hersteller-Cloud zu schonen, und versucht es morgen erneut."
)
RETURN_MESSAGE = "SmartHeat: Die Heizungseinstellung steht wieder auf den gelernten Werten."

def _today() -> date:
    return wallclock.today()


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _set_misses(rt: EnforceRuntime, misses: int) -> None:
    if rt.store.state.manual_override_misses != misses:
        rt.store.update(manual_override_misses=misses)


def _labels(rt: EnforceRuntime) -> dict:
    """Anzeigenamen je Hebel und fuer die Betriebsart (Kundentext, wortgleich bis 0.29.0)."""
    description = rt.override.binding.description
    return {**description.labels, ZONE_MODE: description.preparation_label}


def _read(rt: EnforceRuntime, lever: str) -> float | None:
    """Live-Wert oder None (nicht lesbar, Zone inaktiv) -- None ist nie eine Abweichung."""
    try:
        value = rt.override.binding.read(lever)
    except Exception as error:
        logger.info("Eingriff nicht pruefbar, %s nicht lesbar: %s", lever, error)
        return None
    return value if _is_number(value) else None


def _zone_not_manual(rt: EnforceRuntime) -> bool:
    binding = rt.override.binding
    if not binding.needs_preparation():
        return False
    try:
        return not binding.is_prepared()
    except Exception as error:
        logger.info("Betriebsart der Zone nicht lesbar: %s", error)
        return False


def _deviations(rt: EnforceRuntime) -> tuple[dict, dict, set]:
    """(Sollwerte, abweichende Live-Werte je Hebel, offene Hebel). Offen = der Live-Wert weicht ab,
    der Hebel ist aber noch nicht eingeschwungen (LeverPipeline.settled: eigenes Schreiben oder Start vor
    weniger als settle_seconds, HA hinkt vermutlich nach): weder durchsetzen noch als
    Rueckkehr werten. Die Betriebsart haengt am prepared_lever des Bindings (dessen Schreiben stellt die
    Zone um)."""
    tolerance = rt.override.binding.description.enforce_tolerance
    expected = rt.override.expected_values()
    wanted_min_flow = derived.expected(rt)
    if wanted_min_flow is not None and rt.override.binding.has("min_flow"):
        expected["min_flow"] = wanted_min_flow
    deviating: dict = {}
    pending: set = set(rt.store.state.deferred_levers)  # Plan 3b: Serverwert wegen Tagesbudget noch offen
    for lever, value in expected.items():
        if lever in pending:
            continue
        live = _read(rt, lever)
        if live is None or abs(live - value) <= tolerance[lever] + _EPSILON:
            continue
        if rt.override.settled(lever):
            deviating[lever] = live
        else:
            pending.add(lever)
    if _zone_not_manual(rt):
        if rt.override.settled(rt.override.binding.description.prepared_lever):
            deviating[ZONE_MODE] = 0.0
        else:
            pending.add(ZONE_MODE)
    return expected, deviating, pending


def _group(rt: EnforceRuntime, lever: str) -> tuple[str, ...]:
    """Schreibgruppe des Hebels (Plan 3b, Viessmann curve+level), sonst nur der Hebel selbst."""
    groups = rt.override.binding.description.write_groups
    return next((group for group in groups if lever in group), (lever,))


def _budget_key(rt: EnforceRuntime, lever: str) -> str:
    """enforce:<hebel>; eine Schreibgruppe teilt einen Schluessel (enforce:curve+level)."""
    return write_budget.ENFORCE_PREFIX + "+".join(_group(rt, lever))


def _rule(rt: EnforceRuntime) -> write_budget.Rule:
    """Kontingent des Bindings: 1/RETRY_SECONDS, enforce_per_day am Tag (Vaillant 6 = write_budget.QUOTA)."""
    return write_budget.enforce_rule(rt.override.binding.description.enforce_per_day)


def _may_write(rt: EnforceRuntime, lever: str) -> bool:
    """Kontingent: hoechstens einmal pro RETRY_SECONDS und enforce_per_day am Tag (_rule)."""
    entry = write_budget.get(rt.store, _budget_key(rt, lever))
    return write_budget.allowed(entry, _rule(rt), rt.clock(), _today().isoformat())


def _count_attempt(rt: EnforceRuntime, lever: str) -> None:
    """Zaehlt einen Schreibversuch VOR dem Aufruf: auch ein gescheiterter (z. B. 403 "Quota
    Exceeded") verbraucht Kontingent und darf nicht in jedem Takt wiederholt werden."""
    key = _budget_key(rt, lever)
    write_budget.put(rt.store, key, write_budget.counted(write_budget.get(rt.store, key), rt.clock(), _today().isoformat()))


def _limit_reached_first_time(rt: EnforceRuntime, lever: str) -> bool:
    """True genau einmal pro Tag und Hebel(-gruppe), wenn das Tageslimit erreicht ist (fuer die Meldung)."""
    key, day = _budget_key(rt, lever), _today().isoformat()
    entry = write_budget.get(rt.store, key)
    if not write_budget.limit_first_reached(entry, _rule(rt), day):
        return False
    assert entry is not None  # limit_first_reached verlangt einen Eintrag
    write_budget.put(rt.store, key, {**entry, "limit_notified": day})
    return True


def _restore(rt: EnforceRuntime, expected: dict, deviating: dict) -> tuple[list[str], list[str]]:
    """Schreibt, was das Kontingent erlaubt. Gibt (zurueckgesetzte Hebel, heute erstmals am
    Tageslimit abgewiesene Hebel) zurueck. Betriebsart und prepared_lever (Wunschtemperatur der Zone) sind ein
    Rueckschreiben: write_levers(prepared_lever) stellt die Zone selbst um (LeverPipeline._write), ein
    zweiter Aufruf wuerde auf dem nachhinkenden HA-Zustand ein zweites set_hvac_mode senden. Ohne
    Sollwert fuer den prepared_lever (kein Wiederherstellungspunkt) wird nur die Zone
    umgestellt."""
    prepared = rt.override.binding.description.prepared_lever
    written, at_limit = [], []
    if rt.override.daily_budget_reached():
        # Plan 3b (Weishaupt): Tagesbudget erreicht -- nichts schreiben und nichts zaehlen. Hier kein Hinweis zum
        # Tageslimit: den gibt die Pipeline, sobald sie einen Schreibvorgang am Limit abweist (Serverwerte,
        # Vorbereitung, Mindestvorlauf); der naechste Takt nach dem Tageswechsel setzt zurueck.
        logger.info("Eingriff nicht zurueckgesetzt: Tagesbudget der Schreibvorgaenge erreicht")
        return written, at_limit
    done: set[str] = set()
    for lever in deviating:
        if lever == prepared and ZONE_MODE in deviating:
            continue
        if lever in done:
            continue
        done.update(_group(rt, lever))
        if not _may_write(rt, lever):
            if _limit_reached_first_time(rt, lever):
                at_limit.append(lever)
            continue
        if rt.override.daily_budget_reached():
            # Plan 3b: ein frueherer Hebel dieses Takts hat das Tagesbudget aufgebraucht -- wie oben weder schreiben
            # noch zaehlen.
            logger.info("Eingriff an %s nicht zurueckgesetzt: Tagesbudget der Schreibvorgaenge erreicht", lever)
            break
        _count_attempt(rt, lever)
        try:
            if lever == "min_flow":
                rt.override.write_lever("min_flow", expected["min_flow"])
            elif lever == ZONE_MODE and prepared not in expected:
                rt.override.ensure_prepared()
            elif lever == ZONE_MODE:
                rt.override.write_levers((prepared,))
            else:
                rt.override.write_levers((lever,))
        except WriteBudgetExhausted:
            # Budget waehrend dieses Schreibens erreicht (z. B. durch die Vorbereitung): kein Fehler, kein Retry-Takt.
            logger.info("Eingriff an %s nicht zurueckgesetzt: Tagesbudget der Schreibvorgaenge erreicht", lever)
            break
        except Exception:
            logger.exception("Zuruecksetzen von %s gescheitert, naechster Versuch fruehestens in %s s", lever,
                             RETRY_SECONDS)
            continue
        written.extend(member for member in _group(rt, lever) if member in deviating)
        if lever == ZONE_MODE and prepared in deviating and prepared in expected:
            written.append(prepared)
        logger.warning("Eingriff an %s zurueckgesetzt", lever)
    return written, at_limit


def _kpi_value(rt: EnforceRuntime, lever: str, entries: dict, expected: dict, unsent: dict | None):
    """Wert fuer den KPI: der verstellte Wert des Kunden, sonst der noch nicht gesendete Wert eines
    frueheren Eingriffs (`unsent` = manual_override_pending), sonst der Sollwert, sonst der
    Live-Wert."""
    if lever in entries:
        return entries[lever]
    if unsent is not None and _is_number(unsent["levers"].get(lever)):
        return unsent["levers"][lever]
    if lever in expected:
        return expected[lever]
    return _read(rt, lever)


def _persisted_kpi_value(rt: EnforceRuntime, lever: str, value):
    """KPI-Wert fuer den PERSISTIERTEN Eintrag (state.manual_override, ueber backup.json): ohne
    Live- oder Sollwert (Zone inaktiv und noch kein Wiederherstellungspunkt fuer diesen Hebel) faellt
    er auf den zuletzt gespeicherten Sollwert zurueck, notfalls auf 0.0 -- nie auf None. Sonst waere
    der Eintrag nicht numerisch und state._is_override wuerfe ihn beim naechsten Neustart weg:
    rollen/signatur/gemeldet gingen verloren, derselbe Eingriff wuerde nach dem Neustart erneut
    gemeldet. Der ungefilterte KPI-Wert (kann None sein) bleibt fuer manual_override_pending
    massgeblich, das nur gesetzt wird, wenn jeder Hebel eine Zahl hat."""
    if _is_number(value):
        return value
    fallback = rt.store.state.restore_point.get(lever)
    return fallback if _is_number(fallback) else 0.0


def _record(rt: EnforceRuntime, expected: dict, deviating: dict) -> dict:
    """Fuehrt den laufenden Eingriff fort: Hebel, die schon zu ihm gehoeren (auch solche, die gerade
    offen sind), behalten ihren Wert; nur ein neuer Hebel oder ein neuer Wert aendert ihn (neue
    Signatur, neuer KPI-Eintrag). Der KPI (manual_override_pending) wird zusammengefuehrt, nicht
    ersetzt: ein noch nicht gesendeter Wert des Kunden geht nicht verloren."""
    state = rt.store.state
    previous = state.manual_override
    known = previous.get("rollen", {}) if previous is not None else {}
    if previous is not None and all(known.get(lever) == value for lever, value in deviating.items()):
        return previous
    entries = {**known, **deviating}
    now = wallclock.now().isoformat(timespec="seconds")
    pending = state.manual_override_pending
    # Alle zugeordneten Hebel des Hebelsatzes (Plan-Praezisierung 6), wie Snapshot und Sollwerte (expected_values).
    binding = rt.override.binding
    levers = [lever for lever in binding.description.lever_set.levers if binding.has(lever)]
    kpi_levers = {lever: _kpi_value(rt, lever, entries, expected, pending) for lever in levers}
    record = {
        "levers": {lever: _persisted_kpi_value(rt, lever, value) for lever, value in kpi_levers.items()},
        "erkannt": now,
        "rollen": entries,
        "signatur": ",".join(f"{lever}={entries[lever]:g}" for lever in sorted(entries)),
        "gemeldet": previous.get("gemeldet") if previous is not None else None,
    }
    changes: dict = {"manual_override": record}
    if all(_is_number(value) for value in kpi_levers.values()):
        changes["manual_override_pending"] = {"levers": kpi_levers, "erkannt": now}
    rt.store.update(**changes)
    return record


def check_manual_override(rt: EnforceRuntime) -> None:
    state = rt.store.state
    if state.delivery.datenfehler is not None:
        _set_misses(rt, 0)
        return
    rt.override.write_deferred()
    expected, deviating, pending = _deviations(rt)
    if not deviating:
        _set_misses(rt, 0)
        if pending:
            # Eigenes Schreiben noch nicht eingeschwungen: weder Rueckkehr noch neuer Eingriff.
            return
        if state.manual_override is not None:
            rt.store.update(manual_override=None)
        rt.notifier.notify(KEY, STATE_OK, RETURN_MESSAGE, critical=False, silent_ok=True)
        return
    misses = min(state.manual_override_misses + 1, DETECTION_ROUNDS)
    _set_misses(rt, misses)
    if misses < DETECTION_ROUNDS:
        return

    record = _record(rt, expected, deviating)
    written, at_limit = _restore(rt, expected, deviating)
    # Einmal pro Eingriff melden, und nur, wenn tatsaechlich zurueckgesetzt wurde (am Tageslimit oder
    # nach einem gescheiterten Schreiben nicht: dann gilt hoechstens die Limit-Meldung).
    if written and record.get("gemeldet") != record["signatur"]:
        was = ", ".join(_labels(rt)[lever] for lever in sorted(written))
        rt.notifier.notify(KEY, record["signatur"], MESSAGE.format(was=was), critical=False)
        rt.store.update(manual_override={**record, "gemeldet": record["signatur"]})
    if at_limit:
        limit_was = ", ".join(_labels(rt)[lever] for lever in sorted(at_limit))
        rt.notifier.notify(KEY, f"limit:{_today().isoformat()}:{limit_was}", LIMIT_MESSAGE.format(was=limit_was),
                           critical=False)
