"""Durchsetzen statt Melden (TP11, Spec 5.3). Weicht die Anlage vom Sollstand ab -- Steigung,
Parallelverschiebung (Zeile aus override.py), Mindestvorlauf (= Raum-Soll, min_flow.py) oder die
Zonen-Betriebsart --, hat jemand in der App oder in HA verstellt: SmartHeat schreibt den Sollstand
zurueck, meldet den Eingriff einmal (nicht kritisch, abschaltbar) und schickt Steigung/
Parallelverschiebung als KPI mit dem naechsten Snapshot.

Erst nach DETECTION_ROUNDS Runden in Folge und nie innerhalb von OWN_WRITE_SETTLE_SECONDS
(override.py) nach einem eigenen Schreiben dieser Rolle oder nach dem Start (mypyllant fragt die
Cloud nur alle 30 min ab, HA zeigt so lange den alten Wert; ein Schreiben kurz vor einem Neustart
ist unbekannt) -- dieselbe Regel wie der Quota-Check (Override.settled). Eine solche noch nicht
eingeschwungene Abweichung ist "offen": sie wird weder durchgesetzt noch als Rueckkehr gewertet.

Ein Eingriff (manual_override: KPI-Werte, "rollen" = verstellte Werte je Rolle, "signatur",
"gemeldet") laeuft bis zur Rueckkehr; nur eine neue Rolle oder ein neuer Wert erweitert ihn. Gemeldet
wird er genau einmal, und erst wenn tatsaechlich zurueckgeschrieben wurde -- uebernimmt die Anlage das
Rueckschreiben nicht oder scheitert es teilweise, bleibt es bei dieser einen Meldung
(Plan-Praezisierung "Durchsetzungs-Meldung"). Kontingent-Schutz der Hersteller-Cloud (myVAILLANT
sperrt bei zu vielen Aufrufen ~2 h): je Rolle hoechstens ein Schreibversuch (auch ein gescheiterter)
pro RETRY_SECONDS und MAX_WRITES_PER_DAY am Tag; danach bis zum naechsten Tag nur noch eine
Limit-Meldung. Ein offener Datenfehler pausiert die
Pruefung (die Anlage kann nach einem gescheiterten Schreiben legitim auf alten Werten stehen).
Meldet die Zone Wunschtemperatur 0 (heizt gerade nicht), ist das keine Abweichung."""
import logging
import math
from datetime import date, datetime

from heizungsbruecke import min_flow, plant
from heizungsbruecke.notifier import STATE_OK

logger = logging.getLogger(__name__)

KEY = "manueller_eingriff"
DETECTION_ROUNDS = 2
RETRY_SECONDS = 1800
MAX_WRITES_PER_DAY = 6
# Spec 5.3 nennt fuer die Steigung 0,01. Die Anlage stellt sie aber nur in Schritten von 0,05 dar,
# und der Quota-Check (Override._matches_the_device) ueberspringt ein Schreiben innerhalb eines
# halben Schritts. Mit 0,01 wuerde ein (theoretischer) Zwischenwert als Eingriff erkannt, aber nie
# geschrieben und trotzdem gezaehlt/gemeldet. Deshalb gilt hier der halbe Anlagenschritt (0,025):
# jede Abweichung um mindestens einen Anlagenschritt wird wie mit 0,01 erkannt, und jede erkannte
# Abweichung fuehrt zu einem echten Schreibvorgang. Parallelverschiebung (0,25 = halber Schritt 0,5)
# und Mindestvorlauf (0,1 > halber Schritt 0,05) erfuellen das schon.
TOLERANCE = {"curve_current": plant.STEPS["curve_current"] / 2, "shift_current": 0.25, "min_flow": 0.1}
ZONE_MODE = "zone_mode"
_EPSILON = 1e-9  # Gleitkomma-Rest (0.91 - 0.9) zaehlt nicht als Abweichung
_LABELS = {
    "curve_current": "Heizkurve", "shift_current": "Wunschtemperatur der Zone",
    "min_flow": "Mindestvorlauftemperatur", ZONE_MODE: "Betriebsart der Zone",
}
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
    return date.today()


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _set_misses(rt, misses: int) -> None:
    if rt.store.state.manual_override_misses != misses:
        rt.store.update(manual_override_misses=misses)


def _read(rt, role: str) -> float | None:
    """Live-Wert oder None (nicht lesbar, Zone inaktiv) -- None ist nie eine Abweichung."""
    ref = rt.manifest.entity_ids[role]
    try:
        value = plant.read_shift(rt.ha_api, ref) if role == "shift_current" else rt.ha_api.get_state(ref)
    except Exception as error:
        logger.info("Eingriff nicht pruefbar, %s nicht lesbar: %s", role, error)
        return None
    return value if _is_number(value) else None


