"""Testoffensive T4' Task 7 (Spec §5 "Kompromittierter Server", Regel 4): die lokalen Sicherheitswerte als Property-
und Grenztests. Ein Server, der beliebige Hebelwerte, Schrittweiten und Konfigurationen sendet, und eine Anlage mit
beliebigem Wertebereich bringen keinen Schreibwert ausserhalb der lokalen Grenzen. Die Grenzen stehen woertlich in
test_regel4_werte.py (TABELLE, Nutzer-Freigaben), nie aus dem Code; Generatoren kommen aus strategien.py, die
Laufzeit-Umgebung aus test_property_geraet.py. Findet ein Test hier einen Schreibwert ausserhalb der Grenze, ist das ein Stoppfall (Regel 4),
kein Fix im Produktcode. Beispielzahl, Seed und Frist kommen aus den Profilen in conftest.py."""
import json
import logging
import math
import os
import tempfile
from pathlib import Path

import pytest
from bindung_beispiele import BINDINGS, parameter
from conftest import FakeClock
from hypothesis import example, given
from hypothesis import strategies as st
from strategien import JSON_WERT, WEG, ersetze, pfade
from test_core_config_check import _konfiguration as gateway_konfiguration
from test_core_config_check_ha import _ha as ha_konfiguration
from test_property_geraet import NICHT_NUMERISCH, SAETZE, _endlich, _Fehlerlog, _frische_umgebung
from test_regel4_werte import FBH, HK, TABELLE, TabellenAnlage
from test_runtime import _ha_verbunden, _mqtt, _start

from smartheat_core import config_check as cc
from smartheat_core.bindungsparameter import beschreibung
from smartheat_core.clamping import round_to_step
from smartheat_core.pipeline import LeverPipeline
from smartheat_core.rastung import Rastung
from smartheat_core.safety import LOCAL_SAFETY, check_invariants, resolve_local_safety
from smartheat_device import fehler
from smartheat_runtime.state import StateStore

HEBELSAETZE = ("vaillant_vrc720", "weishaupt_wwp", "weishaupt_wwp_basis", "viessmann_vicare")
#: Eingaben an der float-Grenze und darueber hinaus (riesige Ganzzahlen aus JSON)
RIESEN = [2**1100, -(2**1100), 2**1024, (2**53 - 1) * 2**971]
SCHRITT = st.floats(0.05, 5) | st.sampled_from([0.05, 5.0])  # Schrittweiten des Servers (bindungsparameter: <= 5)


@pytest.fixture(autouse=True)
def _ohne_fsync(monkeypatch):
    """Durchsatz wie in test_property_geraet.py: je Beispiel entstehen ein Speicher und etliche Dateien."""
    monkeypatch.setattr(os, "fsync", lambda fd: None)


def _bekannt(hebelsatz, verteilsystem) -> bool:
    return isinstance(verteilsystem, str) and (hebelsatz, verteilsystem) in TABELLE


def _hebel(key) -> tuple[str, ...]:
    """Gesendete und client-abgeleitete Hebel (Mindestvorlauf) des Hebelsatzes."""
    lever_set = BINDINGS[key[0]].lever_set
    return (*lever_set.levers, *lever_set.client_derived)


# --- resolve_local_safety -> check_invariants (F-client-kern-01) ---

