import json

import pytest

from smartheat_host import bundles

GOOD_COMPOSE = "services:\n  agent:\n    image: ghcr.io/x/gw@sha256:" + "a" * 64 + "\n"


def _manifest(**over):
    data = {"version": "0.3.0", "min_updater_version": "0.2.0",
            "files": {"docker-compose.yml": "b" * 64, "mosquitto.conf": "c" * 64},
            "images": {"gateway": "ghcr.io/x/gw@sha256:" + "a" * 64}}
    data.update(over)
    return json.dumps(data).encode()


def test_parse_manifest():
    manifest = bundles.parse_manifest(_manifest())
    assert manifest.version == "0.3.0" and set(manifest.files) == set(bundles.MANIFEST_FILES)


@pytest.mark.parametrize("raw", [
    b"kaputt", _manifest(version="0.3"), _manifest(files={"docker-compose.yml": "b" * 64}),
    _manifest(files={"docker-compose.yml": "b" * 64, "mosquitto.conf": "c" * 64, "../x": "d" * 64}),
    _manifest(files={"docker-compose.yml": "zz", "mosquitto.conf": "c" * 64}),
    _manifest(images={"gateway": "ghcr.io/x/gw:latest"}),
    b"[1, 2]",
])
def test_bad_manifests(raw):
    with pytest.raises(bundles.ManifestError):
        bundles.parse_manifest(raw)


def test_parse_version():
    assert bundles.parse_version("1.20.3") == (1, 20, 3)
    for bad in ("1.2", "1.2.3.4", "v1.2.3", "1.2.x", "../1.2.3", ""):
        with pytest.raises(bundles.ManifestError):
            bundles.parse_version(bad)


@pytest.mark.parametrize("text", [
    "services:\n  agent:\n    image: ghcr.io/x/gw:latest\n",
    "services:\n  agent:\n    build: .\n    image: ghcr.io/x/gw@sha256:" + "a" * 64 + "\n",
    "services:\n  agent:\n    image: ghcr.io/x/gw:latest # @sha256:" + "a" * 64 + "\n",  # Kommentar versteckt kein Tag
    "services:\n  agent:\n    image: ghcr.io/x/gw@sha256:" + "a" * 64 + "\n  b:\n    image: ghcr.io/x/b:1\n",
    "services:\n  agent:\n    image: ghcr.io/x/gw@sha256:abc\n",
    "services: {}\n",
])
def test_compose_without_digest_or_with_build_is_refused(text):
    with pytest.raises(bundles.ManifestError):
        bundles.check_compose(text)


def test_good_compose_passes_and_lists_images():
    bundles.check_compose(GOOD_COMPOSE)
    quoted = 'services:\n  agent:\n    image: "ghcr.io/x/gw@sha256:' + "a" * 64 + '"\n'
    bundles.check_compose(quoted)
    assert bundles.compose_images(quoted) == ["ghcr.io/x/gw@sha256:" + "a" * 64]


def test_store_installs_atomically_and_replaces(tmp_path):
    store = bundles.BundleStore(tmp_path)
    assert not store.exists("0.3.0")
    store.install("0.3.0", {"docker-compose.yml": GOOD_COMPOSE.encode(), "mosquitto.conf": b"x"})
    store.install("0.3.0", {"docker-compose.yml": GOOD_COMPOSE.encode(), "mosquitto.conf": b"y"})
    assert (tmp_path / "bundles" / "0.3.0" / "mosquitto.conf").read_bytes() == b"y"
    assert [p.name for p in (tmp_path / "bundles").iterdir()] == ["0.3.0"]
    assert store.exists("0.3.0")


def test_compose_runner_commands(tmp_path):
    calls = []
    runner = bundles.ComposeRunner(tmp_path, run=lambda cmd, **kw: calls.append(cmd) or _Done(0, ""))
    runner.pull("0.3.0")
    runner.up("0.3.0")
    compose = str(tmp_path / "bundles" / "0.3.0" / "docker-compose.yml")
    env = str(tmp_path / "host" / "gateway.env")
    base = ["docker", "compose", "-p", "smartheat", "--env-file", env, "-f", compose]
    assert calls == [base + ["pull"], base + ["up", "-d", "--remove-orphans"]]


def test_compose_health_reads_both_output_formats(tmp_path):
    lines = '{"Service": "agent", "Health": "healthy"}\n{"Service": "mosquitto", "Health": ""}\n'
    array = '[{"Service": "agent", "Health": "healthy"}, {"Service": "runtime", "Health": "starting"}]'
    for out, expected in ((lines, {"agent": "healthy"}), (array, {"agent": "healthy", "runtime": "starting"})):
        runner = bundles.ComposeRunner(tmp_path, run=lambda cmd, out=out, **kw: _Done(0, out))
        assert runner.health("0.3.0") == expected


def test_compose_health_failure_is_empty(tmp_path):
    for done in (_Done(1, "boom"), _Done(0, "{kaputt")):
        assert bundles.ComposeRunner(tmp_path, run=lambda cmd, done=done, **kw: done).health("0.3.0") == {}

    def missing(cmd, **kw):
        raise FileNotFoundError("docker")

    assert bundles.ComposeRunner(tmp_path, run=missing).health("0.3.0") == {}


def test_compose_failure_raises(tmp_path):
    runner = bundles.ComposeRunner(tmp_path, run=lambda cmd, **kw: _Done(1, "boom"))
    with pytest.raises(bundles.ComposeError):
        runner.up("0.3.0")


def test_missing_docker_is_a_compose_error(tmp_path):
    def missing(cmd, **kw):
        raise FileNotFoundError("docker")

    with pytest.raises(bundles.ComposeError):
        bundles.ComposeRunner(tmp_path, run=missing).up("0.3.0")


def test_prune_removes_only_unreferenced_images_of_the_bundle_repositories(tmp_path):
    keep = {"ghcr.io/x/gw@sha256:" + "a" * 64, "eclipse-mosquitto@sha256:" + "e" * 64}
    listed = "\n".join([
        "ghcr.io/x/gw@sha256:" + "a" * 64, "ghcr.io/x/gw@sha256:" + "f" * 64,
        "eclipse-mosquitto@sha256:" + "e" * 64, "fremd/bild@sha256:" + "1" * 64,
    ])
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return _Done(0, listed if cmd[:3] == ["docker", "image", "ls"] else "")

    bundles.ComposeRunner(tmp_path, run=run).prune(keep)
    assert ["docker", "image", "rm", "ghcr.io/x/gw@sha256:" + "f" * 64] in calls
    assert not any(cmd[:3] == ["docker", "image", "rm"] and "fremd" in cmd[3] for cmd in calls)
    assert len([cmd for cmd in calls if cmd[:3] == ["docker", "image", "rm"]]) == 1


class _Done:
    def __init__(self, returncode, stdout):
        self.returncode, self.stdout, self.stderr = returncode, stdout, ""
