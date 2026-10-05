"""Durchsetzung (TP11, Spec 5.3)."""
from datetime import date
from unittest.mock import MagicMock

import pytest
from fakes import runtime_config

from heizungsbruecke.ha_binding import HaPlantBinding
from smartheat_core import enforce
from smartheat_core.binding import VAILLANT_MYPYLLANT
from smartheat_core.pipeline import LeverPipeline
from smartheat_core.safety import LocalSafety
from smartheat_runtime.delivery import SOURCE_LOCAL, DataFault, DeliveryState
from smartheat_runtime.notifier import STATE_OK, Notifier
from smartheat_runtime.roles import ChannelManifest
from smartheat_runtime.runtime import Runtime

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
MANIFEST = ChannelManifest(entity_ids={
    "curve_current": "number.curve", "shift_current": "climate.zone::temperature", "min_flow": "number.mf",
})
POINT = {"curve": 0.9, "room_setpoint": 21.0}
TODAY = date(2026, 10, 3)


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


def _rt(make_store, clock, ha, uptime=SETTLE + 1, store=None, manifest=MANIFEST, **point):
    """Runtime mit `uptime` Sekunden Laufzeit seit dem Start (Default: Schonfrist nach dem Start
    vorbei, HA gilt ohne eigenes Schreiben als eingeschwungen). `store` wiederverwendet einen
    vorhandenen StateStore (Neustart-Simulation: derselbe backup.json-Pfad, frisch eingelesen)
    statt den Wiederherstellungspunkt POINT (geaendert um `point`, None = fehlt) neu zu schreiben."""
    if store is None:
        restore_point = {lever: value for lever, value in {**POINT, **point}.items() if value is not None}
        store = make_store(backup={"restore_point": restore_point})
    store.update(stable_target=20.5)
    notifier = MagicMock(spec=Notifier)
    notifier.notify.return_value = True
    override = LeverPipeline(store, HaPlantBinding(ha, manifest), SAFETY, clock=clock)
    ha.clock = clock
    clock.advance(uptime)
    return Runtime(
        manifest=manifest, signals=ha, config=runtime_config(), worker=MagicMock(), store=store,
        override=override, notifier=notifier, clock=clock,
    )


MANIFEST_G = ChannelManifest(entity_ids={**MANIFEST.entity_ids, "heat_limit": "number.hl"})


def _rt_g(make_store, clock, live_heat_limit):
    ha = Ha()
    ha.states["number.hl"] = live_heat_limit
    return _rt(make_store, clock, ha, manifest=MANIFEST_G, heat_limit=15.0)


def _rounds(rt, n=enforce.DETECTION_ROUNDS):
    for _ in range(n):
        enforce.check_manual_override(rt)


def _curve_writes(rt):
    return [w for w in rt.signals.writes if w[0] == "number.curve"]


def _limit_calls(rt):
    return [c for c in rt.notifier.notify.call_args_list if str(c.args[1]).startswith("limit")]


def _message_calls(rt):
    return [
        c for c in rt.notifier.notify.call_args_list
        if c.args[1] != STATE_OK and not str(c.args[1]).startswith("limit")
    ]


def test_no_deviation_no_write(make_store, clock):
    rt = _rt(make_store, clock, Ha())
    _rounds(rt)
    assert rt.signals.writes == []


def test_manual_curve_change_is_written_back_and_reported(make_store, clock):
    rt = _rt(make_store, clock, Ha(curve=1.3))
    _rounds(rt)
    assert rt.signals.writes == [("number.curve", 0.9)]
    assert rt.store.state.manual_override_pending["levers"]["curve"] == 1.3
    assert rt.notifier.notify.call_args.args[0] == enforce.KEY


def test_heat_limit_changed_in_the_app_is_written_back_and_reported(make_store, clock):
    rt = _rt_g(make_store, clock, 16.0)
    _rounds(rt)
    assert ("number.hl", 15.0) in rt.signals.writes
    assert rt.store.state.manual_override_pending is not None
    assert "Heizgrenze" in rt.notifier.notify.call_args.args[2]


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
    assert [c for c in rt.notifier.notify.call_args_list if c.args[1] == STATE_OK] == []