def test_resolve_local_safety_liefert_einen_satz_in_den_invarianten_oder_einen_fehler():
    """Jede Eingabe aus Hebelsatz (Text, wie ihn jeder Aufrufer vorher prueft) und Verteilsystem (beliebiger Wert): ein
    bekanntes Paar ergibt einen Satz, der check_invariants besteht und die Invarianten auch unabhaengig erfuellt
    (Bereiche genau fuer die Hebel, endlich, Minimum <= Maximum, Comfort-Boost im Bereich und nur auf gesendeten Hebeln,
    Notfall-Hebel gesendet, Schwelle positiv); jedes andere ist ein ValueError, nie ein Rueckfall auf einen Satz."""
    zaehler = {"unbekannt": 0}
    je_paar = dict.fromkeys(TABELLE, 0)

    @given(st.sampled_from(HEBELSAETZE) | st.text(max_size=30), st.sampled_from((HK, FBH)) | JSON_WERT)
    def pruefen(hebelsatz, verteilsystem):
        try:
            satz = resolve_local_safety(hebelsatz, verteilsystem)
        except ValueError:
            assert not _bekannt(hebelsatz, verteilsystem), (hebelsatz, verteilsystem)
            zaehler["unbekannt"] += 1
            return
        assert _bekannt(hebelsatz, verteilsystem), (hebelsatz, verteilsystem)
        je_paar[(hebelsatz, verteilsystem)] += 1
        check_invariants(satz, hebelsatz, verteilsystem)
        gesendet = BINDINGS[hebelsatz].lever_set.levers
        assert set(satz.ranges) == set(TABELLE[(hebelsatz, verteilsystem)][0])
        for low, high in satz.ranges.values():
            assert math.isfinite(low) and math.isfinite(high) and low <= high
        for lever, wert in satz.comfort_boost.items():
            assert lever in gesendet and satz.ranges[lever][0] <= wert <= satz.ranges[lever][1]
        assert set(satz.emergency_boost_levers) <= set(gesendet)
        assert math.isfinite(satz.arrival_threshold_k) and satz.arrival_threshold_k > 0

    for paar in sorted(TABELLE):  # jedes der 8 Paare deterministisch, zusaetzlich zu den gezogenen Beispielen
        pruefen = example(*paar)(pruefen)
    pruefen()
    assert all(anzahl > 0 for anzahl in je_paar.values()), je_paar  # Gegenprobe: jedes Paar ergab einen Satz
    assert zaehler["unbekannt"] > 0  # und unbekannte Paare wurden abgelehnt


# --- Pipeline mit einer Tabellen-Anlage (F-client-kern-02, F-client-kern-07) ---

class GrenzAnlage(TabellenAnlage):
    """TabellenAnlage (PlantBinding nur an der V2-Grenze) mit den client-abgeleiteten Hebeln (Mindestvorlauf), einer
    waehlbaren Beschreibung (Schrittweiten des Servers) und einem rohen Wertebereich der Anlage je Hebel (auch NaN,
    Unendlich, Minimum ueber Maximum; None = keiner)."""

    def __init__(self, hebelsatz: str, ranges: dict, *, description=None, device=None):
        super().__init__(hebelsatz, ranges)
        if description is not None:
            self.description = description
        for lever in self.description.lever_set.client_derived:
            self.values[lever] = round_to_step((ranges[lever][0] + ranges[lever][1]) / 2, self.description.steps[lever])
        self.device = device or {}

    def limits(self, lever):
        return self.device.get(lever)


def _pipeline(verzeichnis: Path, key, **anlage):
    verzeichnis.mkdir(parents=True, exist_ok=True)
    grenz_anlage = GrenzAnlage(key[0], TABELLE[key][0], **anlage)
    uhr = FakeClock()
    store = StateStore(verzeichnis / "backup.json", verzeichnis / "failsafe_state.json")
    pipeline = LeverPipeline(store, grenz_anlage, LOCAL_SAFETY[key], clock=uhr)
    uhr.advance(grenz_anlage.description.settle_seconds + 1)
    return pipeline, grenz_anlage


def _schreiben(pipeline, werte: dict) -> None:
    """Gesendete Hebel als Serverwerte (apply_server_values), den Mindestvorlauf ueber write_lever (derived.py)."""
    gesendet = pipeline.binding.description.lever_set.levers
    pipeline.apply_server_values({lever: wert for lever, wert in werte.items() if lever in gesendet})
    for lever, wert in werte.items():
        if lever not in gesendet:
            pipeline.write_lever(lever, wert)


def _alle_in_den_lokalen_grenzen(key, writes) -> None:
    for lever, wert in writes:
        low, high = TABELLE[key][0][lever]
        assert low <= wert <= high, (key, lever, wert)


