"""Zeitquelle des Gerätekerns (smartheat_core/zeitquelle.py): ohne Variablen die Systemzeit, mit Testkennung und Versatz
eine verschobene fachliche Wanduhr; jede andere Lage verhindert den Start des Prozesses. Die Umgebung wird beim Import
gelesen, deshalb läuft jeder Fall in einem eigenen Unterprozess."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PYTHONPFAD = os.pathsep.join(str(REPO / pfad) for pfad in ("heizungsbruecke/src", "gateway/src"))
KENNUNG = "SMARTHEAT_TESTKENNUNG"
VERSATZ = "SMARTHEAT_ZEITRAFFER_VERSATZ_S"
MELDUNG = "Zeitraffer ohne Testkennung oder ungültig: Start verweigert"
ZWEI_JAHRE = "63072000"
GEHEIMER_WERT = "kennung-4711"


def lauf(code: str, **umgebung: str) -> subprocess.CompletedProcess[str]:
    """Führt `code` in einem frischen Interpreter aus; von der Umgebung bleibt nichts aus dem Präfix der Prüfung stehen."""
    basis = {k: v for k, v in os.environ.items() if not k.startswith("SMARTHEAT_")}
    basis["PYTHONPATH"] = PYTHONPFAD
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                          env={**basis, **umgebung})


def gescheitert(ergebnis: subprocess.CompletedProcess[str], *verboten: str) -> None:
    assert ergebnis.returncode != 0
    assert "RuntimeError" in ergebnis.stderr and MELDUNG in ergebnis.stderr, ergebnis.stderr
    assert "SMARTHEAT_" not in ergebnis.stderr, "die Meldung nennt einen Variablennamen"
    for wert in verboten:
        assert wert not in ergebnis.stderr, "die Meldung nennt einen Wert"


IMPORT = "import smartheat_core.zeitquelle\n"
ABWEICHUNG = (
    "import time\n"
    "from smartheat_core import wallclock, zeitquelle\n"
    "vor = time.time()\n"
    "w = wallclock.now()\n"
    "nach = time.time()\n"
    "print(w.timestamp() - vor, w.timestamp() - nach, w.tzinfo is not None)\n"
)


def abweichung(ergebnis: subprocess.CompletedProcess[str]) -> tuple[float, float, str]:
    assert ergebnis.returncode == 0, ergebnis.stderr
    vor, nach, aware = ergebnis.stdout.split()
    return float(vor), float(nach), aware


def test_ohne_variablen_ist_die_wanduhr_die_systemzeit_und_der_import_still():
    ergebnis = lauf(ABWEICHUNG)
    vor, nach, aware = abweichung(ergebnis)
    assert aware == "True"
    assert -1 <= vor < 5 and -5 < nach <= 1
    assert ergebnis.stderr == ""


def test_testkennung_allein_verschiebt_nichts_und_warnt_nicht():
    ergebnis = lauf(ABWEICHUNG, **{KENNUNG: GEHEIMER_WERT})
    vor, nach, _ = abweichung(ergebnis)
    assert -1 <= vor < 5 and -5 < nach <= 1
    assert ergebnis.stderr == ""


def test_mit_testkennung_und_versatz_liegt_die_wanduhr_um_den_versatz_vor_der_echten_zeit():
    ergebnis = lauf(ABWEICHUNG, **{KENNUNG: GEHEIMER_WERT, VERSATZ: "86400.5"})
    vor, nach, aware = abweichung(ergebnis)
    assert aware == "True"
    assert 86399.5 <= vor < 86405.5 and 86395.5 < nach <= 86401.5


def test_die_api_liefert_fachliche_und_echte_epoche():
    code = (
        "import time\n"
        "from smartheat_core import zeitquelle\n"
        "e = zeitquelle.echte_epoche()\n"
        "f = zeitquelle.fachliche_epoche()\n"
        "w = zeitquelle.fachliche_wanduhr()\n"
        "print(e - time.time(), f - e, w.timestamp() - f, w.tzinfo is not None)\n"
    )
    ergebnis = lauf(code, **{KENNUNG: GEHEIMER_WERT, VERSATZ: "3600"})
    assert ergebnis.returncode == 0, ergebnis.stderr
    echt_diff, versatz, wand_diff, aware = ergebnis.stdout.split()
    assert abs(float(echt_diff)) < 5 and 3599 <= float(versatz) < 3605 and abs(float(wand_diff)) < 5
    assert aware == "True"


@pytest.mark.parametrize("umgebung,versatz", [
    ({}, 0.0),
    ({KENNUNG: GEHEIMER_WERT}, 0.0),
    ({KENNUNG: GEHEIMER_WERT, VERSATZ: "0"}, 0.0),
    ({KENNUNG: GEHEIMER_WERT, VERSATZ: "0.25"}, 0.25),
    ({KENNUNG: GEHEIMER_WERT, VERSATZ: "3600"}, 3600.0),
    ({KENNUNG: GEHEIMER_WERT, VERSATZ: ZWEI_JAHRE}, 63072000.0),
])
def test_der_versatz_wirkt_genau_und_nur_vorwaerts(umgebung, versatz):
    """Die fachliche Epoche liegt um genau den Versatz vor der echten (die Messung liegt höchstens Millisekunden daneben)."""
    code = (
        "from smartheat_core import zeitquelle\n"
        "f = zeitquelle.fachliche_epoche()\n"
        "e = zeitquelle.echte_epoche()\n"
        "print(f - e)\n"
    )
    ergebnis = lauf(code, **umgebung)
    assert ergebnis.returncode == 0, ergebnis.stderr
    assert versatz - 0.1 < float(ergebnis.stdout) <= versatz + 1e-6


def test_die_echte_epoche_bleibt_unverschoben():
    code = "import time\nfrom smartheat_core import zeitquelle\nprint(zeitquelle.echte_epoche() - time.time())\n"
    ergebnis = lauf(code, **{KENNUNG: GEHEIMER_WERT, VERSATZ: ZWEI_JAHRE})
    assert ergebnis.returncode == 0, ergebnis.stderr
    assert abs(float(ergebnis.stdout)) < 5


def test_die_umgebung_wird_nur_beim_import_gelesen():
    code = (
        "import os, time\n"
        "from smartheat_core import zeitquelle\n"
        "os.environ['SMARTHEAT_ZEITRAFFER_VERSATZ_S'] = '1000000'\n"
        "print(zeitquelle.fachliche_epoche() - time.time())\n"
    )
    ergebnis = lauf(code, **{KENNUNG: GEHEIMER_WERT, VERSATZ: "100"})
    assert ergebnis.returncode == 0, ergebnis.stderr
    assert 99 <= float(ergebnis.stdout) < 105


def test_die_warnung_kommt_einmal_und_ohne_werte():
    code = "import smartheat_core.zeitquelle\nfrom smartheat_core import wallclock\nimport smartheat_core.zeitquelle\n"
    ergebnis = lauf(code, **{KENNUNG: GEHEIMER_WERT, VERSATZ: "86400.5"})
    assert ergebnis.returncode == 0, ergebnis.stderr
    assert ergebnis.stderr.strip().splitlines() == ["Zeitraffer aktiv"]


@pytest.mark.parametrize("versatz", ["0", "0.0", "1", "0.5", "31.25", "86400", ZWEI_JAHRE, ZWEI_JAHRE + ".0"])
def test_gueltige_versaetze_starten(versatz):
    ergebnis = lauf(IMPORT, **{KENNUNG: GEHEIMER_WERT, VERSATZ: versatz})
    assert ergebnis.returncode == 0, ergebnis.stderr


@pytest.mark.parametrize("versatz", [
    "", " ", "-1", "-0", "+5", "1e3", "1E3", "1_0", ".5", "5.", "0x10", "abc", "1,5", " 1", "1 ", "1\n", "nan", "inf",
    "٣", "63072001", ZWEI_JAHRE + ".5", ZWEI_JAHRE + ".0001", "9" * 400,
])
def test_ungueltige_versaetze_verhindern_den_start_ohne_den_wert_zu_nennen(versatz):
    wert = versatz.strip()
    # kurze Werte kämen zufällig im Traceback vor (Zeilennummern); lange dürfen nie erscheinen
    gescheitert(lauf(IMPORT, **{KENNUNG: GEHEIMER_WERT, VERSATZ: versatz}), GEHEIMER_WERT, *([wert] if len(wert) >= 6 else []))


def test_die_versatzgrenze_gilt_beidseitig():
    assert lauf(IMPORT, **{KENNUNG: GEHEIMER_WERT, VERSATZ: ZWEI_JAHRE}).returncode == 0
    gescheitert(lauf(IMPORT, **{KENNUNG: GEHEIMER_WERT, VERSATZ: "63072000.01"}))
    assert lauf(IMPORT, **{KENNUNG: GEHEIMER_WERT, VERSATZ: "0"}).returncode == 0
    gescheitert(lauf(IMPORT, **{KENNUNG: GEHEIMER_WERT, VERSATZ: "-0.01"}))


def test_ohne_testkennung_startet_der_prozess_nicht():
    gescheitert(lauf(IMPORT, **{VERSATZ: "86400"}), "86400")


@pytest.mark.parametrize("kennung", ["", " ", "   ", "\t", "\n", " \t\n "])
def test_eine_leere_testkennung_zaehlt_als_nicht_gesetzt(kennung):
    gescheitert(lauf(IMPORT, **{KENNUNG: kennung, VERSATZ: "86400"}), "86400")


@pytest.mark.parametrize("name,wert", [
    ("SMARTHEAT_ZEITRAFFER_FAKTOR", "2"),
    ("SMARTHEAT_ZEITRAFFER_FAKTOR", "1"),
    ("SMARTHEAT_ZEITRAFFER_EPOCHE", "1700000000"),
    ("SMARTHEAT_ZEITRAFFER_EPOCHE", ""),
    ("SMARTHEAT_ZEITRAFFER_", "1"),
    ("SMARTHEAT_ZEITRAFFER_VERSATZ", "1"),
    ("SMARTHEAT_ZEITRAFFER_VERSATZ_S_", "1"),
    ("SMARTHEAT_ZEITRAFFER_UNBEKANNT", "1"),
])
def test_andere_variablen_des_praefixes_verhindern_den_start(name, wert):
    gescheitert(lauf(IMPORT, **{KENNUNG: GEHEIMER_WERT, VERSATZ: "86400", name: wert}), GEHEIMER_WERT)
    gescheitert(lauf(IMPORT, **{KENNUNG: GEHEIMER_WERT, name: wert}), GEHEIMER_WERT)


def test_eine_andere_variable_ohne_testkennung_verhindert_den_start():
    gescheitert(lauf(IMPORT, SMARTHEAT_ZEITRAFFER_FAKTOR="2"))


def test_fremde_variablen_ohne_das_praefix_stoeren_nicht():
    ergebnis = lauf(IMPORT, SMARTHEAT_ZEITRAFFER="1", SMARTHEAT_ZEIT="1", ZEITRAFFER_VERSATZ_S="1")
    assert ergebnis.returncode == 0, ergebnis.stderr
    assert ergebnis.stderr == ""


@pytest.mark.parametrize("modul", ["heizungsbruecke.__main__", "smartheat_gateway.runtime_main"])
def test_beide_einstiege_scheitern_beim_import_ohne_testkennung(modul):
    gescheitert(lauf(f"import {modul}\n", **{VERSATZ: "86400"}), "86400")
    gescheitert(lauf(f"import {modul}\n", **{KENNUNG: " ", "SMARTHEAT_ZEITRAFFER_FAKTOR": "2"}))


@pytest.mark.parametrize("modul", ["heizungsbruecke.__main__", "smartheat_gateway.runtime_main"])
def test_beide_einstiege_starten_mit_testkennung_und_warnen_einmal(modul):
    ergebnis = lauf(f"import {modul}\n", **{KENNUNG: GEHEIMER_WERT, VERSATZ: "86400"})
    assert ergebnis.returncode == 0, ergebnis.stderr
    assert ergebnis.stderr.count("Zeitraffer aktiv") == 1
    assert GEHEIMER_WERT not in ergebnis.stderr and "86400" not in ergebnis.stderr
