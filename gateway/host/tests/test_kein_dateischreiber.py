"""Waechter: das Host-Paket hat keinen eigenen Datei-Schreiber. Der einzige atomare Schreibweg
ist smartheat_core.dateien (install.sh legt ihn mit ab, test_boundaries.py); eine zweite Kopie liefe auseinander
(Rechte, fsync, .tmp). Gesucht wird textuell (auch in Kommentaren), Ausnahmen stehen mit Begruendung in AUSNAHMEN."""
import re
import shutil
from collections import Counter
from pathlib import Path

PAKET = Path(__file__).resolve().parents[1] / "smartheat_host"

#: Art -> Muster. open( zaehlt nur mit einem schreibenden Modus (w, a, x oder +, auch als mode=, auch ueber mehrere
#: Zeilen und mit einer Klammerebene im Pfad); lesende Aufrufe
#: (open(p), open(p, "rb"), encoding="utf-8", urlopen(url, timeout=5)) sind frei.
MUSTER = {
    "os.replace": re.compile(r"\bos\.replace\("),
    "os.rename": re.compile(r"\bos\.rename\("),
    "O_EXCL": re.compile(r"\bO_EXCL\b"),
    "O_CREAT": re.compile(r"\bO_CREAT\b"),
    "O_WRONLY": re.compile(r"\bO_WRONLY\b"),
    "O_RDWR": re.compile(r"\bO_RDWR\b"),
    "write_text": re.compile(r"\.write_text\("),
    "write_bytes": re.compile(r"\.write_bytes\("),
    "open schreibend": re.compile(r"""\bopen\((?:[^()]|\([^()]*\))*?["'][rbt]*[wax+][rbtwax+]*["']"""),
}

#: (Datei, Art) -> (Anzahl, Begruendung). Jede weitere Fundstelle derselben Art in derselben Datei ist neu und rot.
AUSNAHMEN = {
    ("bundles.py", "open schreibend"): (
        1, "BundleStore.install (um Zeile 260): schreibt die Dateien eines Bundles in einen temporaeren Ordner, "
           "fsync je Datei; dateien.schreiben ersetzt einzelne Dateien, kein ganzes Verzeichnis"),
    ("bundles.py", "os.replace"): (
        1, "BundleStore.install (um Zeile 266): atomarer Tausch des ganzen Bundle-Verzeichnisses, nicht ueber "
           "dateien.schreiben abbildbar"),
    ("updater.py", "open schreibend"): (
        1, "_open_teil (um Zeile 514): das Archiv des Images (bis 512 MiB) wird in Bloecken nach <ziel>.part gestreamt, "
           "dateien.schreiben nimmt nur Bytes im Speicher; fsync am Ende von fetch_url_to_file"),
    ("updater.py", "os.replace"): (
        2, "fetch_url_to_file (um Zeile 599): <ziel>.part wird erst nach vollstaendigem Abruf zu <ziel>; "
           "_hole_archiv (um Zeile 919): ein zu kurz beendeter Abruf wird wieder zum .part, das der naechste Durchlauf "
           "fortsetzt; beide innerhalb von archive/, nie unter bundles/"),
    ("startauswahl.py", "os.replace"): (
        1, "schreibe_primaer (um Zeile 108): /bootfs/autoboot.txt (FAT) wird ueber autoboot.txt.neu, fsync, rename, fsync "
           "des Ordners und Zuruecklesen ersetzt; dateien.schreiben setzt per fchmod einen Modus, den FAT nicht kennt"),
    ("startauswahl.py", "O_CREAT"): (
        1, "schreibe_primaer (um Zeile 100): legt autoboot.txt.neu an, Begruendung wie bei os.replace in derselben Datei"),
    ("startauswahl.py", "O_WRONLY"): (
        1, "schreibe_primaer (um Zeile 100): schreibt autoboot.txt.neu, Begruendung wie bei os.replace in derselben Datei"),
    ("led.py", "write_text"): (
        3, "Led.take/set/restore (um Zeilen 58-65): sysfs-Attribute (trigger, brightness) werden direkt beschrieben; "
           "ein Tausch per .tmp und os.replace ist dort nicht moeglich"),
}