def _letzter_schreibwert(verzeichnis: Path, key, lever, wert):
    pipeline, anlage = _pipeline(verzeichnis, key)
    anlage.values[lever] = None  # die Anlage meldet keinen Sollwert: der Quota-Check laesst jeden Wert durch
    _schreiben(pipeline, {lever: wert})
    geschrieben = [w for name, w in anlage.writes if name == lever]
    assert geschrieben, (key, lever, wert)  # Gegenprobe: der Wert wurde wirklich geschrieben
    return geschrieben[-1]


GRENZ_FAELLE = [(key, lever) for key in sorted(TABELLE) for lever in _hebel(key)]
GRENZ_IDS = [f"{key[0]}-{key[1]}-{lever}" for key, lever in GRENZ_FAELLE]


@pytest.mark.parametrize(("key", "lever"), GRENZ_FAELLE, ids=GRENZ_IDS)
def test_untere_grenze_genau_und_ein_anlagenschritt_innen_und_aussen(tmp_path, key, lever):
    """Genau auf dem Minimum bleibt es, ein Anlagenschritt darueber bleibt unveraendert, einer darunter wird das
    Minimum (Schritt der Bindungsparameter, Grenze aus TABELLE)."""
    low, schritt = TABELLE[key][0][lever][0], BINDINGS[key[0]].steps[lever]
    werte = [_letzter_schreibwert(tmp_path / str(nr), key, lever, wert)
             for nr, wert in enumerate((low, low + schritt, low - schritt))]
    assert werte == [low, round(low + schritt, 6), low]


@pytest.mark.parametrize(("key", "lever"), GRENZ_FAELLE, ids=GRENZ_IDS)
def test_obere_grenze_genau_und_ein_anlagenschritt_innen_und_aussen(tmp_path, key, lever):
    high, schritt = TABELLE[key][0][lever][1], BINDINGS[key[0]].steps[lever]
    werte = [_letzter_schreibwert(tmp_path / str(nr), key, lever, wert)
             for nr, wert in enumerate((high, high - schritt, high + schritt))]
    assert werte == [high, round(high - schritt, 6), high]


#: beliebiger Serverwert: endliche Zahlen in jeder Groesse, NaN, ±Inf, Ganzzahlen ueber dem float-Bereich
NICHT_ENDLICH = [math.nan, math.inf, -math.inf]
WERT = (st.floats(allow_nan=True, allow_infinity=True) | st.floats(-60, 60) | st.integers(-50, 50)
        | st.sampled_from([*NICHT_ENDLICH, *RIESEN]))


def _mit_schritten(hebelsatz: str, schritte: dict):
    """Beschreibung aus den Bindungsparametern des Servers mit diesen Schrittweiten (Toleranz ein halber Schritt)."""
    return beschreibung(hebelsatz, parameter(hebelsatz, schritte=schritte,
                                             toleranzen={lever: s / 2 for lever, s in schritte.items()}))


def _satz_mit(werte_strategie):
    """(Satz, Schrittweiten je Hebel, Werte je Hebel): alle 8 Saetze x alle Hebel."""
    return st.sampled_from(sorted(TABELLE)).flatmap(lambda key: st.tuples(
        st.just(key), st.fixed_dictionaries({lever: SCHRITT for lever in _hebel(key)}),
        st.fixed_dictionaries({lever: werte_strategie for lever in _hebel(key)})))


def _ist_nan(wert) -> bool:
    return isinstance(wert, float) and math.isnan(wert)


def _ist_riesig(wert) -> bool:
    """Ganzzahl ausserhalb des float-Bereichs (als Literal: ab 2**1024 - 2**970 rundet sie auf Unendlich)."""
    return isinstance(wert, int) and not isinstance(wert, bool) and abs(wert) >= 2**1024 - 2**970