def _zone_not_manual(rt) -> bool:
    ref = rt.manifest.entity_ids.get("shift_current")
    if ref is None or not plant.is_climate(ref):
        return False
    try:
        return rt.ha_api.get_raw_state(plant.entity_of(ref)) != plant.MANUAL_HVAC_MODE
    except Exception as error:
        logger.info("Betriebsart der Zone nicht lesbar: %s", error)
        return False


def _deviations(rt) -> tuple[dict, dict, set]:
    """(Sollwerte, abweichende Live-Werte je Rolle, offene Rollen). Offen = der Live-Wert weicht ab,
    die Rolle ist aber noch nicht eingeschwungen (Override.settled: eigenes Schreiben oder Start vor
    weniger als OWN_WRITE_SETTLE_SECONDS, HA hinkt vermutlich nach): weder durchsetzen noch als
    Rueckkehr werten. Die Betriebsart haengt an der Rolle shift_current (deren Schreiben stellt die
    Zone um)."""
    expected = rt.override.expected_values()
    wanted_min_flow = min_flow.expected(rt)
    if wanted_min_flow is not None and "min_flow" in rt.manifest.entity_ids:
        expected["min_flow"] = wanted_min_flow
    deviating: dict = {}
    pending: set = set()
    for role, value in expected.items():
        live = _read(rt, role)
        if live is None or abs(live - value) <= TOLERANCE[role] + _EPSILON:
            continue
        if rt.override.settled(role):
            deviating[role] = live
        else:
            pending.add(role)
    if _zone_not_manual(rt):
        if rt.override.settled("shift_current"):
            deviating[ZONE_MODE] = 0.0
        else:
            pending.add(ZONE_MODE)
    return expected, deviating, pending


def _may_write(rt, role: str) -> bool:
    """Kontingent: hoechstens einmal pro RETRY_SECONDS und MAX_WRITES_PER_DAY am Tag."""
    log = rt.store.state.enforce_log.get(role)
    today = _today().isoformat()
    if log is None or log["day"] != today:
        return True
    return log["count"] < MAX_WRITES_PER_DAY and rt.clock() - log["last"] >= RETRY_SECONDS


def _count_attempt(rt, role: str) -> None:
    """Zaehlt einen Schreibversuch VOR dem Aufruf: auch ein gescheiterter (z. B. 403 "Quota
    Exceeded") verbraucht Kontingent und darf nicht in jedem Takt wiederholt werden."""
    today = _today().isoformat()
    log = dict(rt.store.state.enforce_log)
    entry = log.get(role)
    count = entry["count"] + 1 if entry is not None and entry["day"] == today else 1
    log[role] = {"day": today, "count": count, "last": rt.clock()}
    rt.store.update(enforce_log=log)


def _limit_reached_first_time(rt, role: str) -> bool:
    """True genau einmal pro Tag und Rolle, wenn das Tageslimit erreicht ist (fuer die Meldung)."""
    log = rt.store.state.enforce_log.get(role)
    today = _today().isoformat()
    if log is None or log["day"] != today or log["count"] < MAX_WRITES_PER_DAY:
        return False
    if log.get("limit_notified") == today:
        return False
    rt.store.update(enforce_log={**rt.store.state.enforce_log, role: {**log, "limit_notified": today}})
    return True


def _restore(rt, expected: dict, deviating: dict) -> tuple[list[str], list[str]]:
    """Schreibt, was das Kontingent erlaubt. Gibt (zurueckgesetzte Rollen, heute erstmals am
    Tageslimit abgewiesene Rollen) zurueck. Betriebsart und Parallelverschiebung sind ein
    Rueckschreiben: write_roles(shift_current) stellt die Zone selbst um (override._write), ein
    zweiter Aufruf wuerde auf dem nachhinkenden HA-Zustand ein zweites set_hvac_mode senden. Ohne
    Sollwert fuer die Parallelverschiebung (kein Wiederherstellungspunkt) wird nur die Zone
    umgestellt."""
    written, at_limit = [], []
    for role in deviating:
        if role == "shift_current" and ZONE_MODE in deviating:
            continue
        if not _may_write(rt, role):
            if _limit_reached_first_time(rt, role):
                at_limit.append(role)
            continue
        _count_attempt(rt, role)
        try:
            if role == "min_flow":
                rt.override.write_min_flow(expected["min_flow"])
            elif role == ZONE_MODE and "shift_current" not in expected:
                rt.override.ensure_manual_zone()
            elif role == ZONE_MODE:
                rt.override.write_roles(("shift_current",))
            else:
                rt.override.write_roles((role,))
        except Exception:
            logger.exception("Zuruecksetzen von %s gescheitert, naechster Versuch fruehestens in %s s", role,
                             RETRY_SECONDS)
            continue
        written.append(role)
        if role == ZONE_MODE and "shift_current" in deviating and "shift_current" in expected:
            written.append("shift_current")
        logger.warning("Eingriff an %s zurueckgesetzt", role)
    return written, at_limit


