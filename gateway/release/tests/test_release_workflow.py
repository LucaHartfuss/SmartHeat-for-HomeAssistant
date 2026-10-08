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


def test_image_job_runs_only_after_a_real_signing_and_never_in_dryrun():
    # Das Bundle des -dryrun verweist auf localhost:5000 (Service-Container des Jobs build): dort kein Image-Bau.
    job = _jobs()["image"]
    condition = job["if"]
    for part in ("!cancelled()", "needs.build.result == 'success'", "vars.SHG_PORTAL_BASE_URL != ''",
                 "needs.sign.result == 'success'"):
        assert part in condition, part
    assert "dryrun" not in condition
    assert job["permissions"] == {"contents": "read"}
    assert job["needs"] == ["build", "sign"]
    assert job["with"]["artifact"] == "gateway-bundle-signed"
    assert "verify" not in job["with"]  # kein Schalter mehr: immer gegen release.pub
    assert job["with"]["portal_base_url"] == "${{ vars.SHG_PORTAL_BASE_URL }}"


def test_sign_job_takes_version_title_and_notes_from_the_tag_not_from_build():
    # Audit 4, A4-36 (GW-9): build fuehrt Fremdcode aus; seine Ausgaben duerfen Version, Titel und Notizen nicht setzen.
    jobs = _jobs()
    sign_steps = yaml.safe_dump(jobs["sign"]["steps"])
    assert "needs.build.outputs" not in sign_steps
    assert "needs.build.outputs" not in yaml.safe_dump(jobs["attach-image"]["steps"])
    tag_step = next(step for step in jobs["sign"]["steps"] if step.get("id") == "tag")
    assert "gateway/VERSION" in tag_step["run"] and "changelog_section.py" in tag_step["run"]
    assert jobs["sign"]["outputs"]["version"] == "${{ steps.tag.outputs.version }}"


def test_attach_image_gate_and_scope():
    job = _jobs()["attach-image"]
    assert "needs.build.outputs.dryrun == 'false'" in job["if"]
    assert "needs.image.result == 'success'" in job["if"]
    assert job["needs"] == ["build", "sign", "image"]
    assert not any("checkout" in step.get("uses", "") for step in job["steps"])


def _attach_script() -> str:
    step = [s for s in _jobs()["attach-image"]["steps"] if "gh release upload" in s.get("run", "")][0]
    assert step["env"]["VERSION"] == "${{ needs.sign.outputs.version }}"
    assert "${{" not in step["run"]
    return step["run"]


def test_attach_image_uploads_exactly_the_two_named_files_without_blanket_clobber():
    script = _attach_script()
    assert '"$RUNNER_TEMP"/image/*' not in script and "image/*" not in script
    uploads = [line for line in script.splitlines() if "gh release upload" in line]
    assert len(uploads) == 1
    assert '"$dir/$img" "$dir/$sum"' in uploads[0] and uploads[0].count("--clobber") <= 1
    assert 'img="smartheat-gateway-$VERSION.img.zst"' in script and 'sum="$img.sha256"' in script


def _run_attach(tmp_path, files: list[str], version="1.2.3"):
    import os
    import subprocess

    image = tmp_path / "image"
    image.mkdir()
    for name in files:
        (image / name).write_text("x")
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "gh").write_text('#!/bin/sh\necho "$@" > "$RUNNER_TEMP/gh.args"\n')
    (fake / "gh").chmod(0o755)
    env = {**os.environ, "RUNNER_TEMP": str(tmp_path), "VERSION": version, "GITHUB_REF_NAME": "gateway-v1.2.3",
           "GITHUB_REPOSITORY": "o/r", "PATH": f"{fake}:{os.environ['PATH']}"}
    result = subprocess.run(["bash", "-eo", "pipefail", "-c", _attach_script()], env=env, capture_output=True, text=True)
    return result, tmp_path / "gh.args"


def test_attach_image_guard_script_accepts_exactly_the_two_files(tmp_path):
    result, args = _run_attach(tmp_path, ["smartheat-gateway-1.2.3.img.zst", "smartheat-gateway-1.2.3.img.zst.sha256"])
    assert result.returncode == 0, result.stderr
    uploaded = args.read_text()
    assert uploaded.count("smartheat-gateway-1.2.3.img.zst") == 2 and "image/*" not in uploaded


def test_attach_image_guard_script_rejects_extra_missing_and_bad_version(tmp_path):
    good = ["smartheat-gateway-1.2.3.img.zst", "smartheat-gateway-1.2.3.img.zst.sha256"]
    cases = {
        "extra": (good + ["manifest.json"], "1.2.3"),
        "missing": (good[:1], "1.2.3"),
        "pilot": (["smartheat-gateway-1.2.3-pilot.img.zst", good[1]], "1.2.3"),
        "version": (good, "1.2.3-dryrun"),
    }
    for name, (files, version) in cases.items():
        case = tmp_path / name
        case.mkdir()
        result, args = _run_attach(case, files, version)
        assert result.returncode != 0, name
        assert not args.exists(), name  # nichts hochgeladen


