"""Attrappen fuer das, was rauc_bundle.py aufruft: `docker` (build, run des rauc-Containers) und `openssl cms`. Die Attrappe
von `docker run` fuehrt das Programm im Container nach (rauc bundle, extract-signature, replace-signature, info, debugfs) und
prueft dabei, was der echte Container braucht: jeder Pfad, den er bekommt, liegt im gemounteten Ordner. Jeder Aufruf geht als
JSON-Zeile in die Datei $ATTRAPPE_LOG; $ATTRAPPE_FEHLER nennt den Schritt, der mit $ATTRAPPE_MELDUNG auf stdout scheitert;
$ATTRAPPE_IMAGE ist das JSON {Pfad im Image: Inhalt} fuer debugfs. Aufruf aus einem Skript, das nach dem Programm heisst
(docker, openssl): `rauc_attrappe.main()`. `openssl` ausser `cms` reicht $ATTRAPPE_OPENSSL_ECHT durch (signieren.py braucht
das echte)."""
import json
import os
import sys
from pathlib import Path

SKRIPT = """#!{python}
import sys
sys.path.insert(0, {ordner!r})
import rauc_attrappe
rauc_attrappe.main()
"""


def skript(python: str = sys.executable) -> str:
    """Inhalt eines Skripts `docker` bzw. `openssl`, das main() aufruft."""
    return SKRIPT.format(python=python, ordner=str(Path(__file__).resolve().parent))


def _protokoll(eintrag: dict) -> None:
    with Path(os.environ["ATTRAPPE_LOG"]).open("a") as datei:
        datei.write(json.dumps(eintrag) + "\n")


def _fehler(schritt: str) -> None:
    if os.environ.get("ATTRAPPE_FEHLER") == schritt:
        sys.stdout.write(os.environ.get("ATTRAPPE_MELDUNG", "Fehler") + "\n")
        sys.exit(1)


def ist_rauc_aufruf(argv: list[str]) -> bool:
    """`docker build` (das rauc-Abbild) oder `docker run` mit dem rauc-Abbild."""
    return bool(argv) and (argv[0] == "build" or any(a.startswith("setwell-rauc:") for a in argv))


def openssl(argv: list[str]) -> None:
    if argv[:1] != ["cms"]:
        os.execv(os.environ["ATTRAPPE_OPENSSL_ECHT"], ["openssl", *argv])
    _protokoll({"prog": "openssl", "argv": argv})
    if "-verify" in argv:
        _fehler("verify")
        Path(argv[argv.index("-out") + 1]).write_bytes(b"manifest")
    else:
        _fehler("sign")
        Path(argv[argv.index("-out") + 1]).write_bytes(b"sig-neu")


def docker(argv: list[str]) -> None:
    if argv[0] == "build":
        _protokoll({"prog": "docker", "argv": argv, "stdin": sys.stdin.read()})
        _fehler("build")
        return
    assert argv[0] == "run", argv
    bild = next(i for i, a in enumerate(argv) if a.startswith("setwell-rauc:"))
    optionen, programm = argv[1:bild], argv[bild + 1:]
    buehne = optionen[optionen.index("-v") + 1].split(":")[0]
    eintrag = {"prog": "docker-run", "optionen": optionen, "bild": argv[bild], "programm": programm,
               # jeder absolute Pfad im Befehl liegt in der Buehne (die Pfade im Image stehen in `debugfs -R "cat ..."`)
               "ausserhalb": [a for a in programm if a.startswith("/") and not a.startswith(buehne + "/")]}
    if programm[0] == "debugfs":
        eintrag["image"] = programm[-1]
        _protokoll(eintrag)
        _fehler("debugfs")
        sys.stdout.write(json.loads(os.environ["ATTRAPPE_IMAGE"])[programm[2].removeprefix("cat ")])
        return
    assert programm[0] == "rauc", programm
    if "bundle" in programm:
        inhalt, ausgabe = Path(programm[-2]), Path(programm[-1])
        eintrag["inhalt"] = {p.name: [p.stat().st_ino, p.stat().st_size] for p in sorted(inhalt.iterdir())}
        eintrag["raucm"] = (inhalt / "manifest.raucm").read_text()
        eintrag["kette"] = [programm[programm.index(o) + 1] for o in ("--cert", "--key")]
        eintrag["kette_da"] = all(Path(p).is_file() for p in eintrag["kette"])
        _protokoll(eintrag)
        _fehler("bundle")
        ausgabe.write_bytes(b"ROH")
    elif "extract-signature" in programm:
        _protokoll(eintrag)
        _fehler("extract-signature")
        Path(programm[-1]).write_bytes(b"cms")
    elif "replace-signature" in programm:
        _protokoll(eintrag)
        _fehler("replace-signature")
        Path(programm[-1]).write_bytes(b"SYSTEM-BUNDLE")
    else:
        _protokoll(eintrag)
        _fehler("info")


def main() -> None:
    name = os.path.basename(sys.argv[0])
    {"docker": docker, "openssl": openssl}[name](sys.argv[1:])
