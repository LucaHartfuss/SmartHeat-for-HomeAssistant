"""Lokaler Check: Comfort- und Notfall-Boost und Kaelteschutz entscheiden, Sollwert-Historie fuehren und
entscheiden, ob ein neuer Tick faellig ist. Welche Werte dann auf der Anlage stehen,
entscheidet die Hebel-Pipeline (LeverPipeline.set_boosts)."""
import logging
from dataclasses import replace
from datetime import UTC, datetime

from smartheat_core import raum_delta, wallclock
from smartheat_core.boost import UNREADABLE_ROOM_CHECKS, comfort_boost_expired, decide_boost
from smartheat_core.emergency_boost import (
    KaelteschutzEntscheidung,
    KaelteschutzZustand,
    Messwert,
    decide_emergency_boost,
    gueltige_ist,
    kaelteschutz_entscheiden,
)
from smartheat_core.logtext import lesbar
from smartheat_runtime import kreiszustand, testphase
from smartheat_runtime.plausibility import ROOM_ACTUAL_RANGE, is_plausible
from smartheat_runtime.ports import SignalUngueltig
from smartheat_runtime.runtime import Runtime
from smartheat_runtime.state import parse_since

logger = logging.getLogger(__name__)

#: Raumnummer des Kreissensors `room_actual`, wenn die Konfiguration keine Raeume nennt (ein Host ohne Raeume).
_KREISSENSOR = 0


def read_room_target_live(rt: Runtime) -> float | None:
    """Liest room_target an HA vorbei am Stable-Target-Cache. Nur fuer Boot, den entprellten
    room_target-Trigger, (Wieder-)Verbinden und den Watchdog. None, wenn die Rolle fehlt."""
    if "room_target" not in rt.manifest.refs:
        return None
    return rt.signals.get_state(rt.manifest.refs["room_target"])


def refresh_stable_target(rt: Runtime) -> None:
    """Ist room_target nicht lesbar, bleibt der alte Wert: die Tick-Pruefung laeuft weiter, und
    der Versuch meldet den Fuehler dann als Datenfehler."""
    try:
        value = read_room_target_live(rt)
    except Exception as error:
        logger.warning(
            "room_target nicht lesbar, Stable-Target-Cache bleibt bei %s: %s", rt.store.state.stable_target,
            lesbar(str(error)),
        )
        return
    rt.store.update(stable_target=value)
    logger.info("Stable-Target-Cache aktualisiert: room_target=%s", value)


def wirksames_raum_ist(rt: Runtime, room_target: float) -> float:
    """Das Ist, mit dem Boost und Notfall-Boost rechnen: `Soll - Delta` ueber die Referenzraeume des Kreises (Ziel je Raum
    aus Soll und Versatz). Ein Raum ohne lesbaren Wert im Raumbereich zaehlt nicht, Kontrollraeume nie. Ohne Referenzraeume
    in der Konfiguration (ein Host ohne Raeume) gilt der Kreissensor `room_actual`. Ist kein Referenzraum gueltig, wirft die
    Funktion SignalUngueltig: der Aufrufer zaehlt das wie einen nicht lesbaren Raum."""
    referenz = dict(rt.config.referenzraeume)
    if not referenz:
        return rt.signals.get_state(rt.manifest.refs["room_actual"])
    ist = {}
    grund = "kein Referenzraum liefert einen gueltigen Wert"
    for raum in referenz:
        ref = rt.manifest.raum_refs.get(raum)
        if ref is None:
            continue
        try:
            wert = rt.signals.get_state(ref)
        except SignalUngueltig as error:  # erwarteter Datenfehler; jeder andere Fehler geht an den Aufrufer
            logger.debug("Referenzraum %d nicht lesbar: %s", raum, lesbar(str(error)))
            grund = str(error)
            continue
        if is_plausible(wert, ROOM_ACTUAL_RANGE):
            ist[raum] = wert
    wirksam = raum_delta.wirksames_ist(room_target, ist, referenz)
    if wirksam is None:
        raise SignalUngueltig(grund)
    return wirksam


