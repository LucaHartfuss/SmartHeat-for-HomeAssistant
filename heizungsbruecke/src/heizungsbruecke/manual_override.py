"""Durchsetzen statt Melden (TP11, Spec 5.3). Weicht die Anlage vom Sollstand ab -- Steigung,
Parallelverschiebung (Zeile aus override.py), Mindestvorlauf (= Raum-Soll, min_flow.py) oder die
Zonen-Betriebsart --, hat jemand in der App oder in HA verstellt: SmartHeat schreibt den Sollstand
zurueck, meldet den Eingriff einmal (nicht kritisch, abschaltbar) und schickt Steigung/
Parallelverschiebung als KPI mit dem naechsten Snapshot.

Erst nach DETECTION_ROUNDS Runden in Folge und nie innerhalb von OWN_WRITE_SETTLE_SECONDS
(override.py) nach einem eigenen Schreiben dieser Rolle (mypyllant fragt die Cloud nur alle 30 min
ab, HA zeigt so lange den alten Wert). Eine solche noch nicht eingeschwungene Abweichung ist
"offen": sie wird weder durchgesetzt noch als Rueckkehr gewertet -- uebernimmt die Anlage das
Rueckschreiben nicht, bleibt es so bei einer Meldung pro Eingriff (Plan-Praezisierung
"Durchsetzungs-Meldung"). Kontingent-Schutz der Hersteller-Cloud (myVAILLANT sperrt bei zu vielen
Aufrufen ~2 h): je Rolle hoechstens ein Rueckschreiben pro RETRY_SECONDS und MAX_WRITES_PER_DAY am
Tag; danach bis zum naechsten Tag nur noch eine Meldung. Ein offener Datenfehler pausiert die
Pruefung (die Anlage kann nach einem gescheiterten Schreiben legitim auf alten Werten stehen).
Meldet die Zone Wunschtemperatur 0 (heizt gerade nicht), ist das keine Abweichung."""
import logging
import math
from datetime import date, datetime

from heizungsbruecke import min_flow, plant
from heizungsbruecke.notifier import STATE_OK
from heizungsbruecke.override import OWN_WRITE_SETTLE_SECONDS

logger = logging.getLogger(__name__)

KEY = "manueller_eingriff"
DETECTION_ROUNDS = 2
RETRY_SECONDS = 1800
MAX_WRITES_PER_DAY = 6
TOLERANCE = {"curve_current": 0.01, "shift_current": 0.25, "min_flow": 0.1}
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


def _settled(rt, role: str) -> bool:
    since = rt.override.seconds_since_write(role)
    return since is None or since >= OWN_WRITE_SETTLE_SECONDS


def _deviations(rt) -> tuple[dict, dict, set]:
    """(Sollwerte, abweichende Live-Werte je Rolle, offene Rollen). Offen = der Live-Wert weicht ab,
    die Rolle wurde aber vor weniger als OWN_WRITE_SETTLE_SECONDS selbst geschrieben (HA hinkt
    vermutlich nach): weder durchsetzen noch als Rueckkehr werten. Die Betriebsart haengt an der
    Rolle shift_current (deren Schreiben stellt die Zone um)."""
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
        if _settled(rt, role):
            deviating[role] = live
        else:
            pending.add(role)
    if _zone_not_manual(rt):
        if _settled(rt, "shift_current"):
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


def _count_write(rt, role: str) -> None:
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


def _restore(rt, expected: dict, deviating: dict) -> list[str]:
    """Schreibt, was das Kontingent erlaubt. Gibt die Rollen zurueck, die heute erstmals am
    Tageslimit abgewiesen wurden (fuer genau eine Meldung). Betriebsart und Parallelverschiebung
    sind ein Rueckschreiben: write_roles(shift_current) stellt die Zone selbst um (override._write),
    ein zweiter Aufruf wuerde auf dem nachhinkenden HA-Zustand ein zweites set_hvac_mode senden."""
    at_limit = []
    for role in deviating:
        if role == "shift_current" and ZONE_MODE in deviating:
            continue
        if not _may_write(rt, role):
            if _limit_reached_first_time(rt, role):
                at_limit.append(role)
            continue
        try:
            if role == "min_flow":
                rt.override.write_min_flow(expected["min_flow"])
            elif role == ZONE_MODE:
                rt.override.write_roles(("shift_current",))
            else:
                rt.override.write_roles((role,))
        except Exception:
            logger.exception("Zuruecksetzen von %s gescheitert, naechster Versuch im naechsten Takt", role)
            continue
        _count_write(rt, role)
        logger.warning("Eingriff an %s zurueckgesetzt", role)
    return at_limit


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

    was = ", ".join(_LABELS[role] for role in sorted(deviating))
    signature = ",".join(f"{role}={deviating[role]:g}" for role in sorted(deviating))
    if state.manual_override is None or state.manual_override.get("signatur") != signature:
        detected = {
            "curve": deviating.get("curve_current", expected.get("curve_current")),
            "shift": deviating.get("shift_current", expected.get("shift_current")),
            "erkannt": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        if _is_number(detected["curve"]) and _is_number(detected["shift"]):
            rt.store.update(manual_override={**detected, "signatur": signature}, manual_override_pending=detected)
        rt.notifier.notify(KEY, signature, MESSAGE.format(was=was), critical=False)
    at_limit = _restore(rt, expected, deviating)
    if at_limit:
        limit_was = ", ".join(_LABELS[role] for role in sorted(at_limit))
        rt.notifier.notify(KEY, f"limit:{_today().isoformat()}:{limit_was}", LIMIT_MESSAGE.format(was=limit_was),
                           critical=False)
