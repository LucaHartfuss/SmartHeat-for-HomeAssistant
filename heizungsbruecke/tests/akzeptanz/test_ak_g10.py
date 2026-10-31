"""Massenwirkung: was immer der Server antwortet, die Anlage bekommt nie einen Wert ausserhalb der lokalen Grenzen, und
der Regel-Worker laeuft weiter. 1000 feindliche Antworten insgesamt, verteilt auf die acht Saetze (feste Saat), je
Satz vollstaendig die Sonderwerte und jede Grenze um einen Schritt ueber- und unterschritten. Jede Antwort geht auf einen
offenen Tick. Die Grenzen und Schritte stehen hier woertlich, nie aus dem Produktcode."""
import math
import random

import pytest
from anlagen import ALLE, hebel_von, schreibziel

INF = math.inf
#: lokale Grenzen (untere, obere) je (Hebelsatz, Verteilsystem) und Hebel (Regel 4), woertlich; min_flow leitet der Client ab
BEREICHE = {
    ("vaillant_vrc720", "Heizkoerper"): {"curve": (0.4, 1.5), "room_setpoint": (15.0, 25.0), "heat_limit": (5.0, 23.0),
                                         "min_flow": (20.0, 30.0)},
    ("weishaupt_wwp", "Heizkoerper"): {"curve": (0.30, 1.00), "room_setpoint": (16.0, 25.0), "heat_limit": (5.0, 23.0)},
    ("weishaupt_wwp_basis", "Heizkoerper"): {"room_setpoint": (16.0, 25.0)},
    ("viessmann_vicare", "Heizkoerper"): {"curve": (0.4, 1.4), "level": (-13.0, 3.0), "room_setpoint": (15.0, 25.0)},
    ("vaillant_vrc720", "Fussbodenheizung"): {"curve": (0.10, 0.35), "room_setpoint": (15.0, 24.0),
                                              "heat_limit": (5.0, 20.0), "min_flow": (20.0, 30.0)},
    ("weishaupt_wwp", "Fussbodenheizung"): {"curve": (0.10, 0.50), "room_setpoint": (16.0, 24.0),
                                            "heat_limit": (5.0, 20.0)},
    ("weishaupt_wwp_basis", "Fussbodenheizung"): {"room_setpoint": (16.0, 24.0)},
    ("viessmann_vicare", "Fussbodenheizung"): {"curve": (0.2, 0.4), "level": (-5.0, 3.0), "room_setpoint": (15.0, 24.0)},
}
#: Schrittweite je Hebelsatz und Hebel, woertlich
SCHRITTE = {
    "vaillant_vrc720": {"curve": 0.05, "room_setpoint": 0.5, "heat_limit": 0.1, "min_flow": 0.1},
    "weishaupt_wwp": {"curve": 0.05, "room_setpoint": 0.5, "heat_limit": 0.5},
    "weishaupt_wwp_basis": {"room_setpoint": 0.5},
    "viessmann_vicare": {"curve": 0.1, "level": 1.0, "room_setpoint": 1.0},
}
GESAMT = 1000  # feindliche Antworten insgesamt
JE_ANLAGE = GESAMT // len(ALLE)
SAAT = 20271031
TAG = 86400
SONDERWERTE = (math.nan, INF, -INF)
#: Werte, die kein Server senden duerfte: Extreme, falsche Typen, Text
UNSINN = (1e308, -1e308, 1.7976931348623157e308, 5e-324, -0.0, 0.0, 10**30, -(10**30), 2**63, True, False, None, "21.5",
          "NaN", "Infinity", [], [21.0], {}, {"wert": 1.0})
HUELLEN = ({}, {"fremd": 1}, {"learned": {"alles": [1, 2, 3]}}, {"reason": "x" * 400}, {"seq2": "fremd", "ts": 7})


def _mitte(bereich: tuple[float, float], schritt: float) -> float:
    return round(round((bereich[0] + bereich[1]) / 2 / schritt) * schritt, 6)


def _zufallswert(rng: random.Random, untere: float, obere: float, schritt: float):
    """Ein feindlicher Wert: Sonderwert, Unsinn, Zufall weit ausserhalb oder innerhalb, knapp ueber einer Grenze, riesig."""
    weite = obere - untere
    return rng.choice((
        lambda: rng.choice(SONDERWERTE), lambda: rng.choice(UNSINN),
        lambda: rng.uniform(untere - 5 * weite, obere + 5 * weite), lambda: rng.uniform(untere, obere),
        lambda: untere - rng.choice((1, 2, 3)) * schritt, lambda: obere + rng.choice((1, 2, 3)) * schritt,
        lambda: rng.choice((-1, 1)) * 10 ** rng.randint(3, 300) * rng.random(),
    ))()


