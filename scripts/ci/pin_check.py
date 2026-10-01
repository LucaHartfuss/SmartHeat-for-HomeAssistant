#!/usr/bin/env python3
"""Pin-Check (TP12e, AU-018, Spec 2026-09-30-tp12e-vertraege-tests-lieferkette-design.md Abschnitt 3):
Jede Basis-Image-Angabe hat einen Digest, jede GitHub-Action eine 40-stellige SHA, jede Lock-Datei
Hashes und passt zu ihrer pyproject.toml. Nur Standardbibliothek; kanonisch in
HomeAssistant_Dev_Root/tools/pin_check.py, identische Kopien in den Unter-Repos unter
scripts/ci/pin_check.py (Root-check.sh Schritt "copies" prueft das).

Aufruf: python3 tools/pin_check.py --repo PFAD. Exit 0 = gueltig, 1 = Verstoesse
(je Zeile `FAIL <datei>: <text>`)."""
import argparse
import fnmatch
import os
import re
import sys
import tomllib
from collections.abc import Callable, Iterator
from pathlib import Path

DIGEST = re.compile(r"@sha256:[0-9a-f]{64}\b")
ACTION_SHA = re.compile(r"@[0-9a-f]{40}$")
HASH_OPTION = re.compile(r"--hash=sha256:[0-9a-f]{64}\b")
LOCK_NAMES = ("requirements.txt", "requirements.lock")
# .devtools, backups und .remember sind Root-spezifische, per .gitignore ausgeschlossene Ordner.
_SKIP_DIRS = {".git", ".venv", ".worktrees", "node_modules", "__pycache__", ".devtools", "backups", ".remember"}
_FROM_LINE = re.compile(r"^\s*FROM(?:\s|$)", re.IGNORECASE)
_FROM = re.compile(r"^\s*FROM\s+(?:--\S+\s+)*(\S+)(?:\s+AS\s+(\S+))?\s*$", re.IGNORECASE)
_PIP_INSTALL = re.compile(r"\bpip3?\s+install\b")
_PIP_REQUIREMENT_OPTION = re.compile(r"\s(?:-r|--requirement)(?:\s|=|$)")
_USES = re.compile(r"^\s*(?:-\s*)?uses:\s*[\"']?([^\s\"']+)[\"']?(.*)$")
_IMAGE = re.compile(r"^\s*image:\s*[\"']?([^\s\"']+)")
_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_REQUIREMENT = re.compile(rf"^({_NAME})(?:\[[^\]]*\])?==([^\s;\\]+)")
_LOCK_ENTRY = re.compile(rf"^({_NAME})")
_DECLARED = re.compile(rf"^\s*({_NAME})\s*(?:\[[^\]]*\])?\s*(.*)$", re.DOTALL)
_CLAUSE = re.compile(r"^(>=|<=|==|!=|~=|<|>)\s*(\d+(?:\.\d+)*)$")
_PLAIN_VERSION = re.compile(r"^\d+(?:\.\d+)*$")


def _walk(repo: Path) -> Iterator[Path]:
    """Alle Dateien unterhalb von `repo` in stabiler Reihenfolge. Uebersprungen werden Werkzeug-Ordner
    und eigene Git-Repos/Worktrees (im Dev-Root sind die vier Unter-Repos so eingebettet) - deren Dateien
    prueft der Pin-Check des jeweiligen Repos."""
    for directory, subdirs, names in os.walk(repo):
        here = Path(directory)
        subdirs[:] = sorted(
            sub for sub in subdirs if sub not in _SKIP_DIRS and not (here / sub / ".git").exists()
        )
        for name in sorted(names):
            yield here / name


def _logical_lines(text: str) -> list[str]:
    """Zeilen mit abschliessendem Backslash zu einer logischen Zeile verbinden."""
    lines: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        if raw.rstrip().endswith("\\"):
            buffer += raw.rstrip()[:-1] + " "
        else:
            lines.append(buffer + raw)
            buffer = ""
    if buffer:
        lines.append(buffer)
    return lines


