"""Waechter: Module, die gegen die Aussenwelt rechnen, benutzen die fachliche Uhr nie.

Die Signatur des Bootstraps, der Besitznachweis, die Hersteller-API, das Kontingent und die Zeitstempel von Zigbee2MQTT
brauchen die echte Zeit; wuerden sie die fachliche (verschobene) Zeit lesen, waeren Signaturen, Token und Kontingente
falsch. `test_zeit_zuordnung.py` erfasst nur Lesestellen der Systemuhr; dieser Test prueft die Gegenrichtung per AST: die
unten genannten Module (beim Einstieg nur die genannte Funktion) importieren und benutzen weder `wallclock` noch
`fachliche_epoche` noch `fachliche_wanduhr`, auch nicht ueber Alias, Sternimport, Attributzugriff, `getattr` oder
`importlib`. Module mit beiden Zeiten (Geraet und Host des Gateways) trennen ihre Uhren ueber getrennte Parameter und
stehen in `GEMISCHT`; ein neues Modul, das `echte_epoche` einfuehrt, muss hier eingetragen werden. Nicht erfasst: die
fachliche Zeit, die ein aufgerufenes fremdes Modul liest."""
import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
QUELLEN = (REPO / "heizungsbruecke" / "src", REPO / "gateway" / "src")
VERBOTEN = frozenset({"wallclock", "fachliche_epoche", "fachliche_wanduhr"})
DYNAMISCH = frozenset({"__import__", "import_module"})
# Module, in denen der Kern ueberhaupt Namen exportiert, die einen Sternimport gefaehrlich machen.
STERN_QUELLEN = frozenset({"smartheat_core", "zeitquelle"})

# Ganze Module (Pfad unter src) und ganze Verzeichnisse (alle *.py darin).
MODULE = [
    "smartheat_device/bootstrap.py",
    "smartheat_device/identity.py",
    "heizungsbruecke/geraet_nachweis.py",
    "smartheat_gateway/quota.py",
    "smartheat_gateway/zigbee.py",
]
VERZEICHNISSE = ["smartheat_gateway/drivers/vicare_cloud"]
# Nur die genannte Funktion des Moduls.
FUNKTIONEN = [("heizungsbruecke/__main__.py", "stdin_anhaengen")]
# Module, die echte und fachliche Zeit getrennt fuehren; die Trennung pruefen deren eigene Tests.
GEMISCHT = frozenset({
    "smartheat_core/zeitquelle.py",
    "smartheat_device/device.py",
    "smartheat_gateway/geraet_host.py",
})


def _komponenten(name: str | None) -> list[str]:
    return [teil for teil in (name or "").split(".") if teil]


