"""Zeitraffer und Vertrauen in die Uhr (smartheat_core/zeitquelle.py, smartheat_runtime/uhr.py, abo.py): der Versatz
verschiebt nur die fachliche Wanduhr. Er ersetzt das Vertrauen in die Uhr nicht (ohne synchrone Meldung des Hosts ruht
sie) und entzieht es nicht (der Versatz ist kein Sprung gegen die monotone Uhr); ein echter Wanduhrsprung nimmt es auch
unter Zeitraffer. Die Umgebung wird beim Import gelesen, deshalb laeuft jeder Fall in einem eigenen Unterprozess mit
echter Wand- und monotoner Uhr; Testkennung und Versatz setzt der Test nur in der Umgebung des Unterprozesses."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PYTHONPFAD = os.pathsep.join(str(REPO / pfad) for pfad in ("heizungsbruecke/src", "gateway/src"))
TAG = 86400.0
EINUNDDREISSIG_TAGE = 31 * TAG

VORLAUF = """
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from smartheat_core import wallclock, zeitquelle
from smartheat_runtime.uhr import UhrPort, UhrVertrauen

SYNCHRON = sys.argv[1] == "ja"
ARG = sys.argv[2]
port = UhrPort(lambda: SYNCHRON, clock=time.monotonic)
vertrauen = UhrVertrauen(port.antwort, clock=time.monotonic)
"""

# Versatz der fachlichen gegen die echte Zeit, zwei Meldungen im Abstand, und der Sprung der Wanduhr gegen die monotone
# Uhr seit der Antwort des Hosts (ohne Antwort 0.0).
VERTRAUEN_OHNE_SPRUNG = VORLAUF + """
versatz = wallclock.now().timestamp() - time.time()
erste = vertrauen()
time.sleep(0.05)
zweite = vertrauen()
antwort = port.antwort()
sprung = 0.0
if antwort.wand is not None:
    sprung = (wallclock.now().timestamp() - antwort.wand.timestamp()) - (time.monotonic() - antwort.mono)
print(json.dumps([versatz, erste, zweite, sprung]))
"""

# Nach einer synchronen Meldung springt die Wanduhr zusaetzlich um ARG Sekunden (ARG ist signiert).
VERTRAUEN_MIT_SPRUNG = VORLAUF + """
vorher = vertrauen()
original = wallclock._now
wallclock._now = lambda: original() + timedelta(seconds=float(ARG))
nachher = vertrauen()
print(json.dumps([vorher, nachher, wallclock.UHR_SPRUNG_TOLERANZ_S]))
"""

# Fristende im Takt des lokalen Checks (abo.check_grace_end): die Frist begann in echter Zeit; ARG ist ein Ordner fuer die
# Datei des Abo-Zustands. finish_grace ist ersetzt und zaehlt, ob das Ende ausgeloest wurde.
ABO_FRIST = VORLAUF + """
from types import SimpleNamespace

from smartheat_runtime import abo, entitlement

beginn = datetime.fromtimestamp(zeitquelle.echte_epoche(), UTC).astimezone()
enden = []
entitlement.query = lambda config: entitlement.INACTIVE
abo.finish_grace = lambda rt, **kwargs: enden.append(kwargs) or True
abo._enter_idle = lambda rt: None
abo.report_restore = lambda notifier, ok: True
zustand = SimpleNamespace(abo_finished=False, abo_inactive_since=beginn)
rt = SimpleNamespace(store=SimpleNamespace(state=zustand), config=SimpleNamespace(entitlement_path=Path(ARG) / "e.json"),
                     uhr_vertrauen=vertrauen, notifier=None)
