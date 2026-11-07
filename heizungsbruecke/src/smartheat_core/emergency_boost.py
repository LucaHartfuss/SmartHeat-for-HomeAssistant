from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from smartheat_core.boost import COMFORT_BOOST_MAX_SECONDS
from smartheat_core.zahlen import ist_endliche_zahl

EMERGENCY_TRIGGER_K = 1.0


def decide_emergency_boost(
    room_actual: float,
    room_target: float,
    emergency_was_active: bool,
    exit_threshold_k: float,
) -> bool:
    """Reine Hysterese-Logik des Notbetriebs -- bewusst nur ausgewertet, waehrend Notbetrieb
    aktiv ist (regulation.run_local_check), nicht generell.

    Trigger (inaktiv -> aktiv): der Raum ist mehr als EMERGENCY_TRIGGER_K (1.0 K) unter
    dem Sollwert. Exit (aktiv -> inaktiv): der Raum hat sich bis auf `exit_threshold_k`
    an den Sollwert angenaehert -- derselbe Wert wie die bestehende
    `boost_threshold_k`-Option des Comfort-Boosts, kein eigenes Config-Feld.

    Die Boost-Werte stehen nicht hier, sondern in LocalSafety.emergency_boost_levers.
    """
    if emergency_was_active:
        return room_actual < room_target - exit_threshold_k
    return room_actual < room_target - EMERGENCY_TRIGGER_K


# --- Kaelteschutz -----------------------------------------------------------------------------------------------
# Nutzer-Entscheidung, Freigabe nach Regel 4 fuer genau diese Werte (Wachter: tests/test_regel4_werte.py). Der
# Kaelteschutz ist ein Notfall-Boost auch ausserhalb des Notbetriebs: er startet, wenn ein Kreis lange zu kalt bleibt,
# und endet bei Ankunft oder nach der Hoechstdauer des Comfort-Boosts.

#: Soll-Ausloeser: Abweichung des Ist vom Ziel ueber die Referenzraeume (Kelvin) ...
KAELTESCHUTZ_SOLL_DELTA_K = 2.0
#: ... durchgehend so lange.
KAELTESCHUTZ_SOLL_DAUER = timedelta(hours=3)
#: Frostnaehe-Ausloeser: ein Raum des Kreises mit gueltigem Ist unter dieser Temperatur (Grad) ...
KAELTESCHUTZ_FROST_C = 5.0
#: ... durchgehend so lange.
KAELTESCHUTZ_FROST_DAUER = timedelta(minutes=15)
#: Ohne Soll endet der Lauf, wenn jeder Raum mit gueltigem Ist mindestens so warm ist (Hysterese 2 K ueber der Frostnaehe).
KAELTESCHUTZ_ENDE_OHNE_SOLL_C = 7.0
#: Nur Ist-Werte in diesem Bereich (Grad) zaehlen; alles andere ist ein Fuehlerfehler und loest nie etwas aus.
KAELTESCHUTZ_IST_BEREICH = (-10.0, 35.0)
#: Laenger als das ohne gueltigen Wert (oder ohne Pruefung) beendet die Zaehlung der Ausloeser.
KAELTESCHUTZ_LUECKE_MAX = timedelta(minutes=15)
#: Hoechstdauer eines Laufs, gleich der des Comfort-Boosts.
KAELTESCHUTZ_HOECHSTDAUER = timedelta(seconds=COMFORT_BOOST_MAX_SECONDS)


@dataclass(frozen=True)
class Messwert:
    """Ist-Temperatur eines Raums mit dem Zeitpunkt der Messung; `wert` None heisst unlesbar."""

    wert: float | None
    zeit: datetime | None


@dataclass(frozen=True)
class KaelteschutzZustand:
    """Laufzustand je Kreis, vom Aufrufer zwischen den Pruefungen gehalten (die Funktion haelt keinen).
    `lauf_art`/`lauf_seit`: der aktive Lauf ("soll" oder "frost") und sein Beginn. Ohne Lauf zaehlen die Ausloeser:
    `soll_seit` ist der Beginn der durchgehenden Abweichung, `frost_seit` nennt je Raum den Beginn der durchgehenden
    Kaelte (Paare Raumnummer, Beginn); `zuletzt` ist der Zeitpunkt der letzten Pruefung."""

    lauf_art: str | None = None
    lauf_seit: datetime | None = None
    soll_seit: datetime | None = None
    frost_seit: tuple[tuple[int, datetime], ...] = ()
    zuletzt: datetime | None = None


