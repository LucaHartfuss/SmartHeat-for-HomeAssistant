"""Durchsetzung (TP11, Spec 5.3)."""
import dataclasses
import logging
from datetime import date, datetime

import pytest
from bindung_beispiele import VAILLANT_MYPYLLANT
from fakes import FREMDTEXT, FREMDTEXT_IM_LOG, FakeNotifySink, logzeilen, runtime_config

from heizungsbruecke.ha_binding import HaPlantBinding
from smartheat_core import enforce, wallclock, write_budget
from smartheat_core.pipeline import LeverPipeline
from smartheat_core.safety import LocalSafety
from smartheat_core.zahlen import ist_endliche_zahl
from smartheat_runtime.delivery import SOURCE_LOCAL, DataFault, DeliveryState
from smartheat_runtime.notifier import Notifier
from smartheat_runtime.roles import ChannelManifest
from smartheat_runtime.runtime import Runtime
from smartheat_runtime.worker import RegulationWorker

SETTLE = VAILLANT_MYPYLLANT.settle_seconds
SAFETY = LocalSafety(
    ranges={"curve": (0.4, 1.5), "room_setpoint": (15.0, 25.0), "heat_limit": (5.0, 20.0), "min_flow": (20.0, 30.0)},
    # Comfort-Boost-Zeile bewusst ungleich der Notfall-Zeile (Maxima curve/room_setpoint), damit die Tests
    # die beiden Zeilen unterscheiden.
    comfort_boost={"curve": 1.2, "room_setpoint": 24.0, "heat_limit": 20.0},
    emergency_boost_levers=("curve", "room_setpoint", "heat_limit"),
    arrival_threshold_k=0.5,
)
ROWS = {"boost_active": (1.2, 24.0), "emergency_boost_active": (1.5, 25.0)}
MANIFEST = ChannelManifest(refs={
    "curve_current": "number.curve", "shift_current": "climate.zone::temperature", "min_flow": "number.mf",
})
POINT = {"curve": 0.9, "room_setpoint": 21.0}
TODAY = date(2026, 10, 3)
_ECHTES_HEUTE = enforce._today  # vor der autouse-Fixture _fixed_day gemerkt


class Ha:
    def __init__(self, curve=0.9, shift=21.0, min_flow=20.5, mode="heat_cool", reflects_writes=True):
        self.states = {"number.curve": curve, "climate.zone::temperature": shift, "number.mf": min_flow,
                       "climate.zone": mode}
        self.writes = []
        # Schreibversuche, auch gescheiterte: (entity_id, Wert, Uhrzeit der Test-Uhr oder None).
        self.attempts = []
        self.clock = None
        # Entity-IDs, deren Schreiben scheitert (z. B. myVAILLANT 403 "Quota Exceeded").
        self.failing = set()
        # False: mypyllant uebernimmt das Rueckschreiben nicht (oder jemand stellt sofort wieder
        # um) -- HA zeigt weiter den verstellten Wert.
        self.reflects_writes = reflects_writes

    def _value(self, key):
        value = self.states[key]
        if isinstance(value, Exception):
            raise value
        return value

    def get_state(self, ref):
        return self._value(ref)

    def get_raw_state(self, entity_id):
        return self._value(entity_id)

    def _attempt(self, entity_id, value):
        self.attempts.append((entity_id, value, self.clock() if self.clock else None))
        if entity_id in self.failing:
            raise RuntimeError("403 Quota Exceeded")

    def _set(self, key, value):
        if self.reflects_writes:
            self.states[key] = value

    def set_number_value(self, entity_id, value):
        self._attempt(entity_id, value)
        self.writes.append((entity_id, value))
        self._set(entity_id, value)

    def set_climate_temperature(self, entity_id, value):
        self._attempt(entity_id, value)
        self.writes.append((entity_id, value))
        self._set(f"{entity_id}::temperature", value)

    def set_hvac_mode(self, entity_id, mode):
        self._attempt(entity_id, mode)
        self.writes.append((entity_id, mode))
        self._set(entity_id, mode)


@pytest.fixture(autouse=True)
def _fixed_day(monkeypatch):
    # Kein Flackern um Mitternacht: der Tageszaehler haengt an _today().
    monkeypatch.setattr(enforce, "_today", lambda: TODAY)