def _antworten(satz: str, verteilsystem: str, rng: random.Random):
    """Genau JE_ANLAGE Antworten (ohne seq): erst die Pflichtfaelle, dann zufaellige, immer genau die Hebel des Satzes
    oder ein davon abweichender Satz."""
    bereiche, schritte = BEREICHE[(satz, verteilsystem)], SCHRITTE[satz]
    hebel = hebel_von(satz)
    mitte = {name: _mitte(bereiche[name], schritte[name]) for name in hebel}
    pflicht = []
    for name in hebel:
        untere, obere = bereiche[name]
        for wert in (*SONDERWERTE, untere - schritte[name], obere + schritte[name]):
            pflicht.append({"status": "ok", "levers": {**mitte, name: wert}})
    zufall = []
    while len(pflicht) + len(zufall) < JE_ANLAGE:
        levers = {}
        for name in hebel:
            untere, obere = bereiche[name]
            levers[name] = _zufallswert(rng, untere, obere, schritte[name])
        form = rng.random()
        if form < 0.1 and len(levers) > 1:
            del levers[rng.choice(sorted(levers))]  # ein Hebel fehlt
        elif form < 0.2:
            levers[rng.choice(("min_flow", "fremd", "heat_limit_x", "level"))] = rng.choice(UNSINN)  # fremdes Feld
        elif form < 0.25:
            levers = rng.choice((None, [], "alle", 5, [1.0, 2.0]))  # gar kein Objekt
        antwort = {"status": rng.choice(("ok", "ok", "ok", "skipped", "rejected", "unbekannt", None)), "levers": levers}
        antwort.update(rng.choice(HUELLEN))
        if rng.random() < 0.1:
            antwort["schema"] = rng.choice((3, 5, "4", 4.0, None, True))
        zufall.append(antwort)
    return pflicht + zufall


#: Hilfswerte, die der Client neben den Hebeln fuehrt (Betriebsart, Komfort- und Absenkwert); sie sind kein Hebel
HILFSZIELE = {"number.komfort", "number.absenk", "select.betriebsart", "climate.heizkreis::preset_mode", "climate.heizkreis"}


def _geschrieben(anlage, satz: str, raumsoll: tuple[float, float]) -> dict[str, list]:
    """Alle Schreibwerte je Hebel; ausser den Hilfszielen sind nur Schreibziele der Hebel erlaubt, mit endlichen Zahlen.
    Komfort- und Absenkwert liegen im Bereich des Raum-Solls (sie sind Raum-Soll-Werte)."""
    ziele = {schreibziel(satz, name): name for name in (*hebel_von(satz), *(["min_flow"] if satz.startswith("vaillant") else []))}
    werte: dict[str, list] = {name: [] for name in ziele.values()}
    for entity, wert, _ in anlage.ha.schreibvorgaenge:
        if entity in HILFSZIELE:
            assert isinstance(wert, str) or math.isfinite(wert), (entity, wert)
            if entity in ("number.komfort", "number.absenk"):
                assert raumsoll[0] <= wert <= raumsoll[1], (entity, wert)
            continue
        assert entity in ziele, f"Schreibaufruf auf {entity}"
        werte[ziele[entity]].append(wert)
    return werte


@pytest.mark.akzeptanz("AK-G10-01")
@pytest.mark.parametrize("satz,verteilsystem", ALLE, ids=[f"{s}-{v}" for s, v in ALLE])
def test_ak_g10_01_feindliche_antworten_bleiben_in_den_grenzen(welt, satz, verteilsystem):
    bereiche = BEREICHE[(satz, verteilsystem)]
    assert set(hebel_von(satz)) <= set(bereiche)
    rng = random.Random(SAAT + ALLE.index((satz, verteilsystem)))
    antworten = _antworten(satz, verteilsystem, rng)
    assert len(antworten) == JE_ANLAGE
    anlage = welt(satz, verteilsystem)
    anlage.starten()
    anlage.raum(22.0)  # warm: kein Boost, kein Notfall-Boost
    beantwortet: set[str] = set()
    for nr, antwort in enumerate(antworten):
        anlage.wunsch(21.0 if nr % 2 else 21.5)  # jeder Wunschwechsel gibt einen Tick
        anlage.runde(2)
        seq = anlage.link.of("up/snapshot")[-1]["seq"]
        assert seq not in beantwortet, f"Antwort {nr}: kein offener Tick"  # ein Tick, auf den noch nichts kam
        beantwortet.add(seq)
        anlage.link.empfangen("down/setpoints", {"schema": 4, "seq": seq, "ts": "x", **antwort})
        anlage.runde(2)
        anlage.raum(22.0)
        anlage.runde(TAG)  # ein neuer Tag: das Tagesbudget der Weishaupt-Regler und die Abstaende gelten wieder
    werte = _geschrieben(anlage, satz, bereiche["room_setpoint"])
    for name, liste in werte.items():
        untere, obere = bereiche[name]
        assert all(isinstance(w, float) and untere <= w <= obere for w in liste), (name, [w for w in liste if not untere <= w <= obere])
    for name in hebel_von(satz):  # die Antworten ueber den Grenzen kamen an und wurden genau auf die Grenzen gebracht
        assert min(werte[name]) == bereiche[name][0] and max(werte[name]) == bereiche[name][1], (name, set(werte[name]))
    # der Regel-Worker laeuft weiter: eine gueltige Antwort auf den naechsten offenen Tick wird geschrieben
    anlage.wunsch(21.0)
    anlage.runde(2)
    ziel = {name: _mitte(bereiche[name], SCHRITTE[satz][name]) for name in hebel_von(satz)}
    anlage.server_antwortet(ziel)
    anlage.runde(2)
    ist = {entity: wert for entity, wert, _ in anlage.ha.schreibvorgaenge}
    assert {name: ist[schreibziel(satz, name)] for name in ziel} == ziel