def _kpi_value(rt, role: str, entries: dict, expected: dict, unsent: dict | None, field: str):
    """Wert fuer den KPI: der verstellte Wert des Kunden, sonst der noch nicht gesendete Wert eines
    frueheren Eingriffs (`unsent` = manual_override_pending), sonst der Sollwert, sonst der
    Live-Wert."""
    if role in entries:
        return entries[role]
    if unsent is not None and _is_number(unsent.get(field)):
        return unsent[field]
    if role in expected:
        return expected[role]
    return _read(rt, role)


def _persisted_kpi_value(rt, role: str, value):
    """KPI-Wert fuer den PERSISTIERTEN Eintrag (state.manual_override, ueber backup.json): ohne
    Live- oder Sollwert (Zone inaktiv und noch kein Wiederherstellungspunkt fuer diese Rolle) faellt
    er auf den zuletzt gespeicherten Sollwert zurueck, notfalls auf 0.0 -- nie auf None. Sonst waere
    der Eintrag nicht numerisch und state._is_override wuerfe ihn beim naechsten Neustart weg:
    rollen/signatur/gemeldet gingen verloren, derselbe Eingriff wuerde nach dem Neustart erneut
    gemeldet. Der ungefilterte KPI-Wert (kann None sein) bleibt fuer manual_override_pending
    massgeblich, das nur bei zwei Zahlen gesetzt wird."""
    if _is_number(value):
        return value
    fallback = getattr(rt.store.state, role)
    return fallback if _is_number(fallback) else 0.0


def _record(rt, expected: dict, deviating: dict) -> dict:
    """Fuehrt den laufenden Eingriff fort: Rollen, die schon zu ihm gehoeren (auch solche, die gerade
    offen sind), behalten ihren Wert; nur eine neue Rolle oder ein neuer Wert aendert ihn (neue
    Signatur, neuer KPI-Eintrag). Der KPI (manual_override_pending) wird zusammengefuehrt, nicht
    ersetzt: ein noch nicht gesendeter Wert des Kunden geht nicht verloren."""
    state = rt.store.state
    previous = state.manual_override
    known = previous.get("rollen", {}) if previous is not None else {}
    if previous is not None and all(known.get(role) == value for role, value in deviating.items()):
        return previous
    entries = {**known, **deviating}
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    pending = state.manual_override_pending
    kpi = {
        "curve": _kpi_value(rt, "curve_current", entries, expected, pending, "curve"),
        "shift": _kpi_value(rt, "shift_current", entries, expected, pending, "shift"),
        "erkannt": now,
    }
    record = {
        "curve": _persisted_kpi_value(rt, "curve_current", kpi["curve"]),
        "shift": _persisted_kpi_value(rt, "shift_current", kpi["shift"]),
        "erkannt": now,
        "rollen": entries,
        "signatur": ",".join(f"{role}={entries[role]:g}" for role in sorted(entries)),
        "gemeldet": previous.get("gemeldet") if previous is not None else None,
    }
    changes: dict = {"manual_override": record}
    if _is_number(kpi["curve"]) and _is_number(kpi["shift"]):
        changes["manual_override_pending"] = kpi
    rt.store.update(**changes)
    return record


def check_manual_override(rt) -> None:
    state = rt.store.state
    if state.delivery.datenfehler is not None:
        _set_misses(rt, 0)
        return
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
        was = ", ".join(_LABELS[role] for role in sorted(written))
        rt.notifier.notify(KEY, record["signatur"], MESSAGE.format(was=was), critical=False)
        rt.store.update(manual_override={**record, "gemeldet": record["signatur"]})
    if at_limit:
        limit_was = ", ".join(_LABELS[role] for role in sorted(at_limit))
        rt.notifier.notify(KEY, f"limit:{_today().isoformat()}:{limit_was}", LIMIT_MESSAGE.format(was=limit_was),
                           critical=False)
