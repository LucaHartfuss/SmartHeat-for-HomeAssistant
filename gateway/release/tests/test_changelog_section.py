import subprocess
import sys
from pathlib import Path

from changelog_section import unterabschnitt

SCRIPT = Path(__file__).resolve().parents[1] / "changelog_section.py"
TEXT = "# Changelog\n\n## Unveröffentlicht\n\n- offen\n\n## 0.4.0\n\n- neu\n- mehr\n\n## 0.3.0\n\n- alt\n"


def _run(tmp_path, version):
    path = tmp_path / "CHANGELOG.md"
    path.write_text(TEXT, encoding="utf-8")
    return subprocess.run([sys.executable, str(SCRIPT), str(path), version], capture_output=True, text=True)


def test_prints_exactly_the_section_of_the_version(tmp_path):
    result = _run(tmp_path, "0.4.0")
    assert result.returncode == 0
    assert result.stdout == "- neu\n- mehr\n"


def test_missing_section_fails(tmp_path):
    result = _run(tmp_path, "9.9.9")
    assert result.returncode == 1 and "9.9.9" in result.stderr


def _run_text(tmp_path, text, version):
    path = tmp_path / "CHANGELOG.md"
    path.write_text(text, encoding="utf-8")
    return subprocess.run([sys.executable, str(SCRIPT), str(path), version], capture_output=True, text=True)


def test_bracketed_and_dated_headings_match_like_the_release_gate(tmp_path):
    # Das Release-Gate (tools/release_gate.py) nimmt "## [V]" und "## V - Datum"; der Signier-Job darf nicht strenger sein.
    for heading in ("## [0.4.0]", "## 0.4.0 - 2026-10-08", "## [0.4.0] - 2026-10-08"):
        result = _run_text(tmp_path, f"# Changelog\n\n{heading}\n\n- neu\n\n## 0.3.0\n\n- alt\n", "0.4.0")
        assert result.returncode == 0 and result.stdout == "- neu\n", heading


def test_empty_section_fails_like_the_release_gate(tmp_path):
    for text in ("## 0.4.0\n\n## 0.3.0\n\n- alt\n", "## 0.4.0\n   \n"):
        result = _run_text(tmp_path, text, "0.4.0")
        assert result.returncode == 1 and result.stdout == "" and "0.4.0" in result.stderr


def test_version_prefixes_do_not_match(tmp_path):
    for heading in ("## 0.4.01", "## 0.4.0x", "## 10.4.0", "## 0.4.0-rc1"):
        result = _run_text(tmp_path, f"{heading}\n\n- falsch\n", "0.4.0")
        assert result.returncode == 1, heading


MIT_UNTERABSCHNITTEN = (
    "# Changelog\n\n## 0.4.0\n\n### Sicherheit\n\n- Luecke\n\n### Funktion\n\n- neue Ansicht\n- mehr\n\n"
    "### Sonstiges\n\n- anderes\n\n## 0.3.0\n\n### Funktion\n\n- alt\n")


def test_unterabschnitt_liefert_nur_den_eigenen_text_der_version():
    assert unterabschnitt(MIT_UNTERABSCHNITTEN, "0.4.0", "Funktion") == "- neue Ansicht\n- mehr\n"
    assert unterabschnitt(MIT_UNTERABSCHNITTEN, "0.4.0", "Sicherheit") == "- Luecke\n"
    assert unterabschnitt(MIT_UNTERABSCHNITTEN, "0.3.0", "Funktion") == "- alt\n"  # nicht der der Nachbarversion


def test_unterabschnitt_ohne_ueberschrift_oder_ohne_eintrag_ist_leer():
    assert unterabschnitt("## 0.4.0\n\n- flach\n", "0.4.0", "Funktion") == ""
    assert unterabschnitt("## 0.4.0\n\n### Funktion\n\n### Sonstiges\n\n- x\n", "0.4.0", "Funktion") == ""
    assert unterabschnitt("## 0.4.0\n\n### Funktion\n   \n", "0.4.0", "Funktion") == ""
    assert unterabschnitt("## 0.4.0\n\n### Funktionen\n\n- x\n", "0.4.0", "Funktion") == ""  # nur die ganze Ueberschrift


def test_unterabschnitt_ohne_abschnitt_der_version_ist_none():
    assert unterabschnitt(MIT_UNTERABSCHNITTEN, "9.9.9", "Funktion") is None
    assert unterabschnitt("## 0.4.0\n\n", "0.4.0", "Funktion") is None