def fundstellen(text: str) -> list[tuple[int, str]]:
    """(Zeile, Art) jeder Fundstelle im Text."""
    funde = []
    for art, muster in MUSTER.items():
        for treffer in muster.finditer(text):
            funde.append((text.count("\n", 0, treffer.start()) + 1, art))
    return sorted(funde)


def verstoesse(paket: Path) -> list[str]:
    """Fundstellen ausserhalb von AUSNAHMEN (Datei:Zeile Art) und Ausnahmen, deren Anzahl nicht mehr stimmt."""
    gezaehlt: Counter = Counter()
    zeilen: dict[tuple[str, str], list[int]] = {}
    for datei in sorted(paket.rglob("*.py")):
        name = datei.relative_to(paket).as_posix()
        for zeile, art in fundstellen(datei.read_text(encoding="utf-8")):
            gezaehlt[(name, art)] += 1
            zeilen.setdefault((name, art), []).append(zeile)
    ergebnis = []
    for schluessel in sorted(set(gezaehlt) | set(AUSNAHMEN)):
        erlaubt = AUSNAHMEN.get(schluessel, (0, ""))[0]
        if gezaehlt[schluessel] != erlaubt:
            name, art = schluessel
            ergebnis.append(f"{name}:{zeilen.get(schluessel, [])} {art}: {gezaehlt[schluessel]} statt {erlaubt}")
    return ergebnis


def _kopie(tmp_path: Path) -> Path:
    """Der ganze Baum des Pakets (verstoesse sucht rekursiv), ohne Bytecode."""
    kopie = tmp_path / "smartheat_host"
    shutil.copytree(PAKET, kopie, ignore=shutil.ignore_patterns("__pycache__"))
    return kopie


def test_das_host_paket_hat_keinen_eigenen_datei_schreiber():
    assert verstoesse(PAKET) == []


def test_das_muster_findet_jede_schreibende_form():
    beispiel = "\n".join([
        "os.replace(tmp, ziel)",
        "os.rename(tmp, ziel)",
        "fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL)",
        "fd = os.open(p, os.O_RDWR)",
        "p.write_text('x')",
        "p.write_bytes(b'x')",
        "open(p, 'w')",
        'open(p, "a")',
        'open(p, "x")',
        'open(p, "wb")',
        'open(p, mode="ab")',
        'open(p, "r+")',
        "open(\n    p,\n    'w',\n)",
        "open(os.path.join(a, b), 'w')",
    ])
    assert [art for _, art in sorted(fundstellen(beispiel), key=lambda f: f[0])] == [
        "os.replace", "os.rename", "O_CREAT", "O_EXCL", "O_WRONLY", "O_RDWR", "write_text", "write_bytes",
        "open schreibend", "open schreibend", "open schreibend", "open schreibend", "open schreibend",
        "open schreibend", "open schreibend", "open schreibend"]


def test_lesende_aufrufe_sind_keine_fundstelle():
    beispiel = "\n".join([
        "open(p)",
        'open(p, "rb")',
        'open(p, encoding="utf-8")',
        "os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)",
        "urllib.request.urlopen(url, timeout=30)",
        "p.read_text()",
    ])
    assert fundstellen(beispiel) == []


def test_eine_neue_fundstelle_ausserhalb_der_tabelle_macht_den_waechter_rot(tmp_path):
    """Gegenprobe am echten Paket: eine Kopie mit einem neuen Schreiber in einer neuen Datei und einem vierten
    write_text in led.py meldet beide; die echten Ausnahmen bleiben still."""
    kopie = _kopie(tmp_path)
    assert verstoesse(kopie) == []
    (kopie / "neu.py").write_text("import os\n\nos.replace('a.tmp', 'a')\n", encoding="utf-8")
    led = kopie / "led.py"
    led.write_text(led.read_text(encoding="utf-8") + "\nPath('x').write_text('1')\n", encoding="utf-8")
    funde = verstoesse(kopie)
    assert len(funde) == 2
    assert funde[0].startswith("led.py:") and "write_text: 4 statt 3" in funde[0]
    assert funde[1] == "neu.py:[3] os.replace: 1 statt 0"


def test_eine_entfallene_ausnahme_muss_aus_der_tabelle(tmp_path):
    kopie = _kopie(tmp_path)
    (kopie / "led.py").unlink()
    assert verstoesse(kopie) == ["led.py:[] write_text: 0 statt 3"]
