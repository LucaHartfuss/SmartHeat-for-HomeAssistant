"""Regel 4 (CLAUDE.md; Spec Stufe 3 S3 Abschnitt 6.1): alle lokalen Sicherheitswerte als woertliche Tabelle, bewusst
nicht aus dem Code abgeleitet. Scheitert ein Test hier, wurde eine Sicherheitsgrenze geaendert: das braucht die
ausdrueckliche Freigabe des Nutzers; danach Tabelle und module/client-kern.md ("Lokale Grenzen") im selben Commit.
Die Verhaltenstabelle rechnet aus derselben Tabelle und laeuft durch die Hebel-Pipeline mit einer Attrappe nur an der
V2-Grenze (PlantBinding)."""
import math
from datetime import timedelta

import pytest
from bindung_beispiele import BINDINGS

from heizungsbruecke import ha_binding
from smartheat_core import boost, emergency_boost, write_budget
from smartheat_core.clamping import round_to_step
from smartheat_core.pipeline import LeverPipeline
from smartheat_core.safety import LOCAL_SAFETY
from smartheat_device import wire
from smartheat_runtime import delivery, entitlement, plausibility

R4 = "Regel 4: Änderung braucht die Freigabe des Nutzers"
HK, FBH = "Heizkoerper", "Fussbodenheizung"

#: (Hebelsatz, Verteilsystem) -> (Bereiche, Comfort-Boost, Notfall-Hebel, Ankunftsschwelle)
TABELLE = {
    ("vaillant_vrc720", HK): (
        {"curve": (0.4, 1.5), "room_setpoint": (15.0, 25.0), "heat_limit": (5.0, 23.0), "min_flow": (20.0, 30.0)},
        {"curve": 1.5, "room_setpoint": 25.0, "heat_limit": 23.0}, ("curve", "room_setpoint", "heat_limit"), 0.5),
    ("weishaupt_wwp", HK): (
        {"curve": (0.30, 1.00), "room_setpoint": (16.0, 25.0), "heat_limit": (5.0, 23.0)},
        {"curve": 1.00, "room_setpoint": 25.0, "heat_limit": 23.0}, ("curve", "room_setpoint", "heat_limit"), 0.5),
    ("weishaupt_wwp_basis", HK): (
        {"room_setpoint": (16.0, 25.0)}, {"room_setpoint": 25.0}, ("room_setpoint",), 0.5),
    ("viessmann_vicare", HK): (
        {"curve": (0.4, 1.4), "level": (-13.0, 3.0), "room_setpoint": (15.0, 25.0)},
        {"curve": 1.4, "level": 3.0, "room_setpoint": 25.0}, ("curve", "level", "room_setpoint"), 0.5),
    ("vaillant_vrc720", FBH): (
        {"curve": (0.10, 0.35), "room_setpoint": (15.0, 24.0), "heat_limit": (5.0, 20.0), "min_flow": (20.0, 30.0)},
        {}, ("curve", "room_setpoint", "heat_limit"), 0.5),
    ("weishaupt_wwp", FBH): (
        {"curve": (0.10, 0.50), "room_setpoint": (16.0, 24.0), "heat_limit": (5.0, 20.0)},
        {}, ("curve", "room_setpoint", "heat_limit"), 0.5),
    ("weishaupt_wwp_basis", FBH): ({"room_setpoint": (16.0, 24.0)}, {}, ("room_setpoint",), 0.5),
    ("viessmann_vicare", FBH): (
        {"curve": (0.2, 0.4), "level": (-5.0, 3.0), "room_setpoint": (15.0, 24.0)},
        {}, ("curve", "level", "room_setpoint"), 0.5),
}

KONSTANTEN = {
    "Plausibilitaet Raum": (plausibility.ROOM_TEMP_RANGE, (5.0, 35.0)),
    "Plausibilitaet aussen": (plausibility.OUTDOOR_TEMP_RANGE, (-40.0, 45.0)),
    "Bedienwunsch": ((wire.RAUM_SOLL_MIN, wire.RAUM_SOLL_MAX, wire.RAUM_SOLL_STEP), (15.0, 25.0, 0.5)),
    "Lesebereich Raum-Soll": ((ha_binding.ROOM_SETPOINT_READ_MIN, ha_binding.ROOM_SETPOINT_READ_MAX), (5.0, 35.0)),
    "Comfort-Boost Ausloeser": (boost._TARGET_RISE_EPSILON_K, 0.01),
    "Comfort-Boost hoechstens": (boost.COMFORT_BOOST_MAX_SECONDS, 4 * 3600),
    "Comfort-Boost unlesbar": (boost.UNREADABLE_ROOM_CHECKS, 3),
    "Notfall-Boost ab": (emergency_boost.EMERGENCY_TRIGGER_K, 1.0),
    "Notbetrieb": ((delivery.NOTBETRIEB_AFTER_SERVER_FAILURES, delivery.ACK_TIMEOUT_SECONDS), (2, 30)),
    "Abo-Frist": (entitlement.GRACE_PERIOD, timedelta(days=30)),
}