def test_kein_eingabewert_und_keine_schrittweite_ergibt_einen_schreibwert_ausserhalb_der_lokalen_grenze():
    """Beliebige Hebelwerte (auch ±Inf, NaN, 2**1100) mit Schrittweiten 0,05 bis 5 durch Begrenzen, Runden und erneutes
    Begrenzen der Pipeline: jeder Schreibwert liegt in der lokalen Grenze; abgelehnt (ValueError/OverflowError) wird
    nur eine nicht endliche Eingabe: ein ValueError nur, wenn ein Wert NaN ist, ein OverflowError nur bei einer Ganzzahl
    ausserhalb des float-Bereichs. Gegenprobe je Art: +Inf und -Inf werden je (geklemmt) geschrieben, NaN und eine
    Ganzzahl ueber dem float-Bereich je abgelehnt."""
    zaehler = {"geschrieben": 0, "plus_inf_geschrieben": 0, "minus_inf_geschrieben": 0, "nan_abgelehnt": 0,
               "riesig_abgelehnt": 0}

    vaillant = ("vaillant_vrc720", HK)
    schritte = {"curve": 0.05, "room_setpoint": 0.5, "heat_limit": 0.1, "min_flow": 0.1}

    @given(_satz_mit(WERT))
    # je Vorzeichen erzwungen (die Ziehung trifft +Inf sonst nicht): jeder Hebel +Inf bzw. -Inf
    @example((vaillant, schritte, dict.fromkeys(schritte, math.inf)))
    @example((vaillant, schritte, dict.fromkeys(schritte, -math.inf)))
    def pruefen(fall):
        key, schritte, werte = fall
        with tempfile.TemporaryDirectory() as verzeichnis:
            pipeline, anlage = _pipeline(Path(verzeichnis), key, description=_mit_schritten(key[0], schritte))
            try:
                _schreiben(pipeline, werte)
            except ValueError:
                assert any(_ist_nan(wert) for wert in werte.values()), werte
                zaehler["nan_abgelehnt"] += 1
            except OverflowError:
                assert any(_ist_riesig(wert) for wert in werte.values()), werte
                zaehler["riesig_abgelehnt"] += 1
            else:
                geschrieben = {lever for lever, _ in anlage.writes}
                for lever, wert in werte.items():
                    if lever in geschrieben and wert == math.inf:
                        zaehler["plus_inf_geschrieben"] += 1
                    elif lever in geschrieben and wert == -math.inf:
                        zaehler["minus_inf_geschrieben"] += 1
            _alle_in_den_lokalen_grenzen(key, anlage.writes)
            zaehler["geschrieben"] += len(anlage.writes)

    pruefen()
    assert all(anzahl > 0 for anzahl in zaehler.values()), zaehler  # Gegenprobe je Art


GRENZWERT = (st.floats(allow_nan=True, allow_infinity=True) | st.floats(-60, 60) | st.integers(-50, 50)
             | st.sampled_from([*NICHT_ENDLICH, *RIESEN]))
ANLAGENBEREICH = st.none() | st.tuples(GRENZWERT, GRENZWERT)


def _arten(bereich) -> set[str]:
    """Arten eines rohen Anlagenbereichs fuer die Gegenprobe (Zaehler je Art)."""
    if bereich is None:
        return {"keiner"}
    floats = [x for x in bereich if isinstance(x, float)]
    arten = {"nan"} if any(math.isnan(x) for x in floats) else set()
    if math.inf in floats:
        arten.add("plus_inf")
    if -math.inf in floats:
        arten.add("minus_inf")
    if "nan" not in arten and bereich[0] > bereich[1]:
        arten.add("min_ueber_max")
    return arten


