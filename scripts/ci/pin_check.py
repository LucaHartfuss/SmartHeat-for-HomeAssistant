#!/usr/bin/env python3
"""Pin-Check (docs/archiv/superpowers/specs/2026-09-30-tp12e-vertraege-tests-lieferkette-design.md Abschnitt 3):
Jede Basis-Image-Angabe hat einen Digest, jede GitHub-Action eine 40-stellige SHA, jede Lock-Datei
Hashes und passt zu ihrer pyproject.toml; die Hash-Locks (HASH_LOCK_NAMES: hypothesis, Bauwerkzeug,
Dev-Werkzeuge) sind exakt gepinnt und gehasht, ohne Abgleich mit pyproject.toml. In Workflows und Dockerfiles laeuft
jedes pip install mit --require-hashes oder traegt die Markierung `# pin-check: ungehasht (<Grund>)`. Jedes `image:`
und `container:` (Workflows, Compose-Dateien) hat einen Digest. Nur Standardbibliothek; kanonisch in
HomeAssistant_Dev_Root/tools/pin_check.py, identische Kopien in den drei anderen Repos (Add-on, Integration,
HomeAssistantPapa) unter scripts/ci/pin_check.py (Root-check.sh Schritt "copies" prueft das). Der Lauf ueber die Wurzel
des Kern-Repos erfasst auch den Ordner HeizungssteuerungServer.

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
# Eigene Hash-Locks neben den Laufzeit-Locks: hypothesis (Installation mit --require-hashes --no-deps), das Bauwerkzeug
# (build-requirements.lock) und die Dev-Werkzeuge des Servers samt Laufzeit (requirements-dev.lock) sowie die
# Werkzeug-Locks des Dev-Roots (requirements-dev.txt, requirements-audit.txt): jeder Eintrag exakt gepinnt und gehasht,
# aber kein Abgleich mit pyproject.toml (dessen dependencies sind Laufzeit).
HASH_LOCK_NAMES = ("requirements-hypothesis.lock", "build-requirements.lock", "requirements-dev.lock",
                   "requirements-dev.txt", "requirements-audit.txt")
# .devtools, backups und .remember sind Root-spezifische, per .gitignore ausgeschlossene Ordner. Jedes docs/archiv/ (des
# Repos und eines Ordners darin, etwa HeizungssteuerungServer/docs/archiv/) ist abgeschlossen (das archivierte
# probe_5c-Dockerfile ohne Hashes wird nie gebaut) und wird uebersprungen.
_SKIP_DIRS = {".git", ".venv", ".worktrees", "node_modules", "__pycache__", ".devtools", "backups", ".remember"}
_ARCHIVE = ("docs", "archiv")
DOCKERFILE_NAMES = ("Dockerfile*", "*.Dockerfile", "Containerfile*")
COMPOSE_NAMES = ("docker-compose*.y*ml", "compose*.y*ml")
_FROM_LINE = re.compile(r"^\s*FROM(?:\s|$)", re.IGNORECASE)
_FROM = re.compile(r"^\s*FROM\s+(?:--\S+\s+)*(\S+)(?:\s+AS\s+(\S+))?\s*$", re.IGNORECASE)
_PIP_INSTALL = re.compile(r"\bpip3?\s+install\b")
# Workflows: jedes pip install laeuft mit --require-hashes oder ist ausdruecklich markiert (CI-Jobs
# ohne Schreibrecht, Testumgebungen mit Versionsbereichen). Ein Workflow mit Schreibrecht traegt nie die Markierung
# (F-werkzeuge-54): Schreibrecht ist jeder Bereich mit `write` (`contents`,
# `packages`, `id-token`, ...) und `write-all`, an Job oder Datei, auch mit Schluessel oder Wert in Anfuehrungszeichen;
# ein Workflow ohne `permissions:` auf oberster Ebene gilt als schreibend (ein Job ohne eigenes `permissions:` hat dann
# das Standardrecht des Tokens, fail closed). Der Check liest zeilenweise und gilt fuer die ganze Datei.
UNHASHED_MARKER = "pin-check: ungehasht"
_PIP_WITHOUT_HASHES = f"pip install ohne --require-hashes (oder Markierung '# {UNHASHED_MARKER} (Grund)')"
_WRITE_PERMISSION = re.compile(r"""["']?[\w-]+["']?\s*:\s*["']?write\b""")
_TOP_LEVEL_PERMISSIONS = re.compile(r"""^["']?permissions["']?\s*:""")
_USES = re.compile(r"^\s*(?:-\s*)?uses:\s*[\"']?([^\s\"']+)[\"']?(.*)$")
# `image:` und `container:` als Schluessel am Zeilenanfang (auch als Listenelement) oder in einem Flow-Mapping
# (`container: {image: x, ...}`); ohne Wert (Job namens `image:`, Mapping folgt) kein Treffer. Ein Wert, der mit `{`
# beginnt, ist ein Flow-Mapping und wird ueber seinen eigenen `image:`-Schluessel gefunden.
_IMAGE = re.compile(r"""(?:^\s*(?:-\s*)?(?:image|container)|[{,]\s*image)\s*:\s*(?:["']([^"']+)["']|(?!\{)([^\s,}"']+))""")
_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_REQUIREMENT = re.compile(rf"^({_NAME})(?:\[[^\]]*\])?==([^\s;\\]+)")
_LOCK_ENTRY = re.compile(rf"^({_NAME})")
_DECLARED = re.compile(rf"^\s*({_NAME})\s*(?:\[[^\]]*\])?\s*(.*)$", re.DOTALL)
_CLAUSE = re.compile(r"^(>=|<=|==|!=|~=|<|>)\s*(\d+(?:\.\d+)*)$")
_PLAIN_VERSION = re.compile(r"^\d+(?:\.\d+)*$")


def _walk(repo: Path) -> Iterator[Path]:
    """Alle Dateien unterhalb von `repo` in stabiler Reihenfolge. Uebersprungen werden Werkzeug-Ordner
    und eigene Git-Repos/Worktrees (im Dev-Root sind die drei Unter-Repos so eingebettet) - deren Dateien
    prueft der Pin-Check des jeweiligen Repos."""
    for directory, subdirs, names in os.walk(repo):
        here = Path(directory)
        subdirs[:] = sorted(
            sub for sub in subdirs if sub not in _SKIP_DIRS and not (here / sub / ".git").exists()
            and (here / sub).relative_to(repo).parts[-2:] != _ARCHIVE
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
        if _PIP_INSTALL.search(line) and "--require-hashes" not in line and UNHASHED_MARKER not in line:
            problems.append(f"{name}: {_PIP_WITHOUT_HASHES}")
    return problems


def _without_comment(line: str) -> str:
    """Zeile ohne YAML-Kommentar: `#` beginnt ihn nur am Zeilenanfang oder nach Leerraum und ausserhalb von
    Anfuehrungszeichen; in einem doppelt quotierten String beendet `\\"` ihn nicht."""
    quote = ""
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
        elif quote:
            escaped = quote == '"' and char == "\\"
            quote = "" if char == quote else quote
        elif char in "\"'":
            quote = char
        elif char == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index]
    return line


def _check_workflow(repo: Path, path: Path) -> list[str]:
    problems = []
    name = path.relative_to(repo)
    lines = path.read_text().splitlines()
    code = [_without_comment(line) for line in lines]
    top_level = any(_TOP_LEVEL_PERMISSIONS.match(line) for line in code)
    writes = not top_level or any(_WRITE_PERMISSION.search(line) for line in code)
    reason = "Schreibrecht" if top_level else "Schreibrecht (ohne permissions: auf oberster Ebene)"
    for line in lines:
        if writes and UNHASHED_MARKER in line:
            problems.append(f"{name}: Workflow mit {reason} darf keine Markierung '{UNHASHED_MARKER}' tragen "
                            "(pip install dort mit --require-hashes)")
        if (not line.lstrip().startswith("#") and _PIP_INSTALL.search(line)
                and "--require-hashes" not in line and UNHASHED_MARKER not in line):
            problems.append(f"{name}: {_PIP_WITHOUT_HASHES}")
        problems += _image_problems(name, line)
        match = _USES.match(line)
        if match is None or match.group(1).startswith("./"):
            continue
        reference, rest = match.group(1), match.group(2)
        if ACTION_SHA.search(reference) is None:
            problems.append(f"{name}: uses: {reference} ohne 40-stellige SHA")
        elif re.search(r"#\s*\S", rest) is None:
            problems.append(f"{name}: uses: {reference} ohne Versionskommentar (# v5)")
    return problems


def _image_problems(name: Path, line: str) -> list[str]:
    """Verstoesse einer Zeile gegen die Digest-Regel fuer `image:`/`container:` (Workflows, Compose-Dateien)."""
    problems = []
    for match in _IMAGE.finditer(_without_comment(line)):
        image = match.group(1) or match.group(2)
        if DIGEST.search(image) is None:
            problems.append(f"{name}: image: {image} ohne @sha256-Digest")
    return problems


def _check_compose(repo: Path, path: Path) -> list[str]:
    name = path.relative_to(repo)
    problems = []
    for line in path.read_text().splitlines():
        problems += _image_problems(name, line)
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


def _check_hash_lock(repo: Path, path: Path) -> list[str]:
    _, problems = _parse_lock(path)
    return [f"{path.relative_to(repo)}: {problem}" for problem in problems]


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
        if any(fnmatch.fnmatch(path.name, pattern) for pattern in DOCKERFILE_NAMES):
            problems += _checked(_check_dockerfile, repo, path)
        elif path.suffix in (".yml", ".yaml") and path.parent.parts[-2:] == (".github", "workflows"):
            problems += _checked(_check_workflow, repo, path)
        elif any(fnmatch.fnmatch(path.name, pattern) for pattern in COMPOSE_NAMES):
            problems += _checked(_check_compose, repo, path)
        elif path.name in LOCK_NAMES:
            problems += _checked(_check_lock, repo, path)
        elif path.name in HASH_LOCK_NAMES:
            problems += _checked(_check_hash_lock, repo, path)
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