def _rt(make_store, clock, ha, uptime=SETTLE + 1, store=None, manifest=MANIFEST, description=VAILLANT_MYPYLLANT,
        **point):
    """Runtime mit `uptime` Sekunden Laufzeit seit dem Start (Default: Schonfrist nach dem Start
    vorbei, HA gilt ohne eigenes Schreiben als eingeschwungen). `store` wiederverwendet einen
    vorhandenen StateStore (Neustart-Simulation: derselbe backup.json-Pfad, frisch eingelesen)
    statt den Wiederherstellungspunkt POINT (geaendert um `point`, None = fehlt) neu zu schreiben. `description`:
    Bindungsbeschreibung (Default Vaillant).
    Echte Teile (A5-25): Notifier mit Fake-Sink (`rt.kunde`: was beim Kunden ankommt), Worker, Pipeline."""
    if store is None:
        restore_point = {lever: value for lever, value in {**POINT, **point}.items() if value is not None}
        store = make_store(backup={"restore_point": restore_point})
    store.update(stable_target=20.5)
    kunde = FakeNotifySink()
    override = LeverPipeline(store, HaPlantBinding(ha, manifest, description), SAFETY, clock=clock)
    ha.clock = clock
    clock.advance(uptime)
    rt = Runtime(
        manifest=manifest, signals=ha, config=runtime_config(), worker=RegulationWorker(clock), store=store,
        override=override, notifier=Notifier(store, kunde), neustart=lambda: None, clock=clock,
    )
    rt.kunde = kunde  # type: ignore[attr-defined]  # nur Test: Sicht auf den Sink
    rt.zone_prepared = True  # die Zone war schon vorbereitet; ohne das setzt die Durchsetzung die Betriebsart nicht durch
    return rt


MANIFEST_G = ChannelManifest(refs={**MANIFEST.refs, "heat_limit": "number.hl"})


def _rt_g(make_store, clock, live_heat_limit):
    ha = Ha()
    ha.states["number.hl"] = live_heat_limit
    return _rt(make_store, clock, ha, manifest=MANIFEST_G, heat_limit=15.0)


def _rounds(rt, n=enforce.DETECTION_ROUNDS):
    for _ in range(n):
        enforce.check_manual_override(rt)


def _curve_writes(rt):
    return [w for w in rt.signals.writes if w[0] == "number.curve"]


# Kundentexte der Meldung `manueller_eingriff` (enforce.MESSAGE, LIMIT_MESSAGE) als Literal.
def _meldung(was):
    return (f"SmartHeat: Die Heizungseinstellung wurde in der App verstellt ({was}) und zurückgesetzt. "
            "Einstellungen bitte nur über das Raumthermostat.")


def _limit_meldung(was):
    return (f"SmartHeat: Die Heizungseinstellung wird immer wieder verstellt ({was}). SmartHeat setzt sie heute "
            "nicht mehr zurück, um das Abrufkontingent der Hersteller-Cloud zu schonen, und versucht es morgen erneut.")


def _pushes(rt):
    """Push-Texte des Schluessels manueller_eingriff, die beim Kunden ankommen (Fake-Sink des echten Notifiers)."""
    return [text for key, text in rt.kunde.pushes if key == "manueller_eingriff"]


def _limit_pushes(rt):
    return [text for text in _pushes(rt) if text.startswith("SmartHeat: Die Heizungseinstellung wird immer wieder")]


def _message_pushes(rt):
    return [text for text in _pushes(rt) if text.startswith("SmartHeat: Die Heizungseinstellung wurde in der App")]


def _meldezustand(rt):
    """Zustand des Meldeschluessels im Notifier (backup.json); \"ok\" = keine offene Meldung."""
    return rt.notifier.state("manueller_eingriff")


def test_no_deviation_no_write(make_store, clock):
    rt = _rt(make_store, clock, Ha())
    _rounds(rt)
    assert rt.signals.writes == []


@pytest.mark.parametrize("huge", [10**400, -10**400], ids=["gross-positiv", "gross-negativ"])
def test_integer_beyond_float_range_from_ha_is_unreadable_not_an_error(make_store, clock, huge):
    # Die echten Adapter liefern keine so grosse Ganzzahl mehr (get_state wirft ValueError); der Test haelt die
    # Invariante der Pruefung selbst fest.
    rt = _rt(make_store, clock, Ha(curve=huge))
    _rounds(rt)
    assert rt.signals.writes == []


def test_manual_curve_change_is_written_back_and_reported(make_store, clock):
    rt = _rt(make_store, clock, Ha(curve=1.3))
    _rounds(rt)
    assert rt.signals.writes == [("number.curve", 0.9)]
    assert rt.store.state.manual_override_pending["levers"]["curve"] == 1.3
    assert rt.kunde.pushes == [("manueller_eingriff", _meldung("Heizkurve"))]


def test_heat_limit_changed_in_the_app_is_written_back_and_reported(make_store, clock):
    rt = _rt_g(make_store, clock, 16.0)
    _rounds(rt)
    assert ("number.hl", 15.0) in rt.signals.writes
    assert rt.store.state.manual_override_pending is not None
    assert _pushes(rt) == [_meldung("Heizgrenze")]


def test_heat_limit_within_half_a_step_is_no_deviation(make_store, clock):
    rt = _rt_g(make_store, clock, 15.04)
    _rounds(rt)
    assert rt.signals.writes == []


def test_detection_needs_two_rounds(make_store, clock):
    rt = _rt(make_store, clock, Ha(curve=1.3))
    _rounds(rt, 1)
    assert rt.signals.writes == []