def test_jeder_anlagenbereich_ergibt_einen_schreibbereich_innerhalb_der_lokalen_grenzen():
    """Wertebereich der Anlage je Hebel beliebig (NaN, ±Inf, Minimum ueber Maximum, riesige Ganzzahl, enger, weiter,
    ohne Schnittmenge): range_of liegt in der lokalen Grenze, und jeder geschriebene Wert auch."""
    zaehler = {"geschrieben": 0, "nan": 0, "plus_inf": 0, "minus_inf": 0, "min_ueber_max": 0, "keiner": 0}

    @given(st.sampled_from(sorted(TABELLE)).flatmap(lambda key: st.tuples(
        st.just(key), st.fixed_dictionaries({lever: ANLAGENBEREICH for lever in _hebel(key)}),
        st.fixed_dictionaries({lever: st.floats(-1e6, 1e6) | st.integers(-50, 50) for lever in _hebel(key)}))))
    # je Vorzeichen erzwungen (die Ziehung trifft +Inf sonst nicht): +Inf und -Inf je als Minimum und als Maximum
    @example((("vaillant_vrc720", HK),
              {"curve": (0.5, math.inf), "room_setpoint": (-math.inf, math.inf), "heat_limit": (math.inf, 10.0),
               "min_flow": (-math.inf, 25.0)},
              {"curve": 2.0, "room_setpoint": 30.0, "heat_limit": 0.0, "min_flow": 40.0}))
    def pruefen(fall):
        key, device, werte = fall
        with tempfile.TemporaryDirectory() as verzeichnis:
            pipeline, anlage = _pipeline(Path(verzeichnis), key, device=device)
            for lever in _hebel(key):
                low, high = TABELLE[key][0][lever]
                bereich = pipeline.range_of(lever)
                assert low <= bereich[0] <= bereich[1] <= high, (lever, device[lever], bereich)
                for art in _arten(device[lever]):
                    zaehler[art] += 1
            _schreiben(pipeline, werte)
            _alle_in_den_lokalen_grenzen(key, anlage.writes)
            zaehler["geschrieben"] += len(anlage.writes)

    pruefen()
    assert all(anzahl > 0 for anzahl in zaehler.values()), zaehler  # Gegenprobe je Art


# --- ueber die echte Laufzeit wie handle_setpoints (F-client-kern-47) ---

#: Wert eines Attributs min/max der HA-Entity (FakeHa liefert str(wert)): Zahl, nicht endlich, Text, riesig
ATTRIBUT = (st.floats(allow_nan=True, allow_infinity=True) | st.floats(-60, 60) | st.integers(-50, 50)
            | st.sampled_from(["nan", "inf", "-inf", "1e400", "x", ""]))
LAUFZEIT_WERT = st.floats(-1e4, 1e4) | st.floats(-60, 60) | st.integers(-50, 50) | st.sampled_from(
    [math.inf, -math.inf, math.nan, *RIESEN])


