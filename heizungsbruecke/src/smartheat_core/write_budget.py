"""Schreibbudget fuer Cloud-Schreibvorgaenge (TP12b, Spec 3.1; AU-015). Die Hersteller-Cloud
(myVAILLANT) sperrt bei zu vielen Aufrufen ~2 h; ein gescheiterter Schreibvorgang darf deshalb nie
in jedem lokalen Check wiederholt werden.

Ein Log je Schluessel in BridgeState.write_budget (backup.json): {"day", "count", "last",
"limit_notified"?}. `count` zaehlt je nach Regel jeden Versuch oder nur Fehlschlaege des Tages.
`last` laeuft auf der monotonen Worker-Uhr und wird beim Laden verworfen (state._parse_budget): nach
einem Neustart gilt das Tageslimit weiter, der Mindestabstand beginnt neu."""
import logging
from dataclasses import dataclass

from smartheat_core import wallclock
from smartheat_core.logtext import lesbar

logger = logging.getLogger(__name__)

INTERVAL_SECONDS = 1800
MAX_PER_DAY = 6


@dataclass(frozen=True)
class Rule:
    # True: jeder Versuch zaehlt (vor dem Aufruf); False: nur Fehlschlaege, ein Erfolg setzt zurueck.
    every_attempt: bool
    # Mindestabstand nach dem n-ten gezaehlten Versuch des Tages; der letzte Eintrag gilt weiter.
    waits: tuple[float, ...]
    max_per_day: int | None  # None: kein Tageslimit


# Zonenvorbereitung: nur Fehlschlaege, 1/30 min, 6/Tag. Ein Erfolg kostet nichts.
RETRY_QUOTA = Rule(every_attempt=False, waits=(INTERVAL_SECONDS,), max_per_day=MAX_PER_DAY)
# Boost-Start (Comfort und Notfall, je eigener Zaehler): jeder Start zaehlt, auch ein erfolgreicher (A6-01, Regel 4):
# hoechstens 6 am Tag, mindestens 30 min Abstand.
START_QUOTA = Rule(every_attempt=True, waits=(INTERVAL_SECONDS,), max_per_day=MAX_PER_DAY)
# Rueckkehr zu den Regelwerten (Boost-Ende, Wiederherstellung): 5/15/30 min, dann alle 30 min, nie
# endgueltig aufgeben.
RETURN_STAIRCASE = Rule(every_attempt=False, waits=(300, 900, INTERVAL_SECONDS), max_per_day=None)

ENFORCE_PREFIX = "enforce:"
# Plan 3b: physische Schreibvorgaenge des Tages ueber alle Hebel (BindingDescription.daily_write_limit, Weishaupt).
TOTAL = "writes:total"
ZONE_PREPARE = "zone_prepare"
BOOST_START = "boost_start"
EMERGENCY_START = "emergency_start"
BOOST_END = "boost_end"
RESTORE = "restore"


def today() -> str:
    return wallclock.today().isoformat()


def allowed(entry: dict | None, rule: Rule, now: float, day: str) -> bool:
    if entry is None or entry.get("day") != day:
        return True
    count = entry["count"]
    if rule.max_per_day is not None and count >= rule.max_per_day:
        return False
    last = entry.get("last")
    if last is None or count == 0:
        return True
    return now - last >= rule.waits[min(count, len(rule.waits)) - 1]


def counted(entry: dict | None, now: float, day: str) -> dict:
    """Eintrag nach einem gezaehlten Versuch; ein Eintrag vom Vortag beginnt neu."""
    if entry is None or entry.get("day") != day:
        return {"day": day, "count": 1, "last": now}
    return {**entry, "count": entry["count"] + 1, "last": now}


def enforce_rule(per_day: int) -> Rule:
    """Durchsetzungs-Regel mit dem Tageslimit des Bindings (BindingDescription.enforce_per_day)."""
    return Rule(every_attempt=True, waits=(INTERVAL_SECONDS,), max_per_day=per_day)


def added(entry: dict | None, count: int, now: float, day: str) -> dict:
    """Eintrag nach `count` weiteren Schreibvorgaengen (TOTAL); ein Eintrag vom Vortag beginnt neu."""
    if entry is None or entry.get("day") != day:
        return {"day": day, "count": count, "last": now}
    return {**entry, "count": entry["count"] + count, "last": now}


def count_today(entry: dict | None, day: str) -> int:
    return entry["count"] if entry is not None and entry.get("day") == day else 0


def limit_first_reached(entry: dict | None, rule: Rule, day: str) -> bool:
    """True, wenn das Tageslimit heute erreicht und noch nicht gemeldet ist (`limit_notified`)."""
    return (
        rule.max_per_day is not None and entry is not None and entry.get("day") == day
        and entry["count"] >= rule.max_per_day and entry.get("limit_notified") != day
    )


def get(store, key: str) -> dict | None:
    return store.state.write_budget.get(key)


def put(store, key: str, entry: dict | None) -> None:
    """Setzt (None: entfernt) einen Eintrag. Best effort: scheitert das Speichern, gilt der Stand im
    Speicher weiter (StateStore.update aendert vor dem Schreiben); gemeldet wird das ueber
    datentraeger.py."""
    budget = dict(store.state.write_budget)
    if entry is None:
        if key not in budget:
            return
        del budget[key]
    else:
        budget[key] = entry
    try:
        store.update(write_budget=budget)
    except Exception as error:
        logger.warning("Schreibbudget '%s' nicht gespeichert (%s), gilt bis zum Neustart", key, lesbar(str(error)))


def may_attempt(store, key: str, rule: Rule, now: float) -> bool:
    return allowed(get(store, key), rule, now, today())


def before_attempt(store, key: str, rule: Rule, now: float, *, day: str | None = None) -> None:
    """Vor dem Aufruf: bei every_attempt zaehlt jeder Versuch, auch ein spaeter gescheiterter (Kontingent der Cloud)."""
    if rule.every_attempt:
        put(store, key, counted(get(store, key), now, day or today()))


def after_success(store, key: str, rule: Rule) -> None:
    """Ohne every_attempt setzt ein Erfolg zurueck; mit every_attempt bleibt die Zaehlung des Tages."""
    if not rule.every_attempt:
        put(store, key, None)


def after_failure(store, key: str, rule: Rule, now: float, *, day: str | None = None) -> None:
    """Ohne every_attempt zaehlt nur der Fehlschlag."""
    if not rule.every_attempt:
        put(store, key, counted(get(store, key), now, day or today()))