def test_zone_mode_is_restored_and_shift_rewritten(make_store, clock):
    rt = _rt(make_store, clock, Ha(mode="auto"))
    _rounds(rt)
    assert ("climate.zone", "heat_cool") in rt.signals.writes
    assert ("climate.zone", 21.0) in rt.signals.writes


def test_zone_mode_restore_switches_the_mode_exactly_once(make_store, clock):
    # Ruling #3: write_levers schaltet die Zone selbst um, kein zweites set_hvac_mode davor.
    # Zone und Wert verstellt, HA hinkt nach: Betriebsart und Parallelverschiebung sind EIN
    # Rueckschreiben (sonst schaltete der zweite Schreibvorgang auf dem veralteten "auto" erneut).
    rt = _rt(make_store, clock, Ha(mode="auto", shift=18.0, reflects_writes=False))
    _rounds(rt)
    assert [w for w in rt.signals.writes if w[1] == "heat_cool"] == [("climate.zone", "heat_cool")]
    assert [w for w in rt.signals.writes if w[1] == 21.0] == [("climate.zone", 21.0)]


def test_zone_mode_restore_does_not_switch_again_while_ha_lags(make_store, clock):
    # HA kann nach dem Umschalten bis zum naechsten Poll noch "auto" zeigen (mypyllant): keine weitere Umschaltung
    # in dieser Zeit, und der Hinweis bleibt stehen (kein "wieder in Ordnung").
    rt = _rt(make_store, clock, Ha(mode="auto", reflects_writes=False))
    _rounds(rt)
    for _ in range(5):
        clock.advance(300)
        _rounds(rt)
    assert [w for w in rt.signals.writes if w[1] == "heat_cool"] == [("climate.zone", "heat_cool")]
    assert rt.store.state.manual_override is not None
    # kein "wieder in Ordnung" dazwischen: eine Meldung, der Meldezustand steht weiter auf dem Eingriff
    assert _message_pushes(rt) == [_meldung("Betriebsart der Zone")]
    assert _meldezustand(rt) == rt.store.state.manual_override["signatur"]


def test_min_flow_deviation_is_written_back(make_store, clock):
    rt = _rt(make_store, clock, Ha(min_flow=25.0))
    _rounds(rt)
    assert rt.signals.writes == [("number.mf", 20.5)]


def test_a_zone_that_was_never_prepared_is_no_zone_mode_deviation(make_store, clock):
    """Fix-Runde 1 (I-1): vor der ersten erfolgreichen Vorbereitung gehoert die Betriebsart dem Start und dem lokalen
    Check (Kontingent `zone_prepare`); die Durchsetzung schaltet nicht um, zaehlt nicht und meldet nichts."""
    rt = _rt(make_store, clock, Ha(mode="auto"))
    rt.zone_prepared = False
    _rounds(rt, 4)
    assert rt.signals.writes == [] and rt.signals.attempts == []
    assert rt.store.state.manual_override is None and _pushes(rt) == []
    rt.zone_prepared = True  # Gegenprobe: nach der Vorbereitung wird dieselbe Abweichung durchgesetzt
    _rounds(rt)
    assert ("climate.zone", "heat_cool") in rt.signals.writes


def test_zone_zero_is_no_deviation(make_store, clock):
    rt = _rt(make_store, clock, Ha(shift=0.0))
    _rounds(rt)
    assert rt.signals.writes == []
    assert rt.store.state.manual_override is None


def test_record_with_an_inactive_zone_and_no_restore_point_survives_a_reload(make_store, clock):
    # Carried Task-13-Befund: ohne Live-Wert (Zone inaktiv) UND ohne gespeicherten
    # Wiederherstellungspunkt fuer die Parallelverschiebung liefert der KPI dafuer None. Ein
    # persistierter Eintrag mit room_setpoint=None faellt bei state._is_override durch -- gemeldet/rollen/
    # signatur gehen nach einem Neustart verloren, derselbe Eingriff wird erneut gemeldet.
    # reflects_writes=False haelt die Abweichung ueber den Neustart hinweg bestehen (mypyllant
    # zeigt einen eigenen Schreibvorgang wie hier erst mit Verzoegerung).
    ha = Ha(curve=1.3, shift=0.0, reflects_writes=False)
    rt = _rt(make_store, clock, ha, room_setpoint=None)
    _rounds(rt)
    override = rt.store.state.manual_override
    assert override is not None
    assert ist_endliche_zahl(override["levers"]["curve"]) and ist_endliche_zahl(override["levers"]["room_setpoint"])
    assert override["gemeldet"] == override["signatur"]

    # Neustart: derselbe backup.json-Pfad, frisch eingelesen.
    reloaded = make_store()
    assert reloaded.state.manual_override == override  # state._is_override akzeptiert den Eintrag

    rt2 = _rt(make_store, clock, ha, store=reloaded)
    _rounds(rt2)

    assert _message_pushes(rt2) == []  # derselbe Eingriff wird nicht erneut gemeldet


