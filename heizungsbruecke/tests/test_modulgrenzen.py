"""Modulgrenzen des Add-on-Repos (Audit 5 A5-59, Konsolidierung K4; Spec Stufe 3 S6 4.4), wie
HeizungssteuerungServer/tests/test_modulgrenzen.py: jedes Paket importiert nur die Standardbibliothek, die erlaubten
eigenen Pakete und die genannten Drittpakete in genau den genannten Dateien. Schichtung core <- runtime <- device <-
Hosts; die Hosts kennen sich nie. Das Gateway-Image kopiert jedes Kernpaket, das das Gateway importiert."""
import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
QUELLEN = {
    "smartheat_core": REPO / "heizungsbruecke" / "src" / "smartheat_core",
    "smartheat_runtime": REPO / "heizungsbruecke" / "src" / "smartheat_runtime",
    "smartheat_device": REPO / "heizungsbruecke" / "src" / "smartheat_device",
    "heizungsbruecke": REPO / "heizungsbruecke" / "src" / "heizungsbruecke",
    "smartheat_gateway": REPO / "gateway" / "src" / "smartheat_gateway",
}
ERLAUBT = {
    "smartheat_core": set(),
    "smartheat_runtime": {"smartheat_core"},
    "smartheat_device": {"smartheat_core", "smartheat_runtime"},
    "heizungsbruecke": {"smartheat_core", "smartheat_runtime", "smartheat_device"},
    "smartheat_gateway": {"smartheat_core", "smartheat_runtime", "smartheat_device"},
}
#: Drittpakete je Paket -> Dateien (relativ zum Paket), in denen sie stehen duerfen
DRITTE = {
    "smartheat_core": {},
    "smartheat_runtime": {},
    "smartheat_device": {"cryptography": {"identity.py"}, "paho": {"link.py"}, "requests": {"bootstrap.py"}},
    "heizungsbruecke": None,  # None: Drittpakete frei (requests, websocket ...), eigene Pakete nach ERLAUBT
    "smartheat_gateway": None,
}
KERN = ("smartheat_core", "smartheat_runtime", "smartheat_device")


def _importe(path: Path) -> list[tuple[str, int]]:
    """(Modul, Ebene) je Import; relative Importe mit ihrer Ebene, damit sie gemeldet werden koennen."""
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found += [(alias.name, 0) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            found.append((node.module or "", node.level))
    return found


def _dateien(paket: str):
    for path in sorted(QUELLEN[paket].rglob("*.py")):
        if "__pycache__" not in path.parts:
            yield path


def verstoesse() -> list[str]:
    eigen = set(QUELLEN)
    funde = []
    for paket in QUELLEN:
        for path in _dateien(paket):
            name = str(path.relative_to(QUELLEN[paket]))
            for modul, ebene in _importe(path):
                if ebene > 0:
                    funde.append(f"{paket}/{name}: relativer Import")
                    continue
                top = modul.split(".")[0]
                if top == paket or top in sys.stdlib_module_names:
                    continue
                if top in eigen:
                    if top not in ERLAUBT[paket]:
                        funde.append(f"{paket}/{name} -> {top}")
                    continue
                dritte = DRITTE[paket]
                if dritte is not None and name not in dritte.get(top, set()):
                    funde.append(f"{paket}/{name} -> {top} (Drittpaket)")
    return funde


def test_importe_folgen_der_grenztabelle():
    assert verstoesse() == []


def test_jedes_paket_hat_eine_zeile_und_es_gibt_kein_fremdes_paket():
    assert set(ERLAUBT) == set(QUELLEN) == set(DRITTE)
    for paket, ordner in QUELLEN.items():  # sonst prueft rglob ein verschobenes Paket still nicht mehr
        assert ordner.is_dir() and (ordner / "__init__.py").exists(), paket
    kern_pakete = {p.name for p in (REPO / "heizungsbruecke" / "src").iterdir()
                   if p.is_dir() and (p / "__init__.py").exists()}
    assert kern_pakete == {"smartheat_core", "smartheat_runtime", "smartheat_device", "heizungsbruecke"}


def test_das_gateway_image_kopiert_jedes_kernpaket():
    dockerfile = (REPO / "gateway" / "Dockerfile").read_text()
    for paket in KERN:
        assert f"COPY heizungsbruecke/src/{paket} ./src/{paket}" in dockerfile, paket