def _verstoesse(quelle: str, funktion: str | None = None) -> list[tuple[int, str]]:
    """Verstoesse einer Quelle (Zeile, Art); mit `funktion` nur im Rumpf dieser Funktion (samt Vorgabewerten), die genau
    einmal vorkommen muss."""
    baum = ast.parse(quelle)
    # Namen, die ein verbotener Import in der Datei bindet (fuer die Pruefung einer einzelnen Funktion).
    alias: set[str] = set()
    for k in ast.walk(baum):
        if isinstance(k, ast.ImportFrom):
            for a in k.names:
                if a.name in VERBOTEN or VERBOTEN & set(_komponenten(k.module)):
                    alias.add(a.asname or a.name)
        elif isinstance(k, ast.Import):
            for a in k.names:
                if VERBOTEN & set(_komponenten(a.name)):
                    alias.add(a.asname or a.name.split(".")[0])
    wurzel: ast.AST = baum
    if funktion is not None:
        gefunden = [k for k in ast.walk(baum)
                    if isinstance(k, (ast.FunctionDef, ast.AsyncFunctionDef)) and k.name == funktion]
        if len(gefunden) != 1:
            return [(0, f"funktion {funktion!r} kommt {len(gefunden)} mal vor")]
        wurzel = gefunden[0]
    funde: list[tuple[int, str]] = []
    for k in ast.walk(wurzel):
        zeile = getattr(k, "lineno", 0)
        if isinstance(k, ast.Import):
            funde += [(zeile, f"import {a.name}") for a in k.names if VERBOTEN & set(_komponenten(a.name))]
        elif isinstance(k, ast.ImportFrom):
            if VERBOTEN & set(_komponenten(k.module)):
                funde.append((zeile, f"from {k.module} import ..."))
            for a in k.names:
                if a.name in VERBOTEN:
                    funde.append((zeile, f"from ... import {a.name}"))
                elif a.name == "*" and (not k.module or _komponenten(k.module)[-1] in STERN_QUELLEN):
                    funde.append((zeile, f"from {'.' * k.level}{k.module or ''} import *"))
        elif isinstance(k, ast.Name) and (k.id in VERBOTEN or k.id in alias):
            funde.append((zeile, f"name {k.id}"))
        elif isinstance(k, ast.Attribute) and k.attr in VERBOTEN:
            funde.append((zeile, f"attribut {k.attr}"))
        elif isinstance(k, ast.Constant) and isinstance(k.value, str) and re.fullmatch(r"[\w.]+", k.value) \
                and VERBOTEN & set(_komponenten(k.value)):
            funde.append((zeile, f"zeichenkette {k.value!r}"))
        elif isinstance(k, ast.Call):
            name = k.func.id if isinstance(k.func, ast.Name) else (
                k.func.attr if isinstance(k.func, ast.Attribute) else "")
            erstes = k.args[0] if k.args else None
            if name in DYNAMISCH and not (isinstance(erstes, ast.Constant) and isinstance(erstes.value, str)):
                funde.append((zeile, f"{name} mit unbekanntem Namen"))
            elif name in DYNAMISCH and "zeitquelle" in _komponenten(erstes.value):
                funde.append((zeile, f"{name}({erstes.value!r})"))
    return sorted(set(funde))


# ---- Selbsttest des Waechters -------------------------------------------------------------------------------------

@pytest.mark.parametrize("quelle", [
    "from smartheat_core import wallclock\n",
    "from smartheat_core import wallclock as w\nx = w.jetzt()\n",
    "import smartheat_core.wallclock\n",
    "import smartheat_core.wallclock as w\n",
    "from smartheat_core.wallclock import vertrauenswuerdig\n",
    "from smartheat_core.zeitquelle import fachliche_epoche\n",
    "from smartheat_core.zeitquelle import echte_epoche, fachliche_wanduhr as wanduhr\nx = wanduhr()\n",
    "from smartheat_core import zeitquelle\nx = zeitquelle.fachliche_epoche()\n",
    "import smartheat_core.zeitquelle as z\nx = z.fachliche_wanduhr()\n",
    "from smartheat_core.zeitquelle import *\n",
    "from smartheat_core import *\n",
    "from .zeitquelle import *\n",
    "from . import *\n",
    "from smartheat_core import zeitquelle\nx = getattr(zeitquelle, 'fachliche_epoche')\n",
    "import importlib\nm = importlib.import_module('smartheat_core.wallclock')\n",
    "m = __import__('smartheat_core.zeitquelle')\n",
    "import importlib\nm = importlib.import_module(name)\n",
    "def f():\n    from smartheat_core import wallclock\n",
    "def f():\n    from smartheat_core.zeitquelle import fachliche_epoche\n",
    "def f(uhr=wallclock.now):\n    pass\n",
    "class C:\n    def f(self):\n        return self.wallclock\n",
])
def test_waechter_findet_verbotene_zeit(quelle):
    assert _verstoesse(quelle), quelle