def _image_check_script() -> tuple[dict, str]:
    image = yaml.safe_load(IMAGE_WORKFLOW.read_text())
    steps = image["jobs"]["image"]["steps"]
    step = [s for s in steps if "own_url.sh" in s.get("run", "")][0]
    return step, step["run"]


def _run_check(tmp_path, api, portal, tag="", event="workflow_call"):
    import os
    import subprocess

    env = {**os.environ, "API_URL": api, "PORTAL_URL": portal, "TAG": tag, "EVENT": event}
    return subprocess.run(["bash", "-eo", "pipefail", "-c", _image_check_script()[1]], env=env, cwd=REPO,
                          capture_output=True, text=True)


def test_image_check_step_rejects_bad_urls_and_tags(tmp_path):
    ok = "https://accounts.hartfussha.org"
    assert _run_check(tmp_path, ok, "https://portal.hartfussha.org").returncode == 0
    assert _run_check(tmp_path, ok, "https://portal.hartfussha.org", "gateway-v1.2.3", "workflow_dispatch").returncode == 0
    for api, portal in (("https://3.120.4.5", ok), (ok, "https://x.eu-central-1.amazonaws.com"), ("", ok)):
        result = _run_check(tmp_path, api, portal)
        assert result.returncode != 0
        assert "3.120.4.5" not in result.stdout + result.stderr and "amazonaws" not in result.stdout + result.stderr
    for api, portal in (("https://accounts.example.test", ok), (ok, "https://hartfussha.org.evil.com")):
        result = _run_check(tmp_path, api, portal)
        assert result.returncode != 0 and "Zone hartfussha.org" in result.stdout, result.stdout
    assert _run_check(tmp_path, "https://ACCOUNTS.HARTFUSSHA.ORG", "https://portal.hartfussha.org").returncode == 0
    for tag in ("", "main", "gateway-v1.2", "gateway-v1.2.3-dryrun", "gateway-v1.2.3\nfoo", "../gateway-v1.2.3"):
        assert _run_check(tmp_path, ok, ok, tag, "workflow_dispatch").returncode != 0, tag


def test_image_check_step_tag_comes_from_env_and_checkout_uses_the_tag_ref():
    _, script = _image_check_script()
    image = yaml.safe_load(IMAGE_WORKFLOW.read_text())
    env = image["jobs"]["image"]["env"]
    assert env["TAG"] == "${{ inputs.tag }}" and env["EVENT"] == "${{ github.event_name }}"
    assert "ARTIFACT" not in env and "FROM_RELEASE" not in env
    assert "gateway-v[0-9]+" in script
    checkout = [s for s in image["jobs"]["image"]["steps"] if "actions/checkout" in s.get("uses", "")][0]
    assert checkout["with"]["ref"] == "${{ github.event_name == 'workflow_dispatch' && format('refs/tags/{0}', inputs.tag) || github.sha }}"


def test_release_build_job_checks_the_urls_early_so_the_dryrun_exercises_the_rule():
    steps = _jobs()["build"]["steps"]
    index = [i for i, s in enumerate(steps) if "own_url.sh" in s.get("run", "")]
    assert len(index) == 1
    step = steps[index[0]]
    assert "shg_own_url_problem" in step["run"] and "${{" not in step["run"]
    assert step["env"]["API_URL"] == "${{ vars.SHG_DEVICE_API_URL || '" + OWN_API_URL + "' }}"
    assert step["env"]["PORTAL_URL"] == "${{ vars.SHG_PORTAL_BASE_URL }}"
    first_expensive = [i for i, s in enumerate(steps) if "setup-qemu" in s.get("uses", "") or "pip install" in s.get("run", "")]
    checkout = [i for i, s in enumerate(steps) if "actions/checkout" in s.get("uses", "")]
    assert checkout[0] < index[0] < min(first_expensive)


def test_release_check_step_rejects_names_outside_the_own_zone():
    import os
    import subprocess

    step = [s for s in _jobs()["build"]["steps"] if "own_url.sh" in s.get("run", "")][0]

    def run(api, portal):
        env = {**os.environ, "API_URL": api, "PORTAL_URL": portal}
        return subprocess.run(["bash", "-eo", "pipefail", "-c", step["run"]], env=env, cwd=REPO,
                              capture_output=True, text=True)

    assert run(OWN_API_URL, "https://portal.hartfussha.org").returncode == 0
    assert run(OWN_API_URL, "").returncode == 0  # Portal-Variable noch nicht gesetzt: wird uebersprungen
    for api, portal in (("https://accounts.example.test", ""), (OWN_API_URL, "https://evilhartfussha.org")):
        result = run(api, portal)
        assert result.returncode != 0 and "Zone hartfussha.org" in result.stdout, result.stdout


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


def test_image_workflow_always_verifies_the_signature_against_release_pub():
    image = yaml.safe_load(IMAGE_WORKFLOW.read_text())
    assert "verify" not in image[True]["workflow_call"]["inputs"]  # kein Schalter zum Abschalten
    assert "VERIFY" not in image["jobs"]["image"]["env"]
    step = [s for s in image["jobs"]["image"]["steps"] if "release.pub" in s.get("run", "")]
    assert len(step) == 1 and "if" not in step[0]
