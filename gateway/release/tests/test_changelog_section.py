import subprocess
import sys
from pathlib import Path

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