def run_local_check(rt: Runtime) -> None:
    """Comfort-Boost nur bei Sollwerterhoehung, Notfall-Boost nur waehrend Notbetrieb, Kaelteschutz (ein Notfall-Boost
    auch ausserhalb des Notbetriebs) bei langer Abweichung oder Frostnaehe. room_target kommt aus dem
    Stable-Target-Cache, damit ein Zwischenwert beim Verstellen keinen Boost ausloest. I/O-Fehler gehen an den
    Aufrufer. In der Testphase ohne Setwell stellt der Check beim Eintritt die Hebel zurueck und startet keinen
    Comfort-Boost; der Kaelteschutz und der Notfall-Boost des Notbetriebs gelten weiter. Steht der Kreis ausser Betrieb,
    stellt der Check beim Eintritt die Hebel zurueck und entscheidet danach nur den Kaelteschutz (Frostnaehe, ohne Soll)."""
    testphase.pruefen(rt)
    kreiszustand.pruefen(rt)
    if kreiszustand.ausser_betrieb(rt):
        _ausser_betrieb_pruefen(rt)
        return
    manifest = rt.manifest
    if "room_actual" not in manifest.refs or "room_target" not in manifest.refs:
        return
    state = rt.store.state
    if state.abo_finished:
        return
    room_target = state.stable_target
    now = wallclock.now()
    # Der Kaelteschutz laeuft auch ohne Soll (dann nur die Frostnaehe) und ohne lesbaren Referenzraum. Eine Bedingung
    # fuer seine Wirksamkeit gehoert vor diesen Aufruf. Bei Soll-Quelle aus (Thermostat aus, Frostschutz-Stellung) haelt
    # der Speicher nur den alten Wunsch: der Kaelteschutz rechnet dann ohne Soll.
    schutz = _kaelteschutz_entscheiden(rt, now, None if rt.soll_quelle_aus else room_target)
    if room_target is None:
        logger.warning("Lokaler Check uebersprungen: Stable-Target-Cache noch nicht befuellt (room_target=None)")
        _kaelteschutz_allein(rt, schutz, now)
        return

    try:
        room_actual = wirksames_raum_ist(rt, room_target)
    except Exception:
        _count_unreadable_room(rt)
        _kaelteschutz_allein(rt, schutz, now)
        raise
    if state.room_actual_misses:
        rt.store.update(room_actual_misses=0)
    # Nicht lesbarer oder zeitzonenloser Zeitstempel = unbekannt (wie ein Boost vor dem Update), nie ein Abbruch.
    since = parse_since(state.boost_since)
    mono_seit = None if rt.boost_mono_start is None else rt.clock() - rt.boost_mono_start
    expired = state.boost_active and comfort_boost_expired(since, now, mono_seit)
    if expired:
        logger.info("Comfort-Boost endet nach der Hoechstdauer (seit %s)", state.boost_since)
    safety = rt.override.safety
    # Ohne Comfort-Boost-Werte (z. B. Fussbodenheizung) gibt es keinen Comfort-Boost.
    comfort = not expired and bool(safety.comfort_boost) and not testphase.aktiv(rt) and decide_boost(
        room_actual=room_actual,
        room_target=room_target,
        previous_room_target=state.last_room_target,
        boost_was_active=state.boost_active,
        arrival_threshold_k=safety.arrival_threshold_k,
    )
    emergency = state.delivery.notbetrieb and decide_emergency_boost(
        room_actual=room_actual,
        room_target=room_target,
        emergency_was_active=state.emergency_boost_active,
        exit_threshold_k=safety.arrival_threshold_k,
    )
    was_active = state.boost_active
    comfort_set, _ = _boosts_setzen(rt, schutz, now, comfort=comfort, notfall=emergency)
    _track_boost_since(rt, now, was_active)

    # B5: wurde der Comfort-Start mangels Wiederherstellungspunkt, wegen der Wartezeit oder eines Fehlers abgelehnt,
    # bleibt der alte Sollwert gemerkt, damit der naechste Check die Erhoehung erneut sieht. Wegen der Tageszahl oder
    # des Tageslimits abgelehnt gilt sie als verbraucht: sonst startete nach dem Tageswechsel ein Boost fuer eine
    # Erhoehung vom Vortag.
    refused = comfort and not comfort_set and not rt.override.comfort_start_heute_gesperrt
    remembered = state.last_room_target if refused else room_target
    rt.store.update_saved(last_room_target=remembered)


