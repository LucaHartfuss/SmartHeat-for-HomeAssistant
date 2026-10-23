"""Profile von hypothesis: `heizungsbruecke/tests/conftest.py` registriert
`schnell` (Standard) und `voll` und waehlt per `HYPOTHESIS_PROFILE`; kein Test unter `tests/` und `testhilfen/` setzt
eigene `max_examples`/`derandomize`. Die Profilwahl prueft je Fall ein eigener pytest-Prozess mit einer kleinen
Testdatei, die die Werte zusichert und die conftest.py dieses Ordners als Plugin laedt (`-p conftest`)."""
import ast
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings

TESTS = Path(__file__).resolve().parent
PAKET = TESTS.parent  # heizungsbruecke
REPO = PAKET.parent
PYTHONPATH = [TESTS, PAKET / "src", PAKET / "testhilfen"]  # wie pythonpath in pyproject.toml, dazu tests/
# Untergrenze der gelesenen Dateien je Ordner (heute 117 *.py unter tests/ ausser conftest.py, 111 = 116 bei der
# Einfuehrung minus 5; 3 unter testhilfen/, mindestens 1): ein leerer oder verschobener Ordner ist rot.
MINDESTENS_DATEIEN = {TESTS: 111, PAKET / "testhilfen": 1}


def _lauf(tmp_path: Path, profil: str | None, testtext: str, *, ci: bool = False) -> subprocess.CompletedProcess:
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")  # kein ini der Umgebung, rootdir = tmp_path
    (tmp_path / "test_profilwerte.py").write_text(testtext, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(str(p) for p in PYTHONPATH)}
    env.pop("HYPOTHESIS_PROFILE", None)
    for name in ("CI", "TF_BUILD", "GITHUB_ACTIONS"):  # hypothesis waehlt in einer CI-Umgebung eigene Vorgaben
        env.pop(name, None)
    if ci:
        env["CI"] = "true"
    if profil is not None:
        env["HYPOTHESIS_PROFILE"] = profil
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "conftest",
                           "test_profilwerte.py"], cwd=tmp_path, env=env, capture_output=True, text=True, check=False,
                          timeout=120)


_SCHNELL = """from hypothesis import HealthCheck, settings


def test_werte():
    s = settings.default
    assert s.max_examples == 50
    assert s.derandomize is True
    assert s.database is None
    assert s.deadline is None
    assert list(s.suppress_health_check) == [HealthCheck.too_slow]
    assert s.print_blob is False
"""
_VOLL = """from hypothesis import HealthCheck, settings


def test_werte():
    s = settings.default
    assert s.max_examples == 500
    assert s.derandomize is False
    assert s.database is None
    assert s.deadline is None
    assert list(s.suppress_health_check) == [HealthCheck.too_slow]
    assert s.print_blob is True
"""


def test_ohne_umgebung_gilt_das_profil_schnell(tmp_path):
    ergebnis = _lauf(tmp_path, None, _SCHNELL)
    assert ergebnis.returncode == 0, ergebnis.stdout + ergebnis.stderr
    assert "1 passed" in ergebnis.stdout


def test_profil_voll_hat_500_zufaellige_beispiele_mit_blob(tmp_path):
    ergebnis = _lauf(tmp_path, "voll", _VOLL)
    assert ergebnis.returncode == 0, ergebnis.stdout + ergebnis.stderr
    assert "1 passed" in ergebnis.stdout


@pytest.mark.parametrize(("profil", "testtext"), [(None, _SCHNELL), ("voll", _VOLL)], ids=["schnell", "voll"])
def test_die_profile_gelten_auch_in_einer_ci_umgebung(tmp_path, profil, testtext):
    """Mit `CI` gesetzt (GitHub Actions) gelten dieselben Werte wie lokal: hypothesis setzt dort sonst `print_blob`."""
    ergebnis = _lauf(tmp_path, profil, testtext, ci=True)
    assert ergebnis.returncode == 0, ergebnis.stdout + ergebnis.stderr
    assert "1 passed" in ergebnis.stdout


def test_profil_schnell_ausdruecklich_gewaehlt(tmp_path):
    ergebnis = _lauf(tmp_path, "schnell", _SCHNELL)
    assert ergebnis.returncode == 0, ergebnis.stdout + ergebnis.stderr


