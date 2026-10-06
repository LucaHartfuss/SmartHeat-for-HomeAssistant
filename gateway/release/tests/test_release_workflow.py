"""Lieferkette des Gateway-Releases (Final-Review FW-3, FW-4, FW-7): Release-Werkzeug nur aus der Hash-Lock-Datei,
Signier-Job prueft per Neubau, Artefakt ueberlebt das Warten auf die Freigabe."""
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
WORKFLOW = REPO / ".github" / "workflows" / "release-gateway.yml"
LOCK = REPO / "gateway" / "release" / "requirements.txt"


def _locked(path: Path) -> dict[str, str]:
    return dict(re.findall(r"^([A-Za-z0-9._-]+)==([^\s\\]+)", path.read_text(), flags=re.MULTILINE))


def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def _runs(job: dict) -> list[str]:
    return [step["run"] for step in job.get("steps", []) if "run" in step]


def test_lock_has_the_versions_of_the_other_locks():
    release = _locked(LOCK)
    gateway = _locked(REPO / "gateway" / "requirements.txt")
    for name in ("cryptography", "cffi", "pycparser"):
        assert release[name] == gateway[name], name
    assert release["pyyaml"] == "6.0.3"  # wie release.yml des Add-ons
    assert set(release) == {"pyyaml", "cryptography", "cffi", "pycparser"}


def test_every_pip_install_uses_the_hash_lock():
    seen = set()
    for name, job in _jobs().items():
        for run in _runs(job):
            for line in run.splitlines():
                if re.search(r"\bpip3?\s+install\b", line):
                    seen.add(name)
                    assert "--require-hashes" in line and "-r gateway/release/requirements.txt" in line, (name, line)
    assert seen == {"build", "sign"}


def test_sign_job_verifies_by_rebuilding_and_build_job_never_signs_for_real():
    jobs = _jobs()
    sign_runs = "\n".join(_runs(jobs["sign"]))
    assert "gateway/release/sign_bundle.sh" in sign_runs and "--dryrun" not in sign_runs
    build_signing = [run for run in _runs(jobs["build"]) if "sign_bundle.sh" in run]
    assert build_signing and all("--dryrun" in run for run in build_signing)
    assert "environment" not in jobs["build"] and jobs["sign"]["environment"] == "release"
    script = (REPO / "gateway" / "release" / "sign_bundle.sh").read_text()
    assert "verify_bundle.py" in script


def test_bundle_artifact_survives_a_reviewer_wait():
    uploads = [step for step in _jobs()["build"]["steps"] if "upload-artifact" in step.get("uses", "")]
    assert [step["with"]["retention-days"] for step in uploads] == [7]