@dataclass(frozen=True)
class KaelteschutzEntscheidung:
    """`art`: "aus", "soll" oder "frost". `grund`: kein_ausloeser | soll_abweichung | frostnaehe (Start) | laeuft |
    ankunft | hoechstdauer | uhr_zurueck (Ende). `gestartet` ist nur im Start-Aufruf wahr (der Aufrufer zaehlt dann den
    Start)."""

    art: str
    grund: str
    gestartet: bool
    zustand: KaelteschutzZustand


def gueltige_ist(jetzt: datetime, ist: Mapping[int, Messwert]) -> dict[int, float]:
    """Die Raeume mit gueltigem Ist: endlicher Wert im Bereich KAELTESCHUTZ_IST_BEREICH, gemessen hoechstens
    KAELTESCHUTZ_LUECKE_MAX vor `jetzt` (nicht danach). Das Delta fuer `kaelteschutz_entscheiden` rechnet der Aufrufer
    nur aus diesen Werten."""
    unten, oben = KAELTESCHUTZ_IST_BEREICH
    gueltig = {}
    for raum, messwert in ist.items():
        wert, zeit = messwert.wert, messwert.zeit
        if not ist_endliche_zahl(wert) or zeit is None or not unten <= wert <= oben:
            continue
        if timedelta(0) <= jetzt - zeit <= KAELTESCHUTZ_LUECKE_MAX:
            gueltig[raum] = wert
    return gueltig


def kaelteschutz_entscheiden(
    jetzt: datetime,
    *,
    ist: Mapping[int, Messwert],
    soll: float | None,
    delta: float | None,
    zustand: KaelteschutzZustand,
    arrival_threshold_k: float,
    lauf_dauer: timedelta | None = None,
) -> KaelteschutzEntscheidung:
    """Kaelteschutz eines Kreises, rein (keine Uhr, kein I/O, keine Eingabe wird veraendert).

    `lauf_dauer`: die Laufzeit eines laufenden Laufs nach einer monotonen Uhr, die der Aufrufer fuehrt (nur fuer einen
    Lauf, den dieser Prozess gestartet hat). Mit ihr zaehlt die Hoechstdauer nach dieser Spanne und nicht nach
    `jetzt - lauf_seit`: ein Sprung der Wanduhr beendet den Lauf weder zu frueh noch zu spaet. Ohne sie (None, etwa bei
    einem Lauf aus einem frueheren Prozess) gilt die Wanduhr, und ein Zurueckspringen beendet den Lauf.

    `ist`: das Ist je Raum des Kreises, Referenz- und Kontrollraeume. `soll`: Soll effektiv oder None (kein Soll).
    `delta`: Abweichung der Referenzraeume (`raum_delta.delta`, nur aus Werten von `gueltige_ist`) oder None;
    das wirksame Ist ist `soll - delta`. `arrival_threshold_k`: Ankunftsschwelle wie beim Comfort-Boost.

    Ohne Lauf zaehlen zwei Ausloeser: Frostnaehe (irgendein Raum mit gueltigem Ist unter KAELTESCHUTZ_FROST_C
    durchgehend KAELTESCHUTZ_FROST_DAUER, je Raum gezaehlt) und, nur mit Soll, die Abweichung ab
    KAELTESCHUTZ_SOLL_DELTA_K durchgehend KAELTESCHUTZ_SOLL_DAUER. Ein ungueltiger oder unlesbarer Wert, eine
    Abweichung unter der Schwelle oder eine Luecke ueber KAELTESCHUTZ_LUECKE_MAX beginnt die Zaehlung von vorn;
    bei beiden Ausloesern zugleich gilt Frost. Ein Lauf endet bei Ankunft (mit Soll: Ist mindestens
    Soll minus Ankunftsschwelle; ohne Soll: jeder gueltige Raum mindestens KAELTESCHUTZ_ENDE_OHNE_SOLL_C, mindestens
    einer gueltig; ein Frost-Lauf endet auch mit Soll nur so) oder nach KAELTESCHUTZ_HOECHSTDAUER; ein Fuehlerausfall beendet ihn nicht. Nach dem Ende beginnt
    alles von vorn."""
    if zustand.lauf_art is not None and zustand.lauf_seit is not None:
        return _laufend(jetzt, ist, soll, delta, zustand, arrival_threshold_k, lauf_dauer)
    return _zaehlen(jetzt, ist, soll, delta, zustand)