def test_unbekanntes_profil_endet_mit_invalid_argument(tmp_path):
    ergebnis = _lauf(tmp_path, "gibtsnicht", "def test_nie():\n    pass\n")
    assert ergebnis.returncode != 0
    assert "InvalidArgument" in ergebnis.stdout + ergebnis.stderr
    assert "1 passed" not in ergebnis.stdout


def test_diese_suite_laeuft_mit_dem_gewaehlten_profil():
    """Im laufenden Prozess: die Beispielzahl folgt HYPOTHESIS_PROFILE (ohne Umgebung schnell)."""
    erwartet = {"schnell": (50, True), "voll": (500, False)}[os.environ.get("HYPOTHESIS_PROFILE", "schnell")]
    assert (settings.default.max_examples, settings.default.derandomize) == erwartet
    assert HealthCheck.too_slow in settings.default.suppress_health_check


# --- Waechter: keine Einstellungen am einzelnen Test ---------------------------------------------------------------


def eigene_einstellungen(quelltext: str) -> list[int]:
    """Zeilen mit einem Aufruf von hypothesis' `settings` (auch per Alias oder als `<modul>.settings`), der
    `max_examples` oder `derandomize` setzt oder Schluessel per `**` uebergibt (nicht pruefbar), und jedes
    `register_profile`/`load_profile` (ein Profil ausserhalb der obersten conftest.py ueberschriebe die Wahl des
    ganzen Prozesses)."""
    baum = ast.parse(quelltext)
    namen = {"settings"}
    for knoten in ast.walk(baum):
        if isinstance(knoten, ast.ImportFrom) and (knoten.module or "").split(".")[0] == "hypothesis":
            namen |= {alias.asname for alias in knoten.names if alias.name == "settings" and alias.asname}
    zeilen = []
    for knoten in ast.walk(baum):
        if not isinstance(knoten, ast.Call):
            continue
        funktion = knoten.func
        ist_settings = (isinstance(funktion, ast.Name) and funktion.id in namen) or (
            isinstance(funktion, ast.Attribute) and funktion.attr == "settings")
        profil = isinstance(funktion, ast.Attribute) and funktion.attr in ("register_profile", "load_profile")
        if profil or (ist_settings and any(k.arg in ("max_examples", "derandomize", None) for k in knoten.keywords)):
            zeilen.append(knoten.lineno)
    return zeilen


@pytest.mark.parametrize("quelltext", [
    "from hypothesis import settings\n@settings(max_examples=10)\ndef test_x():\n    pass\n",
    "from hypothesis import settings\nEINSTELLUNG = settings(derandomize=True, database=None)\n",
    "from hypothesis import settings as s\nE = s(max_examples=5)\n",
    "import hypothesis\nE = hypothesis.settings(max_examples=5)\n",
    "from hypothesis import settings\nE = settings(settings.default, derandomize=False)\n",
    "from hypothesis import settings\nsettings.register_profile('eigen', deadline=None)\n",
    "from hypothesis import settings\nsettings.load_profile('voll')\n",
    "import hypothesis\nhypothesis.settings.register_profile('eigen')\n",
    "from hypothesis import settings\nE = settings(**{'max_examples': 5})\n",
], ids=["dekorator", "konstante", "alias", "modul", "mit_parent", "register_profile", "load_profile",
        "register_profile_modul", "schluessel_als_dict"])
def test_waechter_erkennt_eigene_einstellungen(quelltext):
    assert eigene_einstellungen(quelltext) != []


def test_waechter_laesst_andere_einstellungen_und_fremde_settings_durch():
    assert eigene_einstellungen("from hypothesis import settings\n@settings(deadline=None)\ndef test_x():\n"
                                "    pass\n") == []
    assert eigene_einstellungen("def f(settings):\n    return settings\n") == []


def test_kein_test_setzt_eigene_beispielzahl_oder_seed():
    befunde = []
    for ordner, mindestens in MINDESTENS_DATEIEN.items():
        dateien = sorted(p for p in ordner.rglob("*.py") if p != TESTS / "conftest.py")
        assert len(dateien) >= mindestens, f"nur {len(dateien)} Dateien unter {ordner}"
        befunde += [f"{datei.relative_to(REPO)}:{zeile}" for datei in dateien
                    for zeile in eigene_einstellungen(datei.read_text(encoding="utf-8"))]
    assert befunde == []


