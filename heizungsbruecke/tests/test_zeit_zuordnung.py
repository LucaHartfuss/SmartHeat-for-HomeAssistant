"""Waechter: jede Stelle, die die Wanduhr des Systems liest, ist einer Uhr zugeordnet.

`fachlich` heisst: die Zeit folgt der fachlichen Uhr des Geraets (Nachrichtenstempel, Tagesgang, Wartezeiten der
Anlage); `echt` heisst: die Zeit wird gegen die Aussenwelt gerechnet (Signaturen des Servers, Hersteller-API, Zigbee2MQTT,
Hoststatus) und bleibt echt. Gezaehlt wird jede Referenz, auch als Vorgabewert eines Parameters oder ueber einen Alias;
die Zeitumrechnung eines gegebenen Werts (`fromtimestamp`) und die monotone Uhr lesen die Wanduhr nicht. Eine neue Stelle
ohne Eintrag und ein Eintrag ohne Stelle machen den Test rot. Die Host-Dienste unter `gateway/host` lesen die echte
Zeit und importieren den Kern nicht; sie gehoeren nicht dazu."""
import ast
from collections import Counter
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
QUELLEN = (REPO / "heizungsbruecke" / "src", REPO / "gateway" / "src")
ZUORDNUNGEN = frozenset({"fachlich", "echt"})

# Referenzen, die die Wanduhr lesen.
WANDUHR = frozenset({
    "time.time", "time.time_ns", "time.clock_gettime", "time.clock_gettime_ns",
    "datetime.datetime.now", "datetime.datetime.utcnow", "datetime.datetime.today", "datetime.date.today",
})
# Lesen die Wanduhr nur, wenn kein Zeitwert gegeben ist (Aufruf ohne Argument oder mit None); als blosse Referenz,
# etwa als Vorgabewert, zaehlen sie immer.
OHNE_ZEITWERT = frozenset({"time.localtime", "time.gmtime", "time.ctime", "time.asctime"})
# Lesen die Wanduhr, wenn der Aufruf weniger als diese Zahl an Argumenten hat (Format und Zeitwert).
ZEITWERT_NOETIG = {"time.strftime": 2}
MODULE = frozenset({"time", "datetime"})

# (Datei unter src, Umfeld, Referenz, Zuordnung, Grund). Umfeld: Klasse und Funktion, in der die Referenz steht (ein
# Vorgabewert zaehlt zur Funktion, die ihn traegt); ohne Funktion `<modul>`. Eine Referenz je Zeile.
ZUORDNUNG: list[tuple[str, str, str, str, str]] = [
    ("smartheat_core/zeitquelle.py", "echte_epoche", "time.time", "echt",
     "die einzige Stelle des Kerns, die die Systemuhr liest; die fachliche Wanduhr (Fristen, Plan, Uhr-Vertrauen) "
     "und die fachliche Epoche addieren den Versatz darauf, echte_epoche bleibt unverschoben"),
    ("smartheat_gateway/geraet_host.py", "GatewayDeviceHost.__init__", "time.time", "fachlich",
     "Vorgabe von wall: Zeitstempel der Nachrichten und des Zustands; dieselbe Uhr prueft heute auch das Alter von "
     "host/status.json, das echte Zeit traegt"),
    ("smartheat_gateway/befehle.py", "Kontext", "time.time", "fachlich",
     "Vorgabe von wall im Befehlskontext (der Host reicht seine Uhr durch): last_seen der Geraete, Fenster der Inventur"),
    ("smartheat_gateway/drivers/simulation.py", "SimulationDriver.__init__", "time.time", "fachlich",
     "Tagesgang der Aussentemperatur und Wartezeit, bis ein geschriebener Hebel sichtbar wird; die Registry reicht "
     "keine Uhr durch"),
    ("smartheat_gateway/zigbee.py", "ZigbeeMirror.__init__", "time.time", "echt",
     "Alter der retained Meldungen gegen last_seen von Zigbee2MQTT, das echte Zeit traegt"),
    ("smartheat_gateway/quota.py", "QuotaGuard.__init__", "time.time", "echt",
     "Fenster des Herstellerkontingents und Sperre bis zum Zeitpunkt der Rate-Limit-Antwort"),
    ("smartheat_gateway/drivers/vicare_cloud/api.py", "ViCareApi.__init__", "time.time", "echt",
     "Sperrzeit nach Retry-After der Hersteller-API"),
    ("smartheat_gateway/drivers/vicare_cloud/oauth.py", "TokenStore.__init__", "time.time", "echt",
     "Ablauf der Tokens und der offenen Anmeldung gegen die Zeit des Herstellers"),
    ("smartheat_gateway/drivers/vicare_cloud/driver.py", "ViCareCloudDriver.__init__", "time.time", "echt",
     "Vorgabe von wall, die an Tokenspeicher, Kontingent und API geht; der Stempel der Inventurprobe wird nirgends gelesen"),
]