@pytest.mark.parametrize("key", sorted(TABELLE))
def test_lokale_grenzen(key):
    ranges, comfort, notfall, ankunft = TABELLE[key]
    safety = LOCAL_SAFETY[key]
    ist = (dict(safety.ranges), dict(safety.comfort_boost), safety.emergency_boost_levers, safety.arrival_threshold_k)
    assert ist == (ranges, comfort, notfall, ankunft), f"{R4}: {key}"


def test_keine_weiteren_saetze():
    assert set(LOCAL_SAFETY) == set(TABELLE), R4


@pytest.mark.parametrize("name", sorted(KONSTANTEN))
def test_konstanten(name):
    ist, soll = KONSTANTEN[name]
    assert ist == soll, f"{R4}: {name}"


# --- Verhaltenstabelle ---

AUX = {"mode_select": "start", "setpoint_comfort": 21.0, "setpoint_setback": 18.0}


class TabellenAnlage:
    """PlantBinding (V2) ohne Hersteller: haelt die Hebel des Hebelsatzes in der Mitte ihres Bereichs, speichert jeden
    Schreibvorgang, keine Vorbereitung, kein Anlagenbereich (limits None: es gelten die lokalen Grenzen)."""

    def __init__(self, hebelsatz: str, ranges: dict):
        self.description = BINDINGS[hebelsatz]
        self.physical_writes = 0
        self.writes: list[tuple[str, float]] = []
        self.aux_restored: list[dict] = []
        steps = self.description.steps
        self.values = {lever: round_to_step((ranges[lever][0] + ranges[lever][1]) / 2, steps[lever])
                       for lever in self.description.lever_set.levers}

    def has(self, lever):
        return lever in self.values

    def ref(self, lever):
        return f"tabelle.{lever}"

    def read(self, lever):
        return self.values[lever]

    def read_or(self, lever, fallback):
        return self.values.get(lever, fallback)

    def write(self, lever, value):
        self.values[lever] = value
        self.writes.append((lever, value))
        self.physical_writes += 1

    def needs_preparation(self):
        return False

    def is_prepared(self):
        return True

    def prepare(self):
        return False

    def read_aux(self):
        return {name: AUX[name] for name in self.description.aux_originals}

    def restore_aux(self, values, levers=None):
        self.aux_restored.append(dict(values))

    def aux_zulaessig(self, name, wert):
        return wert in ("start", "anders")

    def limits(self, lever):
        return None


def _pipeline(make_store, clock, key):
    ranges = TABELLE[key][0]
    anlage = TabellenAnlage(key[0], ranges)
    pipeline = LeverPipeline(make_store(), anlage, LOCAL_SAFETY[key], clock=clock)
    clock.advance(anlage.description.settle_seconds + 1)
    return pipeline, anlage


FAELLE = [(key, lever) for key in sorted(TABELLE) for lever in BINDINGS[key[0]].lever_set.levers]


@pytest.mark.parametrize(("key", "lever"), FAELLE)
def test_unter_dem_minimum_wird_die_grenze_geschrieben(make_store, clock, key, lever):
    pipeline, anlage = _pipeline(make_store, clock, key)
    low = TABELLE[key][0][lever][0]
    pipeline.apply_server_values({lever: low - 1.0})
    assert anlage.values[lever] == low


@pytest.mark.parametrize(("key", "lever"), FAELLE)
def test_ueber_dem_maximum_wird_die_grenze_geschrieben(make_store, clock, key, lever):
    pipeline, anlage = _pipeline(make_store, clock, key)
    high = TABELLE[key][0][lever][1]
    pipeline.apply_server_values({lever: high + 1.0})
    assert anlage.values[lever] == high


@pytest.mark.parametrize(("key", "lever"), FAELLE)
def test_unendlich_wird_begrenzt_nan_abgelehnt(make_store, clock, key, lever):
    pipeline, anlage = _pipeline(make_store, clock, key)
    low, high = TABELLE[key][0][lever]
    pipeline.apply_server_values({lever: math.inf})
    assert anlage.values[lever] == high
    pipeline.apply_server_values({lever: -math.inf})
    assert anlage.values[lever] == low
    with pytest.raises(ValueError):
        pipeline.apply_server_values({lever: math.nan})
    assert anlage.values[lever] == low