def test_pause_after_own_write(make_store, clock):
    rt = _rt(make_store, clock, Ha(curve=1.3))
    rt.override.write_levers(("curve",))  # eigener Schreibvorgang
    rt.signals.states["number.curve"] = 1.3  # HA zeigt noch den alten Wert
    rt.signals.writes.clear()
    _rounds(rt)
    assert rt.signals.writes == []


def test_after_a_restart_enforcement_waits_for_the_settle_window(make_store, clock):
    # Ein eigenes Schreiben kurz vor dem Neustart ist unbekannt, HA kann noch den alten Wert zeigen:
    # erst nach SETTLE Laufzeit durchsetzen (dieselbe Regel wie der Quota-Check).
    rt = _rt(make_store, clock, Ha(curve=1.3), uptime=0)
    _rounds(rt)
    clock.advance(SETTLE - 60)
    _rounds(rt)
    assert rt.signals.writes == []
    assert rt.kunde.pushes == [] and _meldezustand(rt) == "ok"
    assert rt.store.state.manual_override is None
    clock.advance(61)
    _rounds(rt)
    assert rt.signals.writes == [("number.curve", 0.9)]
    assert _message_pushes(rt) == [_meldung("Heizkurve")]


def test_enforcement_rate_limited_per_day(make_store, clock, monkeypatch):
    # Review Focus 2: mypyllant uebernimmt das Rueckschreiben nicht -- hoechstens
    # MAX_PER_DAY Schreibvorgaenge, genau eine Eingriffs- und eine Limit-Meldung.
    rt = _rt(make_store, clock, Ha(curve=1.3, reflects_writes=False))
    for _ in range(20):
        clock.advance(SETTLE + 1)
        _rounds(rt)
    assert len(_curve_writes(rt)) == 6
    # genau eine Eingriffs- und eine Limit-Meldung, dazwischen kein "wieder in Ordnung"
    assert _pushes(rt) == [_meldung("Heizkurve"), _limit_meldung("Heizkurve")]
    assert _meldezustand(rt) == "limit:2026-10-03:Heizkurve"
    # Am naechsten Tag wird derselbe, nie zurueckgekehrte Eingriff wieder zurueckgesetzt, aber nicht neu gemeldet.
    monkeypatch.setattr(enforce, "_today", lambda: date(2026, 10, 4))
    clock.advance(SETTLE + 1)
    _rounds(rt)
    assert len(_curve_writes(rt)) == 7
    assert _pushes(rt) == [_meldung("Heizkurve"), _limit_meldung("Heizkurve")]


def test_ignored_write_is_reported_once_until_the_return(make_store, clock):
    # Plan-Praezisierung "Durchsetzungs-Meldung": die Meldung bleibt stehen, bis der Istwert
    # wieder stimmt; erst danach ist ein neuer Eingriff eine neue Meldung.
    rt = _rt(make_store, clock, Ha(curve=1.3, reflects_writes=False))
    _rounds(rt)
    for _ in range(3):
        clock.advance(SETTLE + 1)
        _rounds(rt)
    assert _pushes(rt) == [_meldung("Heizkurve")]
    assert _meldezustand(rt) == "curve=1.3"  # die Meldung bleibt stehen
    rt.signals.states["number.curve"] = 0.9  # die Anlage uebernimmt den Wert endlich
    clock.advance(SETTLE + 1)
    _rounds(rt)
    assert rt.store.state.manual_override is None
    assert _meldezustand(rt) == "ok"
    assert _pushes(rt) == [_meldung("Heizkurve")]  # die Entwarnung ist still


def test_retry_interval_between_writes(make_store, clock):
    rt = _rt(make_store, clock, Ha(curve=1.3))
    _rounds(rt)
    rt.signals.states["number.curve"] = 1.3
    clock.advance(SETTLE + 1)  # 35 min > Poll-Intervall 30 min
    _rounds(rt)
    assert len(_curve_writes(rt)) == 2


@pytest.mark.parametrize(("ago", "allowed"), [(enforce.RETRY_SECONDS - 1, False),
                                              (enforce.RETRY_SECONDS + 1, True)])
def test_may_write_respects_the_retry_interval(make_store, clock, ago, allowed):
    # Ruling #9: RETRY_SECONDS direkt pruefen (im Ablauf wird es von der Schonfrist verdeckt).
    rt = _rt(make_store, clock, Ha())
    rt.store.update(write_budget={"enforce:curve": {"day": TODAY.isoformat(), "count": 1, "last": clock() - ago}})
    assert enforce._may_write(rt, "curve") is allowed


def test_may_write_respects_the_daily_limit(make_store, clock):
    rt = _rt(make_store, clock, Ha())
    day = TODAY.isoformat()
    rt.store.update(write_budget={"enforce:curve": {"day": day, "count": write_budget.MAX_PER_DAY,
                                                    "last": clock() - 10 * enforce.RETRY_SECONDS}})
    assert enforce._may_write(rt, "curve") is False
    assert enforce._may_write(rt, "room_setpoint") is True