@pytest.mark.parametrize("hebelsatz", sorted(SAETZE))
def test_serverantwort_mit_beliebigen_schrittweiten_und_anlagenbereichen_schreibt_nur_in_den_lokalen_grenzen(hebelsatz):
    """Laufzeit wie produktiv (Konfigurationsdokument, HaHost, Worker, handle_setpoints, Pipeline): Schrittweiten 0,05
    bis 5 in den Bindungsparametern, Wertebereiche der Anlage als Attribute min/max (auch NaN, ±Inf, Text, Minimum ueber
    Maximum) und beliebige Hebelwerte (auch ±Inf, NaN, 2**1100): jeder Schreibwert seit dem Start liegt in der lokalen
    Grenze, im Regel-Worker steht kein Fehler."""
    optionen, zustand, entities = SAETZE[hebelsatz]
    bereiche = TABELLE[(hebelsatz, HK)][0]
    erwartet = tuple(sorted(set(entities.values()) - {"min_flow"}))
    alle = _hebel((hebelsatz, HK))
    zaehler = {"angenommen": 0, "geschrieben": 0}

    @given(st.fixed_dictionaries({lever: SCHRITT for lever in alle}),
           st.fixed_dictionaries({entity: st.none() | st.tuples(ATTRIBUT, ATTRIBUT) for entity in sorted(entities)}),
           st.fixed_dictionaries({lever: st.floats(-60, 60) for lever in erwartet})
           | st.fixed_dictionaries({lever: LAUFZEIT_WERT for lever in erwartet}))
    def pruefen(schritte, attribute, levers):
        with tempfile.TemporaryDirectory() as verzeichnis, pytest.MonkeyPatch.context() as mp:
            env = _frische_umgebung(Path(verzeichnis), mp)
            env.ha.states.update(zustand)
            for entity, paar in attribute.items():
                if paar is not None:
                    env.ha.states[f"{entity}::min"], env.ha.states[f"{entity}::max"] = paar
            parameter_neu = parameter(hebelsatz, schritte=schritte,
                                      toleranzen={lever: s / 2 for lever, s in schritte.items()})
            log = _Fehlerlog()
            worker_log = logging.getLogger("smartheat_runtime.worker")
            worker_log.addHandler(log)
            try:
                bridge = _start(env, **optionen, bindungsparameter=parameter_neu)
                _ha_verbunden(env, bridge)
                snapshot = _mqtt(env).snapshots[-1]
                vorher = len(env.ha.writes)
                antwort = {"seq": snapshot["seq"], "schema": 4, "ts": "x", "status": "ok", "reason": None,
                           "levers": levers, "learned": None}
                env.geraet.channel.setpoints(json.loads(json.dumps(antwort)))
                bridge.worker.run_pending()
            finally:
                worker_log.removeHandler(log)
            assert log.eintraege == []
            for entity, wert in env.ha.writes:  # alle Schreibwerte seit dem Start
                if entity in NICHT_NUMERISCH:
                    continue
                assert entity in entities, entity
                low, high = bereiche[entities[entity]]
                assert low <= wert <= high, (entity, wert, schritte, attribute)
            gueltig = sorted(levers) == sorted(snapshot["levers"]) and all(_endlich(w) for w in levers.values())
            if not gueltig:
                assert env.ha.writes[vorher:] == []  # ungueltige Antwort: nichts geschrieben
            zaehler["angenommen"] += int(gueltig)
            zaehler["geschrieben"] += len(env.ha.writes[vorher:])

    pruefen()
    # Gegenprobe wie test_property_geraet.py: gueltige Antworten wurden angenommen und geschrieben
    assert zaehler["angenommen"] > 0 and zaehler["geschrieben"] > 0, zaehler


# --- config_check.pruefen (F-client-kern-04) ---

VORLAGEN = {cc.HOST_GATEWAY: gateway_konfiguration(), cc.HOST_HA: ha_konfiguration()}
RASTUNGEN = {cc.HOST_GATEWAY: Rastung("viessmann_vicare", HK), cc.HOST_HA: Rastung("vaillant_vrc720", HK)}


@pytest.mark.parametrize("host", sorted(VORLAGEN))
def test_pruefen_liefert_fuer_jede_eingabe_eine_ablehnung_des_vertrags_oder_none_und_wirft_nie(host):
    """Beliebige Konfiguration (ein beliebiger Wert an jeder Stelle der gueltigen Vorlage, fehlende Stelle, fremde
    Felder, falsche Typen, NaN, ±Inf, 10**400, Unicode mit Surrogat; oder ein beliebiger JSON-Wert): pruefen wirft nie
    und liefert None oder eine Ablehnung mit Grund aus GRUENDE und details in der Form des Vertrags."""
    vorlage = VORLAGEN[host]
    stellen = [pfad for pfad in pfade(vorlage) if pfad]
    zaehler = {"abgelehnt": 0}
    assert cc.pruefen(vorlage, RASTUNGEN[host], host=host) is None  # Gegenprobe: die Vorlage selbst ist gueltig

    @given(st.one_of(
        st.tuples(st.sampled_from(stellen), JSON_WERT | st.just(WEG)).map(lambda s: ersetze(vorlage, *s)),
        st.builds(lambda fremd: {**vorlage, **fremd}, st.dictionaries(st.text(max_size=12), JSON_WERT, max_size=3)),
        JSON_WERT), st.sampled_from([None, RASTUNGEN[host]]))
    def pruefen(neu, rastung):
        ergebnis = cc.pruefen(neu, rastung, host=host)
        if ergebnis is None:
            return
        assert isinstance(ergebnis, cc.Ablehnung) and ergebnis.grund in cc.GRUENDE, ergebnis
        assert fehler.gueltig(ergebnis.details), ergebnis
        zaehler["abgelehnt"] += 1

    pruefen()
    assert zaehler["abgelehnt"] > 0