def _ausser_betrieb_pruefen(rt: Runtime) -> None:
    """Ein Kreis ausser Betrieb: nur der Kaelteschutz mit dem Frostnaehe-Ausloeser (kein Soll, es wird auch nicht gelesen).
    Kein Comfort-Boost und kein Notfall-Boost des Notbetriebs; ein Lauf endet bei jedem gueltigen Raum ab 7 Grad oder nach
    der Hoechstdauer. Ohne gueltigen Fuehler passiert nichts. Nach `abo_finished` des ganzen Geraets gilt auch das nicht."""
    state = rt.store.state
    if state.abo_finished:
        return
    if not rt.config.referenzraeume and "room_actual" not in rt.manifest.refs:
        return
    now = wallclock.now()
    schutz = _kaelteschutz_entscheiden(rt, now, None)
    _boosts_setzen(rt, schutz, now, comfort=False, notfall=False)


class _Schutz:
    """Ergebnis der Kaelteschutz-Pruefung eines Checks: die Entscheidung, ob vorher ein Lauf bestand und der Zaehlzustand
    vor der Pruefung (fuer einen abgelehnten Start)."""

    def __init__(self, entscheidung: KaelteschutzEntscheidung, lief: bool, vorher: KaelteschutzZustand) -> None:
        self.entscheidung = entscheidung
        self.lief = lief
        self.vorher = vorher

    @property
    def lauf(self) -> bool:
        return self.entscheidung.art != "aus"

    @property
    def beendet(self) -> bool:
        return self.lief and not self.lauf


def _kaelteschutz_lesen(rt: Runtime, jetzt: datetime) -> tuple[dict[int, Messwert], dict[int, float]]:
    """Das Ist je Raum des Kreises (Referenz- und Kontrollraeume) und der Versatz der Referenzraeume. Ein nicht lesbarer
    Raum ist `Messwert(None, None)`; ein Lesefehler wirft nie (aus einem Fehler loest der Kaelteschutz nichts aus, der
    lokale Check zaehlt ihn ueber `wirksames_raum_ist`). Die Signalquelle liefert keinen Messzeitpunkt: ein gelesener
    Wert gilt als eben gemessen."""
    referenz = dict(rt.config.referenzraeume)
    refs = dict(rt.manifest.raum_refs)
    if not referenz:
        refs[_KREISSENSOR] = rt.manifest.refs["room_actual"]
        referenz = {_KREISSENSOR: 0.0}
    ist = {}
    for raum, ref in refs.items():
        try:
            wert = rt.signals.get_state(ref)
        except Exception as error:
            logger.debug("Kaelteschutz: Raum %d nicht lesbar: %s", raum, lesbar(str(error)))
            ist[raum] = Messwert(None, None)
        else:
            ist[raum] = Messwert(wert, jetzt)
    return ist, referenz