def _check_dockerfile(repo: Path, path: Path) -> list[str]:
    problems = []
    name = path.relative_to(repo)
    stages: set[str] = set()
    for line in _logical_lines(path.read_text()):
        if line.lstrip().startswith("#"):
            continue
        match = _FROM.match(line)
        if match is not None and match.group(1).startswith("-"):
            match = None  # nur Optionen, kein Image
        if match is None:
            if _FROM_LINE.match(line):
                problems.append(f"{name}: FROM-Zeile nicht auswertbar: {line.strip()}")
        else:
            image, alias = match.group(1), match.group(2)
            if image.lower() != "scratch" and image.lower() not in stages and DIGEST.search(image) is None:
                problems.append(f"{name}: FROM {image} ohne @sha256-Digest")
            if alias:
                stages.add(alias.lower())
        if _PIP_INSTALL.search(line) and _PIP_REQUIREMENT_OPTION.search(line) and "--require-hashes" not in line:
            problems.append(f"{name}: pip install -r ohne --require-hashes")
    return problems


def _check_workflow(repo: Path, path: Path) -> list[str]:
    problems = []
    name = path.relative_to(repo)
    for line in path.read_text().splitlines():
        match = _USES.match(line)
        if match is None or match.group(1).startswith("./"):
            continue
        reference, rest = match.group(1), match.group(2)
        if ACTION_SHA.search(reference) is None:
            problems.append(f"{name}: uses: {reference} ohne 40-stellige SHA")
        elif re.search(r"#\s*\S", rest) is None:
            problems.append(f"{name}: uses: {reference} ohne Versionskommentar (# v5)")
    return problems


def _check_compose(repo: Path, path: Path) -> list[str]:
    name = path.relative_to(repo)
    problems = []
    for line in path.read_text().splitlines():
        match = _IMAGE.match(line)
        if match is not None and DIGEST.search(match.group(1)) is None:
            problems.append(f"{name}: image: {match.group(1)} ohne @sha256-Digest")
    return problems


def _version(text: str) -> tuple[int, ...]:
    if _PLAIN_VERSION.match(text) is None:
        raise ValueError(f"Version {text!r} nicht unterstuetzt (nur Release-Versionen wie 1.2.3)")
    return tuple(int(part) for part in text.split("."))