def test_min_flow_deviation_is_written_back(make_store, clock):
    rt = _rt(make_store, clock, Ha(min_flow=25.0))
    _rounds(rt)
    assert rt.signals.writes == [("number.mf", 20.5)]


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
    assert enforce._is_number(override["levers"]["curve"]) and enforce._is_number(override["levers"]["room_setpoint"])
    assert override["gemeldet"] == override["signatur"]

    # Neustart: derselbe backup.json-Pfad, frisch eingelesen.
    reloaded = make_store()
    assert reloaded.state.manual_override == override  # state._is_override akzeptiert den Eintrag

    rt2 = _rt(make_store, clock, ha, store=reloaded)
    _rounds(rt2)

    assert _message_calls(rt2) == []  # derselbe Eingriff wird nicht erneut gemeldet


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
    assert rt.notifier.notify.call_args_list == []
    assert rt.store.state.manual_override is None
    clock.advance(61)
    _rounds(rt)
    assert rt.signals.writes == [("number.curve", 0.9)]
    assert len(_message_calls(rt)) == 1


def test_enforcement_rate_limited_per_day(make_store, clock):
    # Review Focus 2: mypyllant uebernimmt das Rueckschreiben nicht -- hoechstens
    # MAX_WRITES_PER_DAY Schreibvorgaenge, genau eine Eingriffs- und eine Limit-Meldung.
    rt = _rt(make_store, clock, Ha(curve=1.3, reflects_writes=False))
    for _ in range(20):
        clock.advance(SETTLE + 1)
        _rounds(rt)
    assert len(_curve_writes(rt)) == enforce.MAX_WRITES_PER_DAY
    assert len(_message_calls(rt)) == 1
    assert len(_limit_calls(rt)) == 1
    assert [c for c in rt.notifier.notify.call_args_list if c.args[1] == STATE_OK] == []