@pytest.mark.parametrize("key", sorted(k for k in TABELLE if TABELLE[k][1]))
def test_comfort_boost_schreibt_genau_die_boost_werte(make_store, clock, key):
    pipeline, anlage = _pipeline(make_store, clock, key)
    pipeline.set_boosts(comfort=True, emergency=False)
    assert {lever: anlage.values[lever] for lever in TABELLE[key][1]} == TABELLE[key][1]


@pytest.mark.parametrize("key", sorted(TABELLE))
def test_notfall_boost_schreibt_das_maximum(make_store, clock, key):
    pipeline, anlage = _pipeline(make_store, clock, key)
    pipeline.set_boosts(comfort=False, emergency=True)
    ranges, _, notfall, _ = TABELLE[key]
    assert {lever: anlage.values[lever] for lever in notfall} == {lever: ranges[lever][1] for lever in notfall}


@pytest.mark.parametrize("key", sorted(k for k in TABELLE if k[1] == FBH))
def test_fussbodenheizung_hat_keine_comfort_zeile(make_store, clock, key):
    pipeline, anlage = _pipeline(make_store, clock, key)
    pipeline.set_boosts(comfort=True, emergency=False)
    assert anlage.writes == []


#: Spec Stufe 3 S4b 4.2: Obergrenzen des Schreibschutzes (Hebelsatz -> (durchsetzen_tag, tag, lebensdauer_hinweis)).
#: Der Server darf nur gleich oder strenger senden. Werte unveraendert aus smartheat_core/binding.py bis 1.0.0.
SCHREIBSCHUTZ = {
    "vaillant_vrc720": (6, None, None),
    "weishaupt_wwp": (6, 10, 50000),
    "weishaupt_wwp_basis": (6, 10, 50000),
    "viessmann_vicare": (4, None, None),
}


def test_schreibschutz_obergrenzen():
    from smartheat_core.verfahren import VERFAHREN

    assert {
        k: (v.obergrenze.durchsetzen_tag, v.obergrenze.tag, v.obergrenze.lebensdauer_hinweis)
        for k, v in VERFAHREN.items()
    } == SCHREIBSCHUTZ, R4


#: Lokale Positivliste der Weishaupt-Optionsschema-Praefixe (weishaupt_modbus 1.x und 2.0). Der Server darf nur eine
#: Teilmenge senden; ein neues Schema braucht ein Geraete-Update (Regel 4).
PRAEFIXE_ERLAUBT = {
    "vaillant_vrc720": (),
    "weishaupt_wwp": ("hz_operationmode_", "heating_circuit_operation_mode_"),
    "weishaupt_wwp_basis": ("hz_operationmode_", "heating_circuit_operation_mode_"),
    "viessmann_vicare": (),
}


def test_weishaupt_praefixe_lokale_positivliste():
    from smartheat_core.verfahren import VERFAHREN

    assert {k: v.praefixe_erlaubt for k, v in VERFAHREN.items()} == PRAEFIXE_ERLAUBT, R4


#: Spec Fixrunde A Schreibschutz 3.1/3.2 (A6-01, A6-34): Boost-Starts je Art (Schluessel -> (jeder Versuch zaehlt,
#: Mindestabstaende in Sekunden, hoechstens je Tag)). Comfort- und Notfall-Boost zaehlen getrennt.
BOOST_KONTINGENT = {"boost_start": (True, (1800,), 6), "emergency_start": (True, (1800,), 6)}


def test_boost_kontingent_regeln():
    regel = write_budget.START_QUOTA
    assert {key: (regel.every_attempt, regel.waits, regel.max_per_day) for key in BOOST_KONTINGENT} == BOOST_KONTINGENT, R4


def test_boost_kontingent_schluessel():
    assert {write_budget.BOOST_START, write_budget.EMERGENCY_START} == set(BOOST_KONTINGENT), R4


@pytest.mark.parametrize(("schluessel", "comfort", "notfall"), [
    ("boost_start", True, False), ("emergency_start", False, True),
])
def test_sechster_start_am_tag_sperrt_den_siebten(make_store, clock, monkeypatch, schluessel, comfort, notfall):
    monkeypatch.setattr(write_budget, "today", lambda: "2026-10-03")
    pipeline, anlage = _pipeline(make_store, clock, ("vaillant_vrc720", HK))
    write_budget.put(pipeline._store, schluessel, {"day": "2026-10-03", "count": 6, "last": 0.0})
    clock.advance(86400)  # der Abstand ist lange vorbei, nur die Tageszahl sperrt
    assert pipeline.set_boosts(comfort=comfort, emergency=notfall) == (False, False), R4
    assert anlage.writes == [], R4