def _padded(left: tuple[int, ...], right: tuple[int, ...]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    width = max(len(left), len(right))
    return left + (0,) * (width - len(left)), right + (0,) * (width - len(right))


def satisfies(version: str, spec: str) -> bool:
    """Erfuellt `version` den Bereich `spec` (z. B. ">=2.1,<3")? Bewusst kleiner, konservativer Ersatz fuer
    einen PEP-440-Resolver (die Standardbibliothek hat keinen): unterstuetzt >=, <=, >, <, ==, !=, ~= mit
    reinen Release-Versionen, kommagetrennt. Alles andere (Wildcards, ===, Pre-Releases, URLs, Marker)
    wirft ValueError - ein Bereich, der nicht geprueft werden kann, darf nie still als erfuellt gelten."""
    actual = _version(version)
    clauses = [clause.strip() for clause in spec.split(",")] if spec.strip() else []
    for clause in clauses:
        match = _CLAUSE.match(clause)
        if match is None:
            raise ValueError(f"Versionsbereich {spec!r} nicht unterstuetzt (Teil {clause!r})")
        operator, bound_text = match.groups()
        bound = _version(bound_text)
        left, right = _padded(actual, bound)
        if operator == "~=":
            if len(bound) < 2:
                raise ValueError(f"Versionsbereich {spec!r} nicht unterstuetzt (~= braucht mindestens zwei Stellen)")
            prefix_length = len(bound) - 1
            holds = left >= right and left[:prefix_length] == right[:prefix_length]
        else:
            holds = {
                ">=": left >= right, "<=": left <= right, "==": left == right,
                "!=": left != right, "<": left < right, ">": left > right,
            }[operator]
        if not holds:
            return False
    return True


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse_lock(path: Path) -> tuple[dict[str, str], list[str]]:
    """({name: version}, Verstoesse innerhalb der Datei: nicht exakt gepinnt oder ohne --hash)."""
    locked: dict[str, str] = {}
    problems: list[str] = []
    text = "\n".join(
        re.sub(r"\s#.*$", "", line) for line in path.read_text().splitlines() if not line.lstrip().startswith("#")
    )
    for line in _logical_lines(text):
        stripped = line.strip()
        entry = _LOCK_ENTRY.match(stripped)
        if entry is None:  # Leerzeile, Option wie --index-url, -e ...
            continue
        match = _REQUIREMENT.match(stripped)
        if match is None:
            problems.append(f"{entry.group(1)} nicht mit == exakt gepinnt")
            continue
        locked[_normalize(match.group(1))] = match.group(2)
        if HASH_OPTION.search(line) is None:
            problems.append(f"{match.group(1)} ohne --hash=sha256:")
    return locked, problems


def _check_lock(repo: Path, path: Path) -> list[str]:
    name = path.relative_to(repo)
    locked, lock_problems = _parse_lock(path)
    problems = [f"{name}: {problem}" for problem in lock_problems]
    pyproject = path.parent / "pyproject.toml"
    if not pyproject.exists():
        return problems
    try:
        project = tomllib.loads(pyproject.read_text()).get("project", {})
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return [*problems, f"{pyproject.relative_to(repo)}: nicht lesbar"]
    declared = project.get("dependencies", []) if isinstance(project, dict) else []
    if not isinstance(declared, list):
        return [*problems, f"{pyproject.relative_to(repo)}: project.dependencies ist keine Liste"]
    for requirement in declared:
        match = _DECLARED.match(requirement) if isinstance(requirement, str) else None
        if match is None:
            problems.append(f"{name}: Abhaengigkeit {requirement!r} aus pyproject.toml nicht unterstuetzt")
            continue
        package, spec = _normalize(match.group(1)), match.group(2).strip()
        if ";" in spec:
            problems.append(f"{name}: {package}: Marker in {requirement!r} nicht unterstuetzt")
        elif package not in locked:
            problems.append(f"{name}: {package} aus pyproject.toml fehlt in der Lock-Datei")
        else:
            try:
                if not satisfies(locked[package], spec):
                    problems.append(f"{name}: {package}=={locked[package]} erfuellt {spec!r} aus pyproject.toml nicht")
            except ValueError as error:
                problems.append(f"{name}: {package}: {error}")
    return problems


def _checked(check: Callable[[Path, Path], list[str]], repo: Path, path: Path) -> list[str]:
    """Eine nicht lesbare Datei (kein UTF-8, kaputter Symlink, ...) ist ein Verstoss, kein Absturz."""
    try:
        return check(repo, path)
    except (OSError, UnicodeDecodeError):
        return [f"{path.relative_to(repo)}: nicht lesbar"]


def check_repo(repo: Path) -> list[str]:
    if not repo.is_dir():
        return [f"{repo}: kein Verzeichnis"]
    problems: list[str] = []
    for path in _walk(repo):
        if fnmatch.fnmatch(path.name, "Dockerfile*"):
            problems += _checked(_check_dockerfile, repo, path)
        elif path.suffix in (".yml", ".yaml") and path.parent.parts[-2:] == (".github", "workflows"):
            problems += _checked(_check_workflow, repo, path)
        elif fnmatch.fnmatch(path.name, "docker-compose*.y*ml"):
            problems += _checked(_check_compose, repo, path)
        elif path.name in LOCK_NAMES:
            problems += _checked(_check_lock, repo, path)
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    problems = check_repo(args.repo.resolve())
    for problem in problems:
        print(f"FAIL {problem}")
    if not problems:
        print("OK   Pins (Images, Actions, Locks)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