def test_ignored_write_is_reported_once_until_the_return(make_store, clock):
    # Plan-Praezisierung "Durchsetzungs-Meldung": die Meldung bleibt stehen, bis der Istwert
    # wieder stimmt; erst danach ist ein neuer Eingriff eine neue Meldung.
    rt = _rt(make_store, clock, Ha(curve=1.3, reflects_writes=False))
    _rounds(rt)
    for _ in range(3):
        clock.advance(SETTLE + 1)
        _rounds(rt)
    assert len(_message_calls(rt)) == 1
    rt.signals.states["number.curve"] = 0.9  # die Anlage uebernimmt den Wert endlich
    clock.advance(SETTLE + 1)
    _rounds(rt)
    assert rt.store.state.manual_override is None
    assert rt.notifier.notify.call_args.args[1] == STATE_OK


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
    rt.store.update(write_budget={"enforce:curve": {"day": day, "count": enforce.MAX_WRITES_PER_DAY,
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
    enforce.check_manual_override(rt)  # Anlage steht wieder richtig
    assert rt.store.state.manual_override is None
    assert rt.notifier.notify.call_args.args[1] == STATE_OK


def test_counter_resets_next_day(make_store, clock, monkeypatch):
    rt = _rt(make_store, clock, Ha(curve=1.3))
    monkeypatch.setattr(enforce, "_today", lambda: date(2026, 10, 3))
    for _ in range(10):
        rt.signals.states["number.curve"] = 1.3
        clock.advance(SETTLE + 1)
        _rounds(rt)
    assert len(_curve_writes(rt)) == enforce.MAX_WRITES_PER_DAY
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
    # RETRY_SECONDS und MAX_WRITES_PER_DAY am Tag; kein "zurueckgesetzt", genau eine Limit-Meldung.
    ha = Ha(curve=1.3)
    ha.failing.add("number.curve")
    rt = _rt(make_store, clock, ha)
    for _ in range(12 * 24):  # ein Tag im 5-min-Takt
        clock.advance(300)
        enforce.check_manual_override(rt)
    times = [at for entity, _, at in ha.attempts if entity == "number.curve"]
    assert len(times) == enforce.MAX_WRITES_PER_DAY
    assert all(later - earlier >= enforce.RETRY_SECONDS for earlier, later in zip(times, times[1:], strict=False))
    assert _message_calls(rt) == []
    assert len(_limit_calls(rt)) == 1
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
    assert len(_message_calls(rt)) == 1
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
    for _ in range(enforce.MAX_WRITES_PER_DAY):
        rt.signals.states["number.curve"] = 1.3
        clock.advance(SETTLE + 1)
        _rounds(rt)
        enforce.check_manual_override(rt)  # Rueckkehr
    assert len(_curve_writes(rt)) == enforce.MAX_WRITES_PER_DAY
    rt.notifier.notify.reset_mock()
    rt.signals.states["number.curve"] = 1.3
    clock.advance(SETTLE + 1)
    _rounds(rt)
    assert _message_calls(rt) == []
    assert len(_limit_calls(rt)) == 1
    assert rt.store.state.manual_override is not None


def test_zone_mode_without_shift_target_switches_the_zone_only(make_store, clock):
    # MINOR 2: kein Wiederherstellungspunkt fuer die Parallelverschiebung -- nur die Zone auf
    # Manuell stellen, keinen Sollwert schreiben.
    rt = _rt(make_store, clock, Ha(mode="auto"), room_setpoint=None)
    _rounds(rt)
    assert rt.signals.writes == [("climate.zone", "heat_cool")]
    assert len(_message_calls(rt)) == 1


def test_curve_tolerance_is_half_a_plant_step(make_store, clock):
    # MINOR 4: eine erkannte Abweichung fuehrt immer zu einem echten Schreibvorgang (der Quota-Check
    # ueberspringt erst innerhalb eines halben Schritts).
    assert VAILLANT_MYPYLLANT.enforce_tolerance["curve"] == pytest.approx(0.025)
    rt = _rt(make_store, clock, Ha(curve=0.92))
    _rounds(rt)
    assert rt.signals.writes == []
    assert rt.store.state.manual_override is None


def test_an_intervention_reported_by_0_29_0_is_not_reported_again_after_the_update(make_store, clock):
    """Plan 2 (P2-4): backup.json aus 0.29.0 nennt in rollen/signatur/gemeldet und im Schreibbudget Rollen. Nach der
    Migration auf Hebel bleibt derselbe Eingriff derselbe (keine zweite Meldung), das Rueckschreiben laeuft weiter und
    zaehlt auf dem migrierten Budget-Schluessel."""
    store = make_store(backup={
        "curve_current": 0.9, "shift_current": 21.0,
        "manual_override": {"curve": 1.3, "shift": 21.0, "erkannt": "2026-10-02T09:00:00+02:00",
                            "rollen": {"curve_current": 1.3}, "signatur": "curve_current=1.3",
                            "gemeldet": "curve_current=1.3"},
        "write_budget": {"enforce:curve_current": {"day": TODAY.isoformat(), "count": 1}},
    })
    rt = _rt(make_store, clock, Ha(curve=1.3, reflects_writes=False), store=store)
    _rounds(rt)
    assert rt.signals.writes == [("number.curve", 0.9)]
    assert _message_calls(rt) == []
    assert rt.store.state.manual_override["gemeldet"] == rt.store.state.manual_override["signatur"] == "curve=1.3"
    assert rt.store.state.write_budget["enforce:curve"]["count"] == 2


def test_state_ok_matches_the_notifier():
    from smartheat_core.enforce import STATE_OK
    from smartheat_runtime.notifier import STATE_OK as NOTIFIER_OK
    assert STATE_OK == NOTIFIER_OK