@pytest.mark.parametrize("quelle", [
    "import time\nx = time.monotonic()\n",
    "from smartheat_core.zeitquelle import echte_epoche\nx = echte_epoche()\n",
    "from smartheat_core import zeitquelle\nx = zeitquelle.echte_epoche()\n",
    "from smartheat_core import dateien\nfrom smartheat_core.zahlen import ist_endliche_zahl\n",
    "from smartheat_core import dateien, binding\n",
    "import importlib\nm = importlib.import_module('smartheat_core.dateien')\n",
    "def f(uhr=echte_epoche):\n    '''Zeit aus der wallclock-Uhr ist hier nicht gemeint.'''\n",
    "x = 'fachliche Epoche und Wanduhr'\n",
    "from smartheat_gateway import wallclock_daten\n",
])
def test_waechter_laesst_saubere_module_durch(quelle):
    assert _verstoesse(quelle) == [], quelle


def test_nur_die_genannte_funktion_zaehlt():
    quelle = ("from smartheat_core.zeitquelle import echte_epoche, fachliche_epoche\n\n"
              "def andere():\n    return fachliche_epoche()\n\n"
              "def ziel(uhr=echte_epoche):\n    return uhr()\n")
    assert _verstoesse(quelle, "ziel") == []
    assert _verstoesse(quelle), "ohne Funktion zaehlt die ganze Datei"
    mit_alias = quelle.replace("return uhr()", "return fachliche_epoche()")
    assert _verstoesse(mit_alias, "ziel") == [(7, "name fachliche_epoche")]
    ueber_alias = ("from smartheat_core.zeitquelle import fachliche_epoche as f\n\n"
                   "def ziel():\n    return f()\n")
    assert _verstoesse(ueber_alias, "ziel") == [(4, "name f")]
    assert _verstoesse("def ziel(uhr=wallclock.jetzt):\n    pass\n", "ziel")


def test_fehlende_oder_doppelte_funktion_ist_rot():
    assert _verstoesse("def andere():\n    pass\n", "ziel")
    assert _verstoesse("def ziel():\n    pass\n\nclass C:\n    def ziel(self):\n        pass\n", "ziel")


# ---- Lauf ueber die echten Module ---------------------------------------------------------------------------------

def _dateien() -> list[str]:
    liste = list(MODULE)
    for verzeichnis in VERZEICHNISSE:
        gefunden = sorted(str(p.relative_to(QUELLEN[1])) for p in (QUELLEN[1] / verzeichnis).glob("*.py"))
        assert gefunden, verzeichnis
        liste += gefunden
    return liste


def _quelle(pfad: str) -> str:
    for wurzel in QUELLEN:
        if (wurzel / pfad).is_file():
            return (wurzel / pfad).read_text("utf-8")
    raise AssertionError(f"{pfad} liegt in keiner Quelle")


@pytest.mark.parametrize("pfad", _dateien())
def test_echtes_modul_benutzt_die_fachliche_uhr_nicht(pfad):
    assert _verstoesse(_quelle(pfad)) == [], pfad


@pytest.mark.parametrize(("pfad", "funktion"), FUNKTIONEN)
def test_echte_funktion_benutzt_die_fachliche_uhr_nicht(pfad, funktion):
    assert _verstoesse(_quelle(pfad), funktion) == [], (pfad, funktion)


def test_jedes_modul_mit_echter_epoche_ist_geprueft():
    """Wer `echte_epoche` einfuehrt, gehoert in die Liste oben (oder bewusst nach `GEMISCHT`)."""
    bekannt = set(_dateien()) | GEMISCHT | {pfad for pfad, _ in FUNKTIONEN}
    ungeprueft = []
    for wurzel in QUELLEN:
        for pfad in wurzel.rglob("*.py"):
            relativ = str(pfad.relative_to(wurzel))
            if "echte_epoche" in pfad.read_text("utf-8") and relativ not in bekannt:
                ungeprueft.append(relativ)
    assert sorted(ungeprueft) == []
    for pfad in GEMISCHT:
        assert _quelle(pfad), pfad