def _kaelteschutz_entscheiden(rt: Runtime, now: datetime, soll: float | None) -> _Schutz:
    """Fragt die reine Entscheidung (`kaelteschutz_entscheiden`) mit dem gelesenen Ist, dem Soll (None = kein Soll), der
    Abweichung der Referenzraeume (nur gueltige Werte) und dem Zustand aus dem Speicher des Kreises. Gerechnet wird in
    UTC, damit eine Zeitumstellung keine Spanne verfaelscht. Aendert den Zustand nicht (das tut `_kaelteschutz_buchen`)."""
    state = rt.store.state
    jetzt = now.astimezone(UTC)
    seit = parse_since(state.kaelteschutz_seit)
    seit_utc = seit.astimezone(UTC) if seit is not None else None
    lief = state.kaelteschutz_art is not None and seit_utc is not None
    vorher = state.kaelteschutz_zaehlung
    zustand = replace(vorher, lauf_art=state.kaelteschutz_art if lief else None, lauf_seit=seit_utc if lief else None)
    ist, referenz = _kaelteschutz_lesen(rt, jetzt)
    delta = None
    if soll is not None:
        gueltig = gueltige_ist(jetzt, ist)
        delta = raum_delta.delta(soll, {raum: wert for raum, wert in gueltig.items() if raum in referenz}, referenz)
    entscheidung = kaelteschutz_entscheiden(
        jetzt, ist=ist, soll=soll, delta=delta, zustand=zustand,
        arrival_threshold_k=rt.override.safety.arrival_threshold_k,
    )
    return _Schutz(entscheidung, lief, vorher)


def _boosts_setzen(
    rt: Runtime, schutz: _Schutz, now: datetime, *, comfort: bool, notfall: bool,
) -> tuple[bool, bool]:
    """`set_boosts` mit dem Kaelteschutz: ein aktiver Lauf ist ein Notfall-Boost (neben dem des Notbetriebs); danach
    bucht `_kaelteschutz_buchen` den Lauf."""
    ergebnis = rt.override.set_boosts(comfort=comfort, emergency=notfall or schutz.lauf, kaelteschutz=schutz.lauf)
    _kaelteschutz_buchen(rt, schutz, now, ergebnis[1])
    return ergebnis


def _kaelteschutz_allein(rt: Runtime, schutz: _Schutz, now: datetime) -> None:
    """Der Kaelteschutz ohne Soll oder ohne lesbaren Referenzraum: nur ein Start oder ein Ende ruehrt die Boosts an,
    Comfort-Boost und ein Notfall-Boost des Notbetriebs bleiben, wie sie sind (ohne Soll-Vergleich gibt es keinen
    neuen Entscheid)."""
    state = rt.store.state
    if schutz.entscheidung.gestartet or schutz.beendet:
        behalten = state.delivery.notbetrieb and state.emergency_boost_active
        _boosts_setzen(rt, schutz, now, comfort=state.boost_active, notfall=behalten)
    else:
        _kaelteschutz_buchen(rt, schutz, now, state.emergency_boost_active)


def _kaelteschutz_buchen(rt: Runtime, schutz: _Schutz, now: datetime, notfall_laeuft: bool) -> None:
    """Merkt den Lauf und den Zaehlzustand nach `set_boosts`. Ein Start, den `set_boosts` ablehnte (Start-Zaehler,
    Wiederherstellungspunkt, Schreibbudget), wird nicht gebucht: der Zaehlzustand bleibt bei den Ausloesern, der naechste
    Check versucht den Start erneut. Ein Ende loescht den Lauf; ein laufender Lauf aendert nichts."""
    entscheidung = schutz.entscheidung
    if entscheidung.gestartet:
        if not notfall_laeuft:
            rt.store.update(kaelteschutz_zaehlung=replace(schutz.vorher, zuletzt=entscheidung.zustand.zuletzt))
            return
        logger.warning("Kaelteschutz startet (%s)", entscheidung.grund)
        rt.store.update(
            kaelteschutz_art=entscheidung.art, kaelteschutz_seit=now.isoformat(),
            kaelteschutz_zaehlung=KaelteschutzZustand(),
        )
    elif not schutz.lauf:
        if schutz.lief:
            logger.info("Kaelteschutz endet (%s)", entscheidung.grund)
        rt.store.update(
            kaelteschutz_art=None, kaelteschutz_seit=None, kaelteschutz_zaehlung=entscheidung.zustand,
        )