def test_open_data_fault_pauses(make_store, clock):
    rt = _rt(make_store, clock, Ha(curve=1.3))
    rt.store.set_delivery(DeliveryState(datenfehler=DataFault(SOURCE_LOCAL, ("room_actual",))))
    _rounds(rt)
    assert rt.signals.writes == []


def test_return_clears_hint(make_store, clock):
    rt = _rt(make_store, clock, Ha(curve=1.3))
    _rounds(rt)
    assert _meldezustand(rt) == "curve=1.3"
    enforce.check_manual_override(rt)  # Anlage steht wieder richtig
    assert rt.store.state.manual_override is None
    assert _meldezustand(rt) == "ok"


def test_counter_resets_next_day(make_store, clock, monkeypatch):
    rt = _rt(make_store, clock, Ha(curve=1.3))
    monkeypatch.setattr(enforce, "_today", lambda: date(2026, 10, 3))
    for _ in range(10):
        rt.signals.states["number.curve"] = 1.3
        clock.advance(SETTLE + 1)
        _rounds(rt)
    assert len(_curve_writes(rt)) == write_budget.MAX_PER_DAY
    monkeypatch.setattr(enforce, "_today", lambda: date(2026, 10, 4))
    rt.signals.states["number.curve"] = 1.3
    clock.advance(SETTLE + 1)
    before = len(rt.signals.writes)
    _rounds(rt)
    assert len(rt.signals.writes) == before + 1


@pytest.mark.parametrize("flag", ["boost_active", "emergency_boost_active"])
def test_boost_row_is_the_target_during_boost(make_store, clock, flag):
    curve, shift = ROWS[flag]
    rt = _rt(make_store, clock, Ha(curve=curve, shift=shift))
    rt.store.update(**{flag: True})
    _rounds(rt)
    assert rt.signals.writes == []


@pytest.mark.parametrize("flag", ["boost_active", "emergency_boost_active"])
def test_restore_point_on_the_plant_during_a_boost_is_written_back_to_the_boost_row(make_store, clock, flag):
    rt = _rt(make_store, clock, Ha(curve=0.9, shift=21.0))
    rt.store.update(**{flag: True})
    _rounds(rt)
    curve, shift = ROWS[flag]
    assert rt.signals.writes == [("number.curve", curve), ("climate.zone", shift)]


def test_unreadable_zone_mode_is_no_deviation(make_store, clock):
    rt = _rt(make_store, clock, Ha(mode=RuntimeError("unavailable")))
    _rounds(rt)
    assert rt.signals.attempts == []
    assert rt.store.state.manual_override is None


def test_failed_write_backs_respect_the_quota(make_store, clock):
    # IMPORTANT 1: myVAILLANT lehnt ab (403) -- jeder VERSUCH zaehlt: hoechstens einer pro
    # RETRY_SECONDS und MAX_PER_DAY am Tag; kein "zurueckgesetzt", genau eine Limit-Meldung.
    ha = Ha(curve=1.3)
    ha.failing.add("number.curve")
    rt = _rt(make_store, clock, ha)
    for _ in range(12 * 24):  # ein Tag im 5-min-Takt
        clock.advance(300)
        enforce.check_manual_override(rt)
    times = [at for entity, _, at in ha.attempts if entity == "number.curve"]
    assert len(times) == write_budget.MAX_PER_DAY
    assert all(later - earlier >= enforce.RETRY_SECONDS for earlier, later in zip(times, times[1:], strict=False))
    assert _pushes(rt) == [_limit_meldung("Heizkurve")]  # kein "zurueckgesetzt", genau eine Limit-Meldung
    assert rt.store.state.manual_override_pending["levers"]["curve"] == 1.3


def test_partial_write_back_is_one_intervention(make_store, clock):
    # IMPORTANT 2: Kurve zurueckgeschrieben, Parallelverschiebung scheitert. Danach steht die Kurve
    # (HA hinkt nach) offen, die Parallelverschiebung weicht weiter ab: kein zweiter Push, der KPI
    # behaelt die Werte des Kunden; auch das spaetere erfolgreiche Rueckschreiben meldet nicht neu.
    ha = Ha(curve=1.3, shift=23.0, reflects_writes=False)
    ha.failing.add("climate.zone")
    rt = _rt(make_store, clock, ha)
    _rounds(rt)
    first = rt.store.state.manual_override_pending
    for _ in range(12):
        _rounds(rt)
        clock.advance(300)
    ha.failing.clear()
    for _ in range(12):
        _rounds(rt)
        clock.advance(300)
    assert ("climate.zone", 21.0) in ha.writes
    assert _message_pushes(rt) == [_meldung("Heizkurve")]
    pending = rt.store.state.manual_override_pending
    assert pending["levers"] == {"curve": 1.3, "room_setpoint": 23.0}
    assert pending is first  # derselbe Eingriff: KPI nicht neu geschrieben (kein erneutes Senden)
    assert rt.store.state.manual_override["levers"] == {"curve": 1.3, "room_setpoint": 23.0}