def _bindungen(baum: ast.AST) -> dict[str, set[str]]:
    """Name -> alle voll qualifizierten Pfade, die der Name nach den Imports der Datei meinen kann (an jeder Stelle,
    auch in Funktionen; derselbe Name darf an mehreren Stellen verschieden gebunden sein, jede Deutung zaehlt)."""
    gebunden: dict[str, set[str]] = {}
    for knoten in ast.walk(baum):
        if isinstance(knoten, ast.Import):
            for a in knoten.names:
                gebunden.setdefault(a.asname or a.name.split(".")[0], set()).add(
                    a.name if a.asname else a.name.split(".")[0])
        elif isinstance(knoten, ast.ImportFrom) and knoten.module and not knoten.level:
            for a in knoten.names:
                if a.name != "*":
                    gebunden.setdefault(a.asname or a.name, set()).add(f"{knoten.module}.{a.name}")
    return gebunden


def _aufloesen(knoten: ast.AST, gebunden: dict[str, set[str]]) -> set[str]:
    """Die Pfade, die ein Name oder eine Attributkette nach den Imports meinen kann; leer, wenn keiner bekannt ist."""
    if isinstance(knoten, ast.Name):
        return set(gebunden.get(knoten.id, ()))
    if isinstance(knoten, ast.Attribute):
        return {f"{vorn}.{knoten.attr}" for vorn in _aufloesen(knoten.value, gebunden)}
    return set()


class _Sucher(ast.NodeVisitor):
    def __init__(self, gebunden: dict[str, set[str]]) -> None:
        self.gebunden = gebunden
        self.umfeld: list[str] = []
        self.funde: list[tuple[str, str]] = []  # (Umfeld, Referenz)
        self.aufruf: dict[int, ast.Call] = {}

    def _name(self) -> str:
        return ".".join(self.umfeld) or "<modul>"

    def _bereich(self, knoten: ast.AST, name: str) -> None:
        self.umfeld.append(name)
        self.generic_visit(knoten)
        self.umfeld.pop()

    def visit_FunctionDef(self, knoten: ast.FunctionDef) -> None:
        self._bereich(knoten, knoten.name)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, knoten: ast.ClassDef) -> None:
        self._bereich(knoten, knoten.name)

    def visit_Call(self, knoten: ast.Call) -> None:
        self.aufruf[id(knoten.func)] = knoten
        name = knoten.func.id if isinstance(knoten.func, ast.Name) else (
            knoten.func.attr if isinstance(knoten.func, ast.Attribute) else "")
        erstes = knoten.args[0] if knoten.args else None
        if name in ("__import__", "import_module") and isinstance(erstes, ast.Constant) \
                and isinstance(erstes.value, str) and erstes.value.split(".")[0] in MODULE:
            self.funde.append((self._name(), f"{name}({erstes.value!r})"))
        if name == "getattr" and len(knoten.args) >= 2 and isinstance(knoten.args[1], ast.Constant) \
                and isinstance(knoten.args[1].value, str):
            for vorn in _aufloesen(knoten.args[0], self.gebunden):
                pfad = f"{vorn}.{knoten.args[1].value}"
                if pfad in WANDUHR | OHNE_ZEITWERT | ZEITWERT_NOETIG.keys():
                    self.funde.append((self._name(), pfad))
        self.generic_visit(knoten)

    def visit_ImportFrom(self, knoten: ast.ImportFrom) -> None:
        if knoten.module in MODULE and any(a.name == "*" for a in knoten.names):
            self.funde.append((self._name(), f"{knoten.module}.*"))

    def _verweis(self, knoten: ast.Attribute | ast.Name) -> None:
        if not isinstance(knoten.ctx, ast.Load):
            return
        aufruf = self.aufruf.get(id(knoten))
        for ziel in _aufloesen(knoten, self.gebunden):
            if ziel in WANDUHR:
                self.funde.append((self._name(), ziel))
            elif ziel in OHNE_ZEITWERT:
                erstes = aufruf.args[0] if aufruf is not None and aufruf.args else None
                hat_zeit = aufruf is not None and (aufruf.args or aufruf.keywords) and not (
                    isinstance(erstes, ast.Constant) and erstes.value is None)
                if not hat_zeit:
                    self.funde.append((self._name(), ziel))
            elif ziel in ZEITWERT_NOETIG and (
                    aufruf is None or len(aufruf.args) + len(aufruf.keywords) < ZEITWERT_NOETIG[ziel]):
                self.funde.append((self._name(), ziel))

    def visit_Attribute(self, knoten: ast.Attribute) -> None:
        self._verweis(knoten)
        self.generic_visit(knoten)

    def visit_Name(self, knoten: ast.Name) -> None:
        self._verweis(knoten)


def _stellen(quelle: str) -> list[tuple[str, str]]:
    """Alle Stellen einer Quelle, die die Wanduhr lesen: (Umfeld, Referenz), in Reihenfolge der Quelle."""
    baum = ast.parse(quelle)
    sucher = _Sucher(_bindungen(baum))
    sucher.visit(baum)
    return sorted(sucher.funde)


def _ist() -> list[tuple[str, str, str]]:
    funde = []
    for wurzel in QUELLEN:
        for pfad in wurzel.rglob("*.py"):
            for umfeld, ziel in _stellen(pfad.read_text("utf-8")):
                funde.append((str(pfad.relative_to(wurzel)), umfeld, ziel))
    return sorted(funde)