abo.check_grace_end(rt)
print(json.dumps([len(enden), zustand.abo_finished, zustand.abo_inactive_since == beginn]))
"""


def lauf(code: str, *, synchron: bool, arg: str = "0", versatz: float | None = None) -> list:
    """Fuehrt `code` in einem frischen Interpreter aus und liest dessen JSON-Zeile. Nur der Unterprozess bekommt die
    Testkennung und den Versatz; von der Umgebung bleibt nichts aus dem Praefix der Prueflaeufe stehen."""
    umgebung = {k: v for k, v in os.environ.items() if not k.startswith("SMARTHEAT_")}
    umgebung["PYTHONPATH"] = PYTHONPFAD
    if versatz is not None:
        umgebung["SMARTHEAT_TESTKENNUNG"] = "uhr-zeitraffer-test"
        umgebung["SMARTHEAT_ZEITRAFFER_VERSATZ_S"] = repr(versatz)
    ergebnis = subprocess.run([sys.executable, "-c", code, "ja" if synchron else "nein", arg], capture_output=True,
                              text=True, timeout=60, env=umgebung)
    assert ergebnis.returncode == 0, ergebnis.stderr
    return json.loads(ergebnis.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("versatz", [EINUNDDREISSIG_TAGE, 60.0])
def test_der_versatz_allein_gibt_kein_vertrauen_ohne_synchrone_meldung_ruht_die_uhr(versatz):
    gemessen, erste, zweite, _ = lauf(VERTRAUEN_OHNE_SPRUNG, synchron=False, versatz=versatz)
    assert abs(gemessen - versatz) < 5  # die fachliche Wanduhr steht wirklich um den Versatz vor (kein leerer Test)
    assert erste is False and zweite is False


@pytest.mark.parametrize("versatz", [EINUNDDREISSIG_TAGE, 60.0, 0.0])
def test_mit_synchroner_meldung_gilt_die_uhr_und_der_versatz_ist_kein_sprung(versatz):
    gemessen, erste, zweite, sprung = lauf(VERTRAUEN_OHNE_SPRUNG, synchron=True, versatz=versatz)
    assert abs(gemessen - versatz) < 5
    assert erste is True and zweite is True
    assert abs(sprung) < 1  # der Versatz ist konstant: gegen die monotone Uhr entsteht kein Sprung


@pytest.mark.parametrize("versatz", [EINUNDDREISSIG_TAGE, None])
@pytest.mark.parametrize("richtung", [1, -1])
def test_ein_wanduhrsprung_ueber_der_toleranz_nimmt_das_vertrauen_auch_unter_zeitraffer(versatz, richtung):
    toleranz = lauf(VERTRAUEN_MIT_SPRUNG, synchron=True, arg="0", versatz=versatz)[2]
    sprung = richtung * (toleranz + 60)
    vorher, nachher, _ = lauf(VERTRAUEN_MIT_SPRUNG, synchron=True, arg=repr(sprung), versatz=versatz)
    assert vorher is True and nachher is False


@pytest.mark.parametrize("richtung", [1, -1])
def test_ein_sprung_innerhalb_der_toleranz_haelt_das_vertrauen_unter_zeitraffer(richtung):
    toleranz = lauf(VERTRAUEN_MIT_SPRUNG, synchron=True, arg="0", versatz=EINUNDDREISSIG_TAGE)[2]
    vorher, nachher, _ = lauf(VERTRAUEN_MIT_SPRUNG, synchron=True, arg=repr(richtung * (toleranz - 60)),
                              versatz=EINUNDDREISSIG_TAGE)
    assert vorher is True and nachher is True


def test_abo_frist_endet_bei_31_tagen_versatz_und_synchroner_uhr(tmp_path):
    enden, beendet, beginn_gleich = lauf(ABO_FRIST, synchron=True, arg=str(tmp_path), versatz=EINUNDDREISSIG_TAGE)
    assert enden == 1 and beginn_gleich is True


def test_abo_frist_bleibt_bei_31_tagen_versatz_ohne_synchrone_uhr_offen(tmp_path):
    enden, beendet, beginn_gleich = lauf(ABO_FRIST, synchron=False, arg=str(tmp_path), versatz=EINUNDDREISSIG_TAGE)
    assert enden == 0 and beendet is False and beginn_gleich is True


@pytest.mark.parametrize("versatz", [None, 29 * TAG])
def test_abo_frist_endet_nicht_vor_den_30_tagen_auch_bei_synchroner_uhr(tmp_path, versatz):
    enden, beendet, _ = lauf(ABO_FRIST, synchron=True, arg=str(tmp_path), versatz=versatz)
    assert enden == 0 and beendet is False