def test_pending_kpi_is_merged_not_replaced(make_store, clock):
    # Ein noch nicht gesendeter Eingriff (Kurve 1.3) bleibt im KPI, wenn danach -- nach der
    # Rueckkehr -- ein zweiter Eingriff nur an der Parallelverschiebung folgt.
    rt = _rt(make_store, clock, Ha(curve=1.3))
    _rounds(rt)
    enforce.check_manual_override(rt)  # Rueckkehr
    assert rt.store.state.manual_override is None
    rt.signals.states["climate.zone::temperature"] = 23.0
    clock.advance(SETTLE + 1)
    _rounds(rt)
    pending = rt.store.state.manual_override_pending
    assert pending["levers"] == {"curve": 1.3, "room_setpoint": 23.0}


def test_detection_at_the_daily_limit_only_sends_the_limit_message(make_store, clock):
    # MINOR 1: der 7. Eingriff am Tag wird nicht mehr zurueckgesetzt -- nur die Limit-Meldung,
    # kein "... und zurueckgesetzt".
    rt = _rt(make_store, clock, Ha(curve=1.3))
    for _ in range(write_budget.MAX_PER_DAY):
        rt.signals.states["number.curve"] = 1.3
        clock.advance(SETTLE + 1)
        _rounds(rt)
        enforce.check_manual_override(rt)  # Rueckkehr
    assert len(_curve_writes(rt)) == 6
    assert _pushes(rt) == [_meldung("Heizkurve")] * 6  # je Eingriff eine Meldung, jeder ist zurueckgekehrt
    rt.signals.states["number.curve"] = 1.3
    clock.advance(SETTLE + 1)
    _rounds(rt)
    assert _pushes(rt)[6:] == [_limit_meldung("Heizkurve")]
    assert rt.store.state.manual_override is not None


def test_zone_mode_without_shift_target_switches_the_zone_only(make_store, clock):
    # MINOR 2: kein Wiederherstellungspunkt fuer die Parallelverschiebung -- nur die Zone auf
    # Manuell stellen, keinen Sollwert schreiben.
    rt = _rt(make_store, clock, Ha(mode="auto"), room_setpoint=None)
    _rounds(rt)
    assert rt.signals.writes == [("climate.zone", "heat_cool")]
    assert _message_pushes(rt) == [_meldung("Betriebsart der Zone")]


def test_curve_tolerance_is_half_a_plant_step(make_store, clock):
    # MINOR 4: eine erkannte Abweichung fuehrt immer zu einem echten Schreibvorgang (der Quota-Check
    # ueberspringt erst innerhalb eines halben Schritts).
    assert VAILLANT_MYPYLLANT.enforce_tolerance["curve"] == pytest.approx(0.025)
    rt = _rt(make_store, clock, Ha(curve=0.92))
    _rounds(rt)
    assert rt.signals.writes == []
    assert rt.store.state.manual_override is None


def test_without_room_target_the_min_flow_is_skipped_and_the_rest_enforced(make_store, clock):
    """A5-24 (a): beim ersten Start ohne Raum-Soll liefert derived.expected None."""
    ha = Ha(curve=1.3, min_flow=25.0)
    rt = _rt(make_store, clock, ha)
    rt.store.update(stable_target=None)
    _rounds(rt)
    assert ("number.curve", 0.9) in ha.writes and not [w for w in ha.writes if w[0] == "number.mf"]


def test_a_second_change_during_an_open_intervention_extends_it(make_store, clock):
    """A5-24 (b): neuer Hebel waehrend eines noch nicht zurueckgekehrten Eingriffs -> neue Signatur, neue Meldung,
    KPI zusammengefuehrt."""
    ha = Ha(curve=1.2, reflects_writes=False)
    rt = _rt(make_store, clock, ha)
    _rounds(rt)
    assert rt.store.state.manual_override["signatur"] == "curve=1.2"
    ha.states["climate.zone::temperature"] = 22.0
    clock.advance(SETTLE + 1)
    _rounds(rt)
    assert rt.store.state.manual_override["signatur"] == "curve=1.2,room_setpoint=22"
    assert _message_pushes(rt) == [_meldung("Heizkurve"), _meldung("Heizkurve, Wunschtemperatur der Zone")]
    assert _meldezustand(rt) == "curve=1.2,room_setpoint=22"
    pending = rt.store.state.manual_override_pending["levers"]
    assert pending["curve"] == 1.2 and pending["room_setpoint"] == 22.0


def test_after_a_data_fault_detection_needs_two_rounds_again(make_store, clock):
    """A5-24 (c)."""
    ha = Ha(curve=1.3)
    rt = _rt(make_store, clock, ha)
    enforce.check_manual_override(rt)  # Runde 1 von 2
    rt.store.set_delivery(DeliveryState(datenfehler=DataFault(SOURCE_LOCAL, ("room_actual",))))
    enforce.check_manual_override(rt)  # pausiert, setzt zurueck
    rt.store.set_delivery(DeliveryState())
    enforce.check_manual_override(rt)
    assert _curve_writes(rt) == []
    enforce.check_manual_override(rt)
    assert _curve_writes(rt) == [("number.curve", 0.9)]