def _delta_gueltig(delta: float | None) -> bool:
    return ist_endliche_zahl(delta)


def _angekommen(jetzt, ist, soll, delta, arrival_threshold_k) -> bool:
    if soll is not None:
        return _delta_gueltig(delta) and delta <= arrival_threshold_k
    gueltig = gueltige_ist(jetzt, ist)
    return bool(gueltig) and all(wert >= KAELTESCHUTZ_ENDE_OHNE_SOLL_C for wert in gueltig.values())


def _laufend(jetzt, ist, soll, delta, zustand, arrival_threshold_k, lauf_dauer) -> KaelteschutzEntscheidung:
    dauer = jetzt - zustand.lauf_seit if lauf_dauer is None else lauf_dauer
    if dauer < timedelta(0):
        return KaelteschutzEntscheidung("aus", "uhr_zurueck", False, KaelteschutzZustand())
    if dauer >= KAELTESCHUTZ_HOECHSTDAUER:
        return KaelteschutzEntscheidung("aus", "hoechstdauer", False, KaelteschutzZustand())
    # Ein Frost-Lauf schuetzt den kaeltesten Raum, nicht den Soll-Vergleich der Referenzraeume: er endet auch mit Soll
    # nur nach der Regel ohne Soll (jeder Raum mit gueltigem Ist warm genug).
    soll_fuer_ende = None if zustand.lauf_art == "frost" else soll
    if _angekommen(jetzt, ist, soll_fuer_ende, delta, arrival_threshold_k):
        return KaelteschutzEntscheidung("aus", "ankunft", False, KaelteschutzZustand())
    return KaelteschutzEntscheidung(zustand.lauf_art, "laeuft", False, zustand)


def _zaehlen(jetzt, ist, soll, delta, zustand) -> KaelteschutzEntscheidung:
    fortlaufend = zustand.zuletzt is not None and timedelta(0) <= jetzt - zustand.zuletzt <= KAELTESCHUTZ_LUECKE_MAX
    soll_vorher = zustand.soll_seit if fortlaufend else None
    frost_vorher = dict(zustand.frost_seit) if fortlaufend else {}

    kalte_raeume = [raum for raum, wert in gueltige_ist(jetzt, ist).items() if wert < KAELTESCHUTZ_FROST_C]
    frost_seit = {raum: frost_vorher.get(raum, jetzt) for raum in kalte_raeume}
    soll_abweichung = soll is not None and _delta_gueltig(delta) and delta >= KAELTESCHUTZ_SOLL_DELTA_K
    soll_seit = (soll_vorher or jetzt) if soll_abweichung else None

    if any(jetzt - seit >= KAELTESCHUTZ_FROST_DAUER for seit in frost_seit.values()):
        return KaelteschutzEntscheidung("frost", "frostnaehe", True, KaelteschutzZustand("frost", jetzt, zuletzt=jetzt))
    if soll_seit is not None and jetzt - soll_seit >= KAELTESCHUTZ_SOLL_DAUER:
        return KaelteschutzEntscheidung("soll", "soll_abweichung", True, KaelteschutzZustand("soll", jetzt, zuletzt=jetzt))
    neu = KaelteschutzZustand(None, None, soll_seit, tuple(sorted(frost_seit.items())), jetzt)
    return KaelteschutzEntscheidung("aus", "kein_ausloeser", False, neu)
