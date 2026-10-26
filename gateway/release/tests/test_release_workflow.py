"""Lieferkette des Gateway-Releases: Release-Werkzeug nur aus Hash-Lock-Dateien; die CI prueft nur (volle CI, Probelauf mit
Wegwerf-Kette, Gate mit Leserecht, Tag-Signatur und Fast-Forward von main hinter `environment: release`), baut und
signiert nichts fuer ein Release: das tut der Betreiber am eigenen Rechner (release_lokal.sh). Kein Signier-Secret, kein
`packages: write`, kein Werks-Image in der CI (FR-26). Root-Code (siblings.sh, Gate) laeuft nur im Job `gate` ohne
Schreibrecht (I-1)."""
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO / ".github" / "workflows"
WORKFLOW = WORKFLOWS / "release-gateway.yml"
LOCK = REPO / "gateway" / "release" / "requirements.txt"


def _locked(path: Path) -> dict[str, str]:
    return dict(re.findall(r"^([A-Za-z0-9._-]+)==([^\s\\]+)", path.read_text(), flags=re.MULTILINE))


def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def _runs(job: dict) -> list[str]:
    return [step["run"] for step in job.get("steps", []) if "run" in step]


def _index(job: dict, merkmal: str) -> int:
    treffer = [i for i, step in enumerate(job["steps"]) if merkmal in step.get("run", "")]
    assert len(treffer) == 1, f"{merkmal}: {len(treffer)} Schritte"
    return treffer[0]


def test_lock_has_the_versions_of_the_other_locks():
    release = _locked(LOCK)
    gateway = _locked(REPO / "gateway" / "requirements.txt")
    for name in ("cryptography", "cffi", "pycparser"):
        assert release[name] == gateway[name], name
    assert release["pyyaml"] == "6.0.3"  # wie release.yml des Add-ons
    assert set(release) == {"pyyaml", "cryptography", "cffi", "pycparser"}


def test_every_pip_install_uses_a_hash_lock():
    seen = {}
    for name, job in _jobs().items():
        for run in _runs(job):
            for line in run.splitlines():
                if re.search(r"\bpip3?\s+install\b", line):
                    seen[name] = line.strip()
                    assert "--require-hashes" in line, (name, line)
    assert seen["probe"].endswith("-r gateway/release/requirements.txt")
    assert seen["gate"].endswith('-r "$DEV_ROOT/tools/release/requirements.txt"') and "--no-deps" in seen["gate"]
    assert set(seen) == {"probe", "gate"}  # der Job mit Schreibrecht installiert nichts


def test_kein_job_hat_ein_secret_ausser_github_token():
    text = WORKFLOW.read_text()
    assert "MINISIGN" not in text and "minisign" not in text and "sign_bundle.sh" not in text
    # Einziges Secret: das Token fuer die Geschwister-Repos (siblings.sh, privates Root-Repo); alles andere ist github.token.
    assert set(re.findall(r"secrets\.(\w+)", text)) == {"CROSS_REPO_TOKEN"}
    jobs = _jobs()
    assert jobs["ci"]["secrets"] == "inherit"  # die volle CI braucht das Token der Geschwister-Repos
    assert "secrets." not in yaml.safe_dump(jobs["probe"])  # der Probelauf fuehrt Repo-Code aus: keine Secrets
    assert "secrets." not in yaml.safe_dump(jobs["main"])  # der Job mit Schreibrecht hat kein Secret (I-1)
    for name in ("probe", "gate", "main"):
        for step in jobs[name]["steps"]:
            if "secrets." in yaml.safe_dump(step):
                assert name == "gate" and "siblings.sh" in step["run"] and "pip" not in step["run"]
                assert step["env"] == {"CROSS_REPO_TOKEN": "${{ secrets.CROSS_REPO_TOKEN }}"}


def test_kein_job_hat_packages_write():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    assert "packages" not in (workflow.get("permissions") or {})
    for name, job in _jobs().items():
        assert "packages" not in (job.get("permissions") or {}), name
    schreibend = [name for name, job in _jobs().items() if "write" in (job.get("permissions") or {}).values()]
    assert schreibend == ["main"]


def test_main_braucht_gate_und_probe_und_environment():
    jobs = _jobs()
    assert set(jobs) == {"ci", "probe", "gate", "main"}
    assert jobs["probe"]["needs"] == "ci" and "environment" not in jobs["probe"]
    assert jobs["probe"]["permissions"] == {"contents": "read"}
    assert jobs["gate"]["needs"] == "ci" and "environment" not in jobs["gate"]
    assert jobs["gate"]["permissions"] == {"contents": "read"}
    assert jobs["main"]["needs"] == ["gate", "probe"]
    assert jobs["main"]["environment"] == "release"
    assert jobs["main"]["permissions"] == {"contents": "write"}
    assert "if" not in jobs["main"]  # auch im -dryrun: Tag-Pruefung und Gate laufen, nur Schreiben entfaellt