def _abweichungen(ist: list[tuple[str, str, str]], eintraege) -> dict[str, list]:
    soll = Counter((datei, umfeld, ziel) for datei, umfeld, ziel, *_ in eintraege)
    gefunden = Counter(ist)
    return {"ohne_zuordnung": sorted((gefunden - soll).elements()), "ohne_stelle": sorted((soll - gefunden).elements())}


def test_jede_uhrstelle_ist_zugeordnet():
    assert _abweichungen(_ist(), ZUORDNUNG) == {"ohne_zuordnung": [], "ohne_stelle": []}


def test_zuordnung_ist_fachlich_oder_echt_mit_grund():
    for datei, umfeld, ziel, zuordnung, grund in ZUORDNUNG:
        assert zuordnung in ZUORDNUNGEN, (datei, umfeld, ziel)
        assert grund.strip(), (datei, umfeld, ziel)


def test_leere_liste_und_neue_stelle_sind_rot():
    ist = [("a.py", "f", "time.time")]
    assert _abweichungen(ist, []) == {"ohne_zuordnung": ist, "ohne_stelle": []}
    assert _abweichungen([], [("a.py", "f", "time.time", "echt", "x")]) == {
        "ohne_zuordnung": [], "ohne_stelle": ist}
    # eine zweite Referenz im selben Umfeld braucht einen eigenen Eintrag
    assert _abweichungen(ist * 2, [("a.py", "f", "time.time", "echt", "x")])["ohne_zuordnung"] == ist
    assert _abweichungen(ist, [("a.py", "f", "time.time", "echt", "x")]) == {"ohne_zuordnung": [], "ohne_stelle": []}


@pytest.mark.parametrize("quelle", [
    "import time\nx = time.time()\n",
    "import time as t\nx = t.time()\n",
    "from time import time\nx = time()\n",
    "from time import time as f\nx = f()\n",
    "import time\nx = time.time_ns()\n",
    "import time\nx = time.clock_gettime(0)\n",
    "import time\nx = time.localtime()\n",
    "import time\nx = time.gmtime()\n",
    "import time\nx = time.localtime(None)\n",
    "import time\nx = time.ctime()\n",
    "import time\nx = time.strftime('%H')\n",
    "from time import strftime\nx = strftime('%H')\n",
    "import time\nf = getattr(time, 'time')\n",
    "from datetime import datetime\nf = getattr(datetime, 'now')\n",
    "from datetime import datetime\nx = datetime.now()\n",
    "from datetime import datetime as dt\nx = dt.utcnow()\n",
    "import datetime\nx = datetime.datetime.now()\n",
    "import datetime as d\nx = d.datetime.now(d.UTC)\n",
    "from datetime import date\nx = date.today()\n",
    "import datetime\nx = datetime.date.today()\n",
    "import time\ndef f(clock=time.time):\n    return clock()\n",
    "import time\ndef f(*, wall=time.time):\n    return wall()\n",
    "import time\nclass C:\n    wall = time.time\n",
    "import time\nf = lambda: time.time()\n",
    "import time\nx = time.localtime\n",
    "from datetime import datetime\nclock = lambda: datetime.now\n",
    "import time\n\ndef f():\n    from datetime import time\n    return time(1)\n\ndef g():\n    return time.time()\n",
    "from time import *\n",
    "from datetime import *\n",
    "__import__('time')\n",
    "import importlib\nimportlib.import_module('datetime')\n",
])
def test_waechter_erkennt_die_schreibweise(quelle):
    assert _stellen(quelle), quelle


@pytest.mark.parametrize("quelle", [
    "from datetime import time\nx = time(12, 0)\n",
    "from datetime import datetime\nx = datetime.fromtimestamp(5)\n",
    "import time\nx = time.monotonic()\ntime.sleep(1)\n",
    "import time\nx = time.localtime(5)\n",
    "import time\nx = time.gmtime(5)\n",
    "import time\nx = time.strftime('%H', time.localtime(5))\n",
    "import time\nx = time.strftime('%H', t)\n",
    "import time\nf = getattr(time, 'sleep')\n",
    "import time\nf = getattr(time, name)\n",
    "import time\nf = getattr(obj, 'time')\n",
    "class C:\n    time = 5\nx = C.time\n",
    "from smartheat_core import wallclock\nx = wallclock.now()\n",
])
def test_waechter_gegenprobe_ohne_fund(quelle):
    assert _stellen(quelle) == [], quelle


def test_umfeld_und_vorgabewert():
    quelle = ("import time\nfrom datetime import datetime\n\n"
              "class A:\n    def __init__(self, wall=time.time):\n        self.t = datetime.now()\n\n"
              "def frei(clock=time.time, wall=time.time):\n    pass\n\nx = time.time()\n")
    assert _stellen(quelle) == sorted([
        ("A.__init__", "time.time"), ("A.__init__", "datetime.datetime.now"),
        ("frei", "time.time"), ("frei", "time.time"), ("<modul>", "time.time")])