def _track_boost_since(rt: Runtime, now: datetime, was_active: bool) -> None:
    """Beginn des Comfort-Boosts merken bzw. loeschen. Startet der Boost jetzt (vorher inaktiv), gilt `now` und ein
    uebrig gebliebener alter Zeitstempel wird ueberschrieben; ein laufender Boost behaelt seinen Zeitstempel, ein
    vor dem Update persistierter (ohne oder mit unlesbarem Zeitstempel) laeuft ab jetzt. update() statt
    update_saved(): bei einem Schreibfehler bleibt der Wert im Speicher, die 4-h-Grenze greift trotzdem. Dazu der
    Stand der monotonen Uhr beim Start in diesem Prozess; ein Boost aus einem frueheren Lauf hat ihn nicht."""
    state = rt.store.state
    if not state.boost_active:
        since = None
        rt.boost_mono_start = None
    elif not was_active or parse_since(state.boost_since) is None:
        since = now.isoformat()
        if not was_active:
            rt.boost_mono_start = rt.clock()
    else:
        since = state.boost_since
    if since != state.boost_since:
        rt.store.update(boost_since=since)


def _count_unreadable_room(rt: Runtime) -> None:
    """Nach UNREADABLE_ROOM_CHECKS unlesbaren Raumwerten in Folge endet ein laufender Comfort-Boost
    (Rueckkehr auf den Wiederherstellungspunkt); der Notfall-Boost bleibt, wie er ist."""
    misses = rt.store.state.room_actual_misses + 1
    rt.store.update(room_actual_misses=misses)
    if misses >= UNREADABLE_ROOM_CHECKS and rt.store.state.boost_active:
        logger.warning("Raumfuehler %d-mal in Folge nicht lesbar, Comfort-Boost endet", misses)
        rt.override.set_boosts(comfort=False, emergency=rt.store.state.emergency_boost_active)
        _track_boost_since(rt, wallclock.now(), was_active=True)


def claim_due_tick(rt: Runtime, now: datetime) -> str | None:
    """Neuer Tick, wenn daily_trigger_time heute erstmals erreicht ist ("daily") oder sich
    room_target seit dem letzten Tick geaendert hat ("target_change"). Faellt beides auf denselben
    Check, geht der Tick als "daily" raus: der Server lernt nur auf "daily" und wendet die
    Vorsteuerung auf jeden Tick an, sonst fiele der Lernschritt des Tages aus. Gebucht wird beim
    Entstehen, nicht beim Publish: ein zurueckgehaltener Tick erzeugt keine Duplikate, seine
    Wiederholungen laufen ueber die Zustellung. Ohne Stable-Target-Cache (Thermostat beim Start
    nicht lesbar) gibt es nur einen faelligen Tagestick; dessen Versuch liest live und meldet den
    Fuehler als Datenfehler."""
    state = rt.store.state
    if state.abo_inactive_since is not None:
        return None
    room_target = state.stable_target
    today = now.date().isoformat()
    daily_trigger_time = rt.config.daily_trigger_time
    daily_due = False
    if daily_trigger_time:
        trigger_time = datetime.strptime(daily_trigger_time, "%H:%M").time()
        daily_due = now.time() >= trigger_time and state.last_daily_trigger_date != today
    if room_target is None:
        if not daily_due:
            return None
        rt.store.update_saved(last_daily_trigger_date=today)
        return "daily"
    target_changed = room_target != state.last_published_target_rt
    if not (daily_due or target_changed):
        return None
    changes: dict[str, object] = {"last_published_target_rt": room_target}
    if daily_due:
        changes["last_daily_trigger_date"] = today
    # Wie 0.16.0: ohne gespeicherte Buchung kein Tick; der naechste Check beansprucht ihn erneut.
    rt.store.update_saved(**changes)
    return "daily" if daily_due else "target_change"