def test_probe_runs_the_local_script_with_a_test_chain_and_uploads_nothing():
    probe = _jobs()["probe"]
    runs = [run for run in _runs(probe) if "release_lokal.sh" in run]
    assert len(runs) == 1 and "ca.py test-kette" in runs[0]
    assert "--probe" in runs[0] and "--roots" in runs[0] and "--schluessel" in runs[0] and "--zertifikat" in runs[0]
    assert "SHG_BUNDLE" not in yaml.safe_dump(probe) and "scp" not in " ".join(runs).split()
    uses = [step.get("uses", "") for step in probe["steps"]]
    assert any(u.startswith("docker/setup-qemu-action@") for u in uses)
    assert any(u.startswith("docker/setup-buildx-action@") for u in uses)
    assert not any("upload-artifact" in u for u in uses)
    script = (REPO / "gateway" / "release" / "release_lokal.sh").read_text()
    assert "verify_bundle.py" in script and "signieren.py" in script


def test_probe_runs_for_dryrun_and_real_tags_alike():
    probe = _jobs()["probe"]
    assert "if" not in probe and all("if" not in step for step in probe["steps"])
    version = [step for step in probe["steps"] if "GITHUB_REF_NAME" in step.get("run", "")]
    assert len(version) == 1 and "-dryrun" in version[0]["run"]


def test_gate_steps_in_order():
    gate = _jobs()["gate"]
    order = [_index(gate, m) for m in ("tag_pruefen.sh", "siblings.sh", "pip install", "release_gate.py")]
    assert order == sorted(order) and len(set(order)) == 4
    assert gate["steps"][_index(gate, "release_gate.py")]["id"] == "gate"
    assert gate["outputs"] == {"dryrun": "${{ steps.gate.outputs.dryrun }}", "title": "${{ steps.gate.outputs.title }}"}


def test_main_steps_in_order_and_writes_only_for_real_tags():
    main = _jobs()["main"]
    order = [_index(main, m) for m in ("tag_pruefen.sh", "Gate meldet", "git push", "gh release create")]
    assert order == sorted(order) and len(set(order)) == 4
    for merkmal in ("git push", "gh release create"):
        assert main["steps"][_index(main, merkmal)]["if"] == "steps.gate.outputs.dryrun == 'false'"
    assert 'git push "https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git" "$GITHUB_SHA:refs/heads/main"' in (
        _runs(main)[_index_run(main, "git push")])
    assert main["steps"][_index(main, "Gate meldet")]["id"] == "gate"
    assert "siblings.sh" not in yaml.safe_dump(main) and "DEV_ROOT" not in yaml.safe_dump(main)


def _index_run(job: dict, merkmal: str) -> int:
    return [i for i, run in enumerate(_runs(job)) if merkmal in run][0]


def test_release_note_hangs_no_assets_on_the_release():
    main = _jobs()["main"]
    step = main["steps"][_index(main, "gh release create")]
    assert "--notes-file" in step["run"] and "--verify-tag" in step["run"]
    assert "manifest" not in step["run"] and "upload" not in step["run"]
    assert "bundle" not in step["run"] and ".img" not in step["run"]
    # Einziges Artefakt: die Release-Notiz des Gates (reine Daten); der Job laedt nichts hoch.
    assert not any("upload-artifact" in s.get("uses", "") for s in main["steps"])
    herunter = [s for s in main["steps"] if "download-artifact" in s.get("uses", "")]
    assert [s["with"]["name"] for s in herunter] == ["release-notiz"]


def test_kein_werks_image_in_der_ci():
    assert not (WORKFLOWS / "image-gateway.yml").exists()  # der Bau kommt lokal (AP-05a), nicht aus der CI (FR-26)
    jobs = _jobs()
    assert "image" not in jobs and "attach-image" not in jobs and "build" not in jobs and "sign" not in jobs
    for pfad in sorted(WORKFLOWS.glob("*.yml")):
        text = pfad.read_text()
        for merkmal in ("make_image.sh", "rpi-image-gen", "image-gateway", "gateway-bundle-signed", "ubuntu-24.04-arm",
                        "gh release upload"):
            assert merkmal not in text, (pfad.name, merkmal)
    for job in jobs.values():
        assert "uses" not in job or job["uses"] == "./.github/workflows/ci.yml"


def test_main_baut_und_signiert_nichts():
    # Der Bau fuehrt Repo-Code aus und bleibt im Job probe ohne Schreibrecht; main prueft nur und schreibt.
    main = _jobs()["main"]
    text = "\n".join(_runs(main))
    for merkmal in ("docker", "release_lokal.sh", "build_bundle", "ca.py", "signieren.py"):
        assert merkmal not in text, merkmal


def test_keine_release_pub_mehr_der_schluesselbund_heisst_release_roots_pem():
    assert not (REPO / "gateway" / "host" / "release.pub").exists()
    assert (REPO / "gateway" / "host" / "release-roots.pem").is_file()
    script = (REPO / "gateway" / "release" / "release_lokal.sh").read_text()
    assert "release.pub" not in script and "gateway/host/release-roots.pem" in script