@pytest.mark.parametrize(("mode", "shift", "was"), [
    ("auto", 22.0, "Wunschtemperatur der Zone, Betriebsart der Zone"),
    ("auto", 21.0, "Betriebsart der Zone"),
])
def test_message_names_mode_and_setpoint_correctly(make_store, clock, mode, shift, was):
    """A5-24 (d), Mutanten 71-73."""
    ha = Ha(mode=mode, shift=shift)
    rt = _rt(make_store, clock, ha)
    _rounds(rt)
    assert _message_pushes(rt) == [_meldung(was)]


def test_limit_message_is_not_critical(make_store, clock):
    """A5-24 (d), Mutant 123."""
    ha = Ha(curve=1.3)
    rt = _rt(make_store, clock, ha)
    rt.store.update(write_budget={"enforce:curve": {"day": TODAY.isoformat(), "count": 6, "last": 0.0}})
    _rounds(rt)
    assert _pushes(rt) == [_limit_meldung("Heizkurve")]
    assert rt.kunde.shown == {}  # nicht kritisch: keine offene Meldung


@pytest.mark.parametrize(("live", "durchgesetzt"), [(20.6, True), (20.4, True), (20.55, False), (20.5, False)])
def test_min_flow_deviation_of_one_plant_step_is_enforced(make_store, clock, live, durchgesetzt):
    """A5-64: Toleranz = halber Anlagenschritt (0,05) wie bei allen Hebeln; Soll = Raum-Soll 20,5."""
    ha = Ha(min_flow=live)
    rt = _rt(make_store, clock, ha)
    _rounds(rt)
    assert (("number.mf", 20.5) in ha.writes) is durchgesetzt


def test_state_ok_matches_the_notifier():
    from smartheat_core.enforce import STATE_OK
    from smartheat_runtime.notifier import STATE_OK as NOTIFIER_OK
    assert STATE_OK == NOTIFIER_OK


# --- Mutationsprobe Stufe 3 S3 (Task 11) ------------------------------------------------------------------------

def test_today_is_the_wallclock_day(monkeypatch):
    """Mutationsprobe (Mutant 2): _today() liefert den Tag der Wanduhr, nicht None."""
    monkeypatch.setattr(wallclock, "_now", lambda: datetime(2026, 10, 14, 23, 59))
    assert _ECHTES_HEUTE() == date(2026, 10, 14)


def test_a_clean_round_resets_the_detection_counter(make_store, clock):
    """Mutationsprobe (Mutant 107): eine unauffaellige Runde setzt den Zaehler auf 0 zurueck; eine einzelne spaetere
    Abweichung wird wieder erst nach DETECTION_ROUNDS Runden durchgesetzt."""
    ha = Ha(curve=1.3)
    rt = _rt(make_store, clock, ha)
    _rounds(rt, 1)
    assert rt.store.state.manual_override_misses == 1
    ha.states["number.curve"] = 0.9
    _rounds(rt, 1)
    assert rt.store.state.manual_override_misses == 0
    ha.states["number.curve"] = 1.3
    _rounds(rt, 1)
    assert ha.writes == []


def test_return_to_learned_values_is_reported_quietly_and_not_critical(make_store, clock, caplog):
    """Mutationsprobe (Mutanten 111, 112): die Rueckkehr-Meldung ist weder kritisch noch laut."""
    ha = Ha(curve=1.3)
    rt = _rt(make_store, clock, ha)
    _rounds(rt)
    assert _meldezustand(rt) == "curve=1.3"
    ha.states["number.curve"] = 0.9
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="smartheat_runtime.notifier"):
        _rounds(rt, 1)
    assert _meldezustand(rt) == "ok"
    assert rt.kunde.pushes == [("manueller_eingriff", _meldung("Heizkurve"))]  # still: kein Push
    assert rt.kunde.shown == {} and rt.kunde.withdrawn == []  # nicht kritisch: keine offene Meldung angefasst
    assert [(r.levelno, r.getMessage()) for r in caplog.records if r.name == "smartheat_runtime.notifier"] == [
        (logging.INFO, "SmartHeat: Die Heizungseinstellung steht wieder auf den gelernten Werten.")]


def test_reset_message_is_not_critical(make_store, clock):
    """Mutationsprobe (Mutant 121): die Meldung ueber das Zuruecksetzen ist nicht kritisch."""
    rt = _rt(make_store, clock, Ha(curve=1.3))
    _rounds(rt)
    assert _message_pushes(rt) == [_meldung("Heizkurve")]
    assert rt.kunde.shown == {}  # nicht kritisch: keine offene Meldung


