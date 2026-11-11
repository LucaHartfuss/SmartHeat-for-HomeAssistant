"""Das Add-on-Image bringt keine Systemzonendaten mit (schlankes Basis-Image): Europe/Berlin kommt aus dem Paket
tzdata, das Pyproject und Lock-Datei (mit Hash) fuehren. Die Planung rechnet die Ortszeit der Anlage damit."""
import re
import subprocess
import sys
import tomllib
from pathlib import Path

WURZEL = Path(__file__).resolve().parents[1]


def test_europe_berlin_laedt_ohne_systemdaten():
    """Mit leerem Suchpfad fuer Systemzonendaten findet zoneinfo die Zone nur ueber das Paket tzdata."""
    code = (
        "import zoneinfo, datetime\n"
        "zoneinfo.reset_tzpath(to=[])\n"
        "assert zoneinfo.TZPATH == (), zoneinfo.TZPATH\n"
        "z = zoneinfo.ZoneInfo('Europe/Berlin')\n"
        "winter = datetime.datetime(2026, 1, 15, 12, tzinfo=z).utcoffset()\n"
        "sommer = datetime.datetime(2026, 7, 15, 12, tzinfo=z).utcoffset()\n"
        "print(int(winter.total_seconds()), int(sommer.total_seconds()))\n"
    )
    ergebnis = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert ergebnis.returncode == 0, ergebnis.stderr
    assert ergebnis.stdout.split() == ["3600", "7200"]


def test_tzdata_ist_laufzeitabhaengigkeit_in_pyproject():
    daten = tomllib.loads((WURZEL / "pyproject.toml").read_text(encoding="utf-8"))
    namen = {re.split(r"[<>=!~ \[]", abhaengigkeit, maxsplit=1)[0] for abhaengigkeit in daten["project"]["dependencies"]}
    assert "tzdata" in namen


def test_tzdata_ist_in_der_lock_datei_exakt_gepinnt_und_gehasht():
    lock = (WURZEL / "requirements.txt").read_text(encoding="utf-8")
    eintrag = re.search(r"^tzdata==[0-9][^\s\\]*( \\\n(?:    --hash=sha256:[0-9a-f]{64}(?: \\)?\n)+)", lock, re.MULTILINE)
    assert eintrag, "tzdata fehlt in requirements.txt oder ist nicht gehasht"
    assert eintrag.group(1).count("--hash=sha256:") >= 1
