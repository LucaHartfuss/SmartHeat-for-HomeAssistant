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


IMAGE_WORKFLOW = REPO / ".github" / "workflows" / "image-gateway.yml"
OWN_API_URL = "https://accounts.hartfussha.org"


def test_image_jobs_never_see_secrets_and_only_attach_may_write():
    jobs = _jobs()
    assert jobs["image"]["uses"] == "./.github/workflows/image-gateway.yml"
    assert "secrets" not in jobs["image"] and "environment" not in jobs["image"]
    assert jobs["attach-image"]["permissions"] == {"contents": "write"}
    attach_runs = "\n".join(_runs(jobs["attach-image"]))
    assert "gh release upload" in attach_runs and "checkout" not in str(jobs["attach-image"]["steps"])
    image = yaml.safe_load(IMAGE_WORKFLOW.read_text())
    assert image["permissions"] == {"contents": "read"}
    assert "secrets." not in IMAGE_WORKFLOW.read_text()


def test_image_workflow_never_puts_inputs_into_scripts():
    image = yaml.safe_load(IMAGE_WORKFLOW.read_text())
    for job in image["jobs"].values():
        for run in _runs(job):
            assert "${{" not in run, run  # Eingaben nur ueber env:
    for job in _jobs().values():
        for run in _runs(job):
            assert "inputs." not in run, run


def test_sign_job_hands_the_signed_bundle_to_the_image_job():
    uploads = [step for step in _jobs()["sign"]["steps"] if "upload-artifact" in step.get("uses", "")]
    assert [step["with"]["name"] for step in uploads] == ["gateway-bundle-signed"]


def test_image_job_is_skipped_without_portal_variable_and_dryrun_is_not_verified():
    job = _jobs()["image"]
    assert "vars.SHG_PORTAL_BASE_URL != ''" in job["if"]
    assert job["with"]["verify"] == "${{ needs.build.outputs.dryrun != 'true' }}"
    assert job["with"]["portal_base_url"] == "${{ vars.SHG_PORTAL_BASE_URL }}"


def test_device_api_url_defaults_to_the_own_hostname_in_both_entry_points():
    # Nutzer-Vorgabe 2026-10-07: eigener Hostname, nie IP oder AWS-Adresse.
    release_url = _jobs()["image"]["with"]["device_api_url"]
    assert release_url == "${{ vars.SHG_DEVICE_API_URL || '" + OWN_API_URL + "' }}"
    image = yaml.safe_load(IMAGE_WORKFLOW.read_text())
    assert image[True]["workflow_dispatch"]["inputs"]["device_api_url"]["default"] == OWN_API_URL
    # Die Repo-Variable wird nur durchgereicht; ihre Pruefung macht der Fail-fast-Schritt im Image-Workflow.
    for path in (WORKFLOW, IMAGE_WORKFLOW):
        uses = re.findall(r"vars\.SHG_DEVICE_API_URL[^}]*", path.read_text())
        assert all(use.startswith("vars.SHG_DEVICE_API_URL || '" + OWN_API_URL + "'") for use in uses), path


def test_image_workflow_validates_both_urls_with_own_url_sh_before_building():
    image = yaml.safe_load(IMAGE_WORKFLOW.read_text())
    steps = image["jobs"]["image"]["steps"]
    index = [i for i, step in enumerate(steps) if "own_url.sh" in step.get("run", "")]
    assert len(index) == 1, "genau ein Fail-fast-Schritt mit own_url.sh erwartet"
    step = steps[index[0]]
    assert "shg_own_url_problem" in step["run"] and "API_URL" in step["run"] and "PORTAL_URL" in step["run"]
    # Nach dem Checkout (own_url.sh liegt im Repo), vor allem Teuren (Bundle-Download, Image-Bau).
    checkout = [i for i, s in enumerate(steps) if "actions/checkout" in s.get("uses", "")]
    build = [i for i, s in enumerate(steps) if "make_image.sh" in s.get("run", "")]
    assert checkout and build and checkout[0] < index[0] < build[0]
    assert all("download" not in s.get("uses", "") and "gh release download" not in s.get("run", "")
               for s in steps[: index[0]])
    # Keine Kopie der Regel im Workflow (keine Regex fuer Hostnamen, keine AWS-Liste).
    text = IMAGE_WORKFLOW.read_text()
    assert "amazonaws" not in text and "[a-z0-9" not in text
    assert image["jobs"]["image"]["env"]["API_URL"] == "${{ inputs.device_api_url }}"
    assert image["jobs"]["image"]["env"]["PORTAL_URL"] == "${{ inputs.portal_base_url }}"


def test_no_workflow_uses_an_ip_address_or_aws_host_as_url():
    for path in sorted((REPO / ".github" / "workflows").glob("*.yml")):
        for url in re.findall(r"https?://[^\s'\")]+", path.read_text()):
            host = re.sub(r"^https?://", "", url).split("/")[0].split(":")[0]
            assert not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", host), (path.name, url)
            assert "amazonaws.com" not in host, (path.name, url)