def test_zone_mode_deviation_is_recorded_as_zero(make_store, clock):
    """Mutationsprobe (Mutant 33): die verstellte Betriebsart steht im Eingriff als 0 (nicht aktiv), nicht als 0,5."""
    rt = _rt(make_store, clock, Ha(mode="auto", reflects_writes=False))
    _rounds(rt)
    record = rt.store.state.manual_override
    assert record["rollen"][enforce.ZONE_MODE] == 0.0
    assert "zone_mode=0" in record["signatur"].split(",") and "zone_mode=0.5" not in record["signatur"]


def test_persisted_kpi_falls_back_to_zero_without_live_value_and_restore_point(make_store, clock):
    """Mutationsprobe (Mutant 90): ohne Live-Wert und ohne Wiederherstellungspunkt steht im Eintrag 0.0."""
    rt = _rt(make_store, clock, Ha(curve=1.3, shift=0.0, reflects_writes=False), room_setpoint=None)
    _rounds(rt)
    assert rt.store.state.manual_override["levers"]["room_setpoint"] == 0.0


def test_kpi_takes_the_live_value_of_a_lever_without_setpoint(make_store, clock):
    """Mutationsprobe (Mutant 85): ein lesbarer Hebel ohne Sollwert geht mit seinem Live-Wert in den KPI."""
    rt = _rt(make_store, clock, Ha(curve=1.3, shift=22.0, reflects_writes=False), room_setpoint=None)
    _rounds(rt)
    assert rt.store.state.manual_override_pending["levers"]["room_setpoint"] == 22.0


@pytest.mark.parametrize(("ha", "zeile"), [
    (lambda: Ha(curve=RuntimeError(FREMDTEXT)), f"Eingriff nicht pruefbar, curve nicht lesbar: {FREMDTEXT_IM_LOG}"),
    (lambda: Ha(mode=RuntimeError(FREMDTEXT)), f"Betriebsart der Zone nicht lesbar: {FREMDTEXT_IM_LOG}"),
], ids=["hebel", "betriebsart"])
def test_a_read_error_from_ha_is_logged_only_escaped(make_store, clock, caplog, ha, zeile):
    """T6-Rest, R-c: der Fehlertext beim Lesen aus HA (Hebel, Betriebsart der Zone) steht nur als Escape im Log."""
    rt = _rt(make_store, clock, ha())
    with caplog.at_level(logging.INFO, logger="smartheat_core.enforce"):
        _rounds(rt)
    assert zeile in logzeilen(caplog)


@pytest.mark.parametrize(("live", "durchgesetzt"), [(0.925, False), (0.926, True)])
def test_a_deviation_of_exactly_tolerance_plus_float_rest_is_no_deviation(make_store, clock, live, durchgesetzt):
    """Testoffensive T8 (Mutationsprobe K2): die Grenze gehoert zur Toleranz. Die Toleranz der Steigung (knapp unter
    0,025; der Server darf jede Toleranz bis zum Schritt senden) ist so gewaehlt, dass 0,925 genau Toleranz plus
    Gleitkomma-Rest 1e-9 ueber dem Soll 0,9 liegt: keine Abweichung; 0,926 ist eine."""
    toleranz = (0.925 - 0.9) - 1e-9
    assert toleranz + 1e-9 == 0.925 - 0.9 and 0.0249 < toleranz < 0.025  # genau auf der Grenze, nicht ungefaehr
    description = dataclasses.replace(
        VAILLANT_MYPYLLANT, enforce_tolerance={**VAILLANT_MYPYLLANT.enforce_tolerance, "curve": toleranz})
    ha = Ha(curve=live)
    rt = _rt(make_store, clock, ha, description=description)
    assert rt.override.expected_values()["curve"] == 0.9  # das Soll, gegen das die Grenze gerechnet ist
    _rounds(rt)
    assert (("number.curve", 0.9) in ha.writes) is durchgesetzt
    assert (rt.store.state.manual_override is not None) is durchgesetzt


def test_a_failed_write_back_within_the_retry_interval_sends_no_limit_message(make_store, clock):
    """Testoffensive T8 (Mutationsprobe K2): der erste Rueckschreibversuch scheitert; die Runden in den naechsten
    30 Minuten (Abstand der Versuche) schreiben nicht und melden auch kein Tageslimit -- erst 6 Versuche am Tag sind
    das Limit."""
    ha = Ha(curve=1.3)
    ha.failing.add("number.curve")
    rt = _rt(make_store, clock, ha)
    _rounds(rt)
    assert [entity for entity, _, _ in ha.attempts] == ["number.curve"]
    for _ in range(5):  # 25 min im 5-min-Takt, unter den 30 min bis zum naechsten Versuch
        clock.advance(300)
        enforce.check_manual_override(rt)
    assert [entity for entity, _, _ in ha.attempts] == ["number.curve"]
    assert _pushes(rt) == []
    assert _meldezustand(rt) == "ok"  # keine offene Meldung, auch kein Tageslimit