# --- scripts/check.sh: --full waehlt das Profil voll ----------------------------------------------------------------


def _profil_beim_pytest(tmp_path: Path, *args: str, profil: str | None) -> list[str]:
    """HYPOTHESIS_PROFILE, das jeder pytest-Aufruf von check.sh sieht (Attrappe protokolliert nur `-m pytest`)."""
    tmp_path = Path(tempfile.mkdtemp(dir=tmp_path))
    protokoll = tmp_path / "profil.log"
    attrappe = tmp_path / "python-attrappe"
    attrappe.write_text('#!/bin/sh\n[ "$1 $2" = "-m pytest" ] && '
                        'echo "${HYPOTHESIS_PROFILE-ungesetzt}" >> "$PROTOKOLL"\nexit 0\n', encoding="utf-8")
    attrappe.chmod(0o755)
    env = {**os.environ, "PYTHON": str(attrappe), "PROTOKOLL": str(protokoll), "DEV_ROOT": str(tmp_path)}
    env.pop("HYPOTHESIS_PROFILE", None)
    if profil is not None:
        env["HYPOTHESIS_PROFILE"] = profil
    ergebnis = subprocess.run(["bash", str(REPO / "scripts" / "check.sh"), *args], env=env, capture_output=True,
                              text=True, check=False, timeout=60)
    assert ergebnis.returncode == 0, ergebnis.stdout + ergebnis.stderr
    return protokoll.read_text(encoding="utf-8").splitlines()


def test_check_sh_full_waehlt_das_profil_voll(tmp_path):
    """--full setzt HYPOTHESIS_PROFILE=voll fuer alle vier pytest-Laeufe (auch gegen einen anderen Wert der Umgebung);
    ohne --full reicht check.sh die Umgebung unveraendert weiter (die CI setzt sie je Stufe, ohne Wert gilt schnell)."""
    assert _profil_beim_pytest(tmp_path, "--full", "--only", "test", profil=None) == ["voll"] * 4
    assert _profil_beim_pytest(tmp_path, "--full", "--only", "test", profil="schnell") == ["voll"] * 4
    assert _profil_beim_pytest(tmp_path, "--only", "test", profil=None) == ["ungesetzt"] * 4
    assert _profil_beim_pytest(tmp_path, "--only", "test", profil="voll") == ["voll"] * 4


def _nennt_hypothesis(anforderungen) -> list[str]:
    return [a for a in anforderungen if re.match(r"\s*hypothesis\b", a, re.IGNORECASE)]


def test_geladen_ist_die_version_des_hash_locks():
    """Geladen ist genau die Version des Hash-Locks (Literal); zoege eine ungehashte
    Installation eine andere Version nach, waere das rot."""
    import hypothesis
    assert hypothesis.__version__ == "6.170.1"
    assert "hypothesis==6.170.1 \\\n" in (PAKET / "requirements-hypothesis.lock").read_text(encoding="utf-8")


def test_dev_abhaengigkeiten_nennen_hypothesis_nicht():
    """Hypothesis kommt nur aus dem Hash-Lock; `[dev]` (ungehasht installiert) und die
    uebrigen Extras von heizungsbruecke, gateway, gateway/host und gateway/release nennen es nicht."""
    gruppen = {}
    for pyproject in (PAKET, REPO / "gateway", REPO / "gateway" / "host", REPO / "gateway" / "release"):
        projekt = tomllib.loads((pyproject / "pyproject.toml").read_text(encoding="utf-8")).get("project", {})
        for gruppe, liste in projekt.get("optional-dependencies", {}).items():
            gruppen[f"{pyproject.relative_to(REPO)}[{gruppe}]"] = _nennt_hypothesis(liste)
    assert {"heizungsbruecke[dev]", "gateway[dev]"} <= set(gruppen)
    assert gruppen == {gruppe: [] for gruppe in gruppen}
    assert _nennt_hypothesis(["hypothesis>=6", "Hypothesis[cli]", " hypothesis==6.1"]) == [
        "hypothesis>=6", "Hypothesis[cli]", " hypothesis==6.1"]
    assert _nennt_hypothesis(["hypothesis-jsonschema", "pytest"]) == ["hypothesis-jsonschema"]
