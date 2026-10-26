import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from smartheat_host import bundles

DIGEST = "sha256:" + "d" * 64
IMAGE = "smartheat-gateway@" + DIGEST
ARCHIV = {"datei": "gateway-image.oci.tar", "sha256": "e" * 64, "bytes": 1000, "name": "smartheat-gateway",
          "digest": DIGEST}
GOOD_COMPOSE = "services:\n  runtime:\n    image: " + IMAGE + "\n    pull_policy: never\n"


def _manifest(**over):
    data = {"version": "0.3.0", "min_updater_version": "0.2.0",
            "files": {"docker-compose.yml": "b" * 64, "mosquitto.conf": "c" * 64},
            "images": {"gateway": IMAGE}, "archiv": dict(ARCHIV)}
    data.update(over)
    return json.dumps(data).encode()


def _archiv(**over) -> bytes:
    return _manifest(archiv={**ARCHIV, **over})


def _ohne(*keys) -> bytes:
    data = json.loads(_manifest())
    for key in keys:
        del data[key]
    return json.dumps(data).encode()


def _archiv_ohne(key) -> bytes:
    archiv = dict(ARCHIV)
    del archiv[key]
    return _manifest(archiv=archiv)


def test_parse_manifest():
    manifest = bundles.parse_manifest(_manifest())
    assert manifest.version == "0.3.0" and set(manifest.files) == set(bundles.MANIFEST_FILES)
    assert manifest.archiv == bundles.Archiv(**ARCHIV)
    assert manifest.archiv.ref == IMAGE


def test_the_archive_may_be_exactly_as_large_as_the_limit():
    assert bundles.MAX_ARCHIVE_BYTES == 536870912
    assert bundles.parse_manifest(_archiv(bytes=bundles.MAX_ARCHIVE_BYTES)).archiv.bytes == 536870912
    assert bundles.parse_manifest(_archiv(bytes=1)).archiv.bytes == 1


@pytest.mark.parametrize("raw", [
    b"kaputt", _manifest(version="0.3"), _manifest(files={"docker-compose.yml": "b" * 64}),
    _manifest(files={"docker-compose.yml": "b" * 64, "mosquitto.conf": "c" * 64, "../x": "d" * 64}),
    _manifest(files={"docker-compose.yml": "zz", "mosquitto.conf": "c" * 64}),
    _manifest(images={"gateway": "ghcr.io/x/gw:latest"}),
    b"[1, 2]",
    # archiv: fehlt, kein Objekt, unvollstaendig oder mit Fremdfeld
    _ohne("archiv"), _manifest(archiv=None), _manifest(archiv="gateway-image.oci.tar"), _manifest(archiv=[]),
    *[_archiv_ohne(key) for key in ARCHIV], _archiv(extra="x"),
    # bytes: ueber der Grenze, null, negativ, Wahrheitswert, Text, Kommazahl
    _archiv(bytes=bundles.MAX_ARCHIVE_BYTES + 1), _archiv(bytes=0), _archiv(bytes=-5), _archiv(bytes=True),
    _archiv(bytes="1000"), _archiv(bytes=1000.5), _archiv(bytes=None),
    # digest: ohne sha256:, falsche Laenge, Grossbuchstaben, kein Text
    _archiv(digest="d" * 64), _archiv(digest="sha256:" + "d" * 63), _archiv(digest="sha256:" + "D" * 64),
    _archiv(digest="sha512:" + "d" * 64), _archiv(digest=None), _archiv(digest=DIGEST + "\n"),
    # sha256 der Datei, Name und Dateiname
    _archiv(sha256="zz"), _archiv(sha256="E" * 64), _archiv(sha256=None),
    _archiv(name="ghcr.io/x/gw"), _archiv(name="andere"), _archiv(name=None),
    _archiv(datei="../gateway-image.oci.tar"), _archiv(datei="andere.tar"), _archiv(datei=None),
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
    "services:\n  runtime:\n    image: ghcr.io/x/gw:latest\n",
    "services:\n  runtime:\n    build: .\n    image: " + IMAGE + "\n    pull_policy: never\n",
    "services:\n  runtime:\n    image: ghcr.io/x/gw:latest # @sha256:" + "a" * 64 + "\n",  # Kommentar versteckt kein Tag
    GOOD_COMPOSE + "  b:\n    image: ghcr.io/x/b:1\n",
    "services:\n  runtime:\n    image: ghcr.io/x/gw@sha256:abc\n",
    "services: {}\n",
])
def test_compose_without_digest_or_with_build_is_refused(text):
    with pytest.raises(bundles.ManifestError):
        bundles.check_compose(text, bundles.Archiv(**ARCHIV))


def test_good_compose_passes_and_lists_images():
    archiv = bundles.Archiv(**ARCHIV)
    bundles.check_compose(GOOD_COMPOSE, archiv)
    quoted = 'services:\n  runtime:\n    image: "' + IMAGE + '"\n    pull_policy: "never"\n'
    bundles.check_compose(quoted, archiv)
    assert bundles.compose_images(quoted) == [IMAGE]
    registry = "services:\n  runtime:\n    image: ghcr.io/x/gw@sha256:" + "a" * 64 + "\n"
    bundles.check_compose(registry, archiv)  # Images aus Registries brauchen kein never und kein Archiv


def _service(image, policy=None, name="runtime") -> str:
    return f"services:\n  {name}:\n    image: {image}\n" + (f"    pull_policy: {policy}\n" if policy else "")


@pytest.mark.parametrize("text", [
    _service("ghcr.io/x/gw@sha256:" + "a" * 64, "never"),  # never nur fuer das Archiv, sonst kein Image da
    _service("eclipse-mosquitto:2@sha256:" + "e" * 64, "never"),
    _service("smartheat-gateway@sha256:" + "f" * 64, "never"),  # eigener Name, aber nicht der Digest des Archivs
    _service("localhost:5000/smartheat-gateway@" + DIGEST, "never"),  # anderer Name (Registry)
    _service(IMAGE),  # Archiv-Image ohne never: compose pull fragte docker.io
    _service(IMAGE, "always"), _service(IMAGE, "missing"), _service(IMAGE, "if_not_present"),
    _service("smartheat-gateway@sha256:" + "f" * 64),  # eigener Name ohne never und mit fremdem Digest
    _service(IMAGE, "never") + "  zweiter:\n    image: ghcr.io/x/gw@sha256:" + "a" * 64 + "\n    pull_policy: never\n",
])
def test_compose_never_nur_mit_archiv_digest(text):
    with pytest.raises(bundles.ManifestError):
        bundles.check_compose(text, bundles.Archiv(**ARCHIV))


def test_compose_never_with_the_archive_digest_passes_for_every_service():
    two = _service(IMAGE, "never", "init") + "  runtime:\n    image: " + IMAGE + "\n    pull_policy: never\n"
    bundles.check_compose(two, bundles.Archiv(**ARCHIV))
    # ein anderer Archiv-Digest im Manifest macht dieselbe Datei ungueltig
    other = bundles.Archiv(**{**ARCHIV, "digest": "sha256:" + "9" * 64})
    with pytest.raises(bundles.ManifestError):
        bundles.check_compose(two, other)


def test_compose_services_reads_image_and_pull_policy_of_the_first_level_only():
    text = ("x-ignoriert: 1\nservices:\n  init:\n    image: a@sha256:" + "1" * 64 + "\n    pull_policy: never\n"
            "    healthcheck:\n      image: nicht-ein-image\n  runtime:\n    # Kommentar\n\n"
            "    image: \"b@sha256:" + "2" * 64 + "\" # mit Kommentar\n    pull_policy: 'never'\n"
            "  leer:\n    restart: no\nnetworks:\n  image: kein-dienst\n")
    assert bundles.compose_services(text) == {
        "init": {"image": "a@sha256:" + "1" * 64, "pull_policy": "never"},
        "runtime": {"image": "b@sha256:" + "2" * 64, "pull_policy": "never"},
        "leer": {},
    }
    assert bundles.compose_services("services: {}\n") == {}
    assert bundles.compose_services("nichts: 1\n") == {}


def test_store_installs_atomically_and_replaces(tmp_path):
    store = bundles.BundleStore(tmp_path)
    assert not store.exists("0.3.0")
    store.install("0.3.0", {"docker-compose.yml": GOOD_COMPOSE.encode(), "mosquitto.conf": b"x"})
    store.install("0.3.0", {"docker-compose.yml": GOOD_COMPOSE.encode(), "mosquitto.conf": b"y"})
    assert (tmp_path / "bundles" / "0.3.0" / "mosquitto.conf").read_bytes() == b"y"
    assert [p.name for p in (tmp_path / "bundles").iterdir()] == ["0.3.0"]
    assert store.exists("0.3.0")


def test_the_archive_lies_outside_the_bundle_folder_that_install_deletes(tmp_path):
    store = bundles.BundleStore(tmp_path)
    assert store.archive_path("0.3.0") == tmp_path / "archive" / "0.3.0.oci.tar"
    store.archive_path("0.3.0").parent.mkdir()
    store.archive_path("0.3.0").write_bytes(b"x")
    store.install("0.3.0", {"docker-compose.yml": GOOD_COMPOSE.encode(), "mosquitto.conf": b"x"})
    assert store.archive_path("0.3.0").read_bytes() == b"x"  # install loescht bundles/<version>, nicht archive/


def test_clean_archives_keeps_only_the_archive_and_the_part_file_of_one_version(tmp_path):
    store = bundles.BundleStore(tmp_path)
    folder = tmp_path / "archive"
    folder.mkdir()
    for name in ("0.2.0.oci.tar", "0.2.0.oci.tar.part", "0.3.0.oci.tar", "0.3.0.oci.tar.part", "rest", "0.3.0.oci.tar2"):
        (folder / name).write_bytes(b"x")
    (folder / "ordner").mkdir()
    store.clean_archives("0.3.0")
    assert sorted(path.name for path in folder.iterdir()) == ["0.3.0.oci.tar", "0.3.0.oci.tar.part", "ordner"]
    bundles.BundleStore(tmp_path / "leer").clean_archives("0.3.0")  # ohne Ordner kein Fehler
    store.clean_archives(None)  # None: alle Dateien
    assert sorted(path.name for path in folder.iterdir()) == ["ordner"]


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
    lines = '{"Service": "runtime", "Health": "healthy"}\n{"Service": "mosquitto", "Health": ""}\n'
    array = '[{"Service": "runtime", "Health": "healthy"}, {"Service": "zigbee2mqtt", "Health": "starting"}]'
    for out, expected in ((lines, {"runtime": "healthy"}),
                          (array, {"runtime": "healthy", "zigbee2mqtt": "starting"})):
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


def test_prune_matches_tagged_refs_and_registries_with_a_port(tmp_path):
    """Compose nennt Images mit Tag (eclipse-mosquitto:2@sha256:...), `docker image ls` zeigt das Repository ohne Tag;
    ein Registry-Port (localhost:5000/...) ist kein Tag."""
    keep = {"eclipse-mosquitto:2@sha256:" + "e" * 64, "koenkk/zigbee2mqtt:2.14.2@sha256:" + "d" * 64,
            "localhost:5000/smartheat-gateway@sha256:" + "a" * 64}
    listed = "\n".join([
        "eclipse-mosquitto@sha256:" + "e" * 64, "eclipse-mosquitto@sha256:" + "1" * 64,
        "koenkk/zigbee2mqtt@sha256:" + "d" * 64, "koenkk/zigbee2mqtt@sha256:" + "2" * 64,
        "localhost:5000/smartheat-gateway@sha256:" + "a" * 64, "localhost:5000/smartheat-gateway@sha256:" + "3" * 64,
        "localhost@sha256:" + "4" * 64, "fremd/bild@sha256:" + "5" * 64,
    ])
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return _Done(0, listed if cmd[:3] == ["docker", "image", "ls"] else "")

    bundles.ComposeRunner(tmp_path, run=run).prune(keep)
    removed = sorted(cmd[3] for cmd in calls if cmd[:3] == ["docker", "image", "rm"])
    assert removed == sorted([
        "eclipse-mosquitto@sha256:" + "1" * 64, "koenkk/zigbee2mqtt@sha256:" + "2" * 64,
        "localhost:5000/smartheat-gateway@sha256:" + "3" * 64])


def test_prune_behaelt_eigenes_image_der_vorigen_version(tmp_path):
    """Der Rueckweg startet die vorige Version ohne Netz: ihr Archiv-Image (smartheat-gateway@<Digest>) bleibt, ein
    aelteres verschwindet. `docker image ls` zeigt es mit Repository smartheat-gateway und seinem Index-Digest."""
    jetzt, vorige, aelter = (f"smartheat-gateway@sha256:{ziffer * 64}" for ziffer in "abc")
    listed = "\n".join([jetzt, vorige, aelter, "eclipse-mosquitto@sha256:" + "e" * 64])
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return _Done(0, listed if cmd[:3] == ["docker", "image", "ls"] else "")

    keep = {jetzt, vorige, "eclipse-mosquitto:2@sha256:" + "e" * 64}
    bundles.ComposeRunner(tmp_path, run=run).prune(keep)
    assert [cmd for cmd in calls if cmd[:3] == ["docker", "image", "rm"]] == [["docker", "image", "rm", aelter]]


@pytest.mark.parametrize(("ref", "repository"), [
    ("eclipse-mosquitto:2@sha256:" + "e" * 64, "eclipse-mosquitto"),
    ("eclipse-mosquitto@sha256:" + "e" * 64, "eclipse-mosquitto"),
    ("localhost:5000/smartheat-gateway@sha256:" + "a" * 64, "localhost:5000/smartheat-gateway"),
    ("localhost:5000/smartheat-gateway:1.2@sha256:" + "a" * 64, "localhost:5000/smartheat-gateway"),
    ("ghcr.io/x/gw:latest", "ghcr.io/x/gw"),
])
def test_image_repository_strips_only_the_tag(ref, repository):
    assert bundles.image_repository(ref) == repository


@pytest.mark.parametrize(("text", "paths"), [
    ("services:\n  z:\n    devices:\n    - /dev/zigbee:/dev/zigbee\n    environment: {}\n", ["/dev/zigbee"]),
    ("services:\n  z:\n    devices:\n      - \"/dev/a:/dev/a:rwm\"\n      - /dev/b\n    x: 1\n", ["/dev/a", "/dev/b"]),
    ('services:\n  z:\n    devices: ["/dev/zigbee:/dev/zigbee", \'/dev/c:/c\']\n', ["/dev/zigbee", "/dev/c"]),
    ("services:\n  z:\n    devices:\n    # Kommentar\n\n    - /dev/x:/dev/x # Stick\n  y:\n    devices: [/dev/y]\n",
     ["/dev/x", "/dev/y"]),
    ("services:\n  z:\n    devices:\n    - ${STICK}:/dev/zigbee\n    - vendor.com/class=all\n", []),
    ("services:\n  z:\n    image: x\n", []),
])
def test_compose_devices_lists_host_paths(text, paths):
    assert bundles.compose_devices(text) == paths


def test_compose_devices_does_not_see_a_multiline_flow_list():
    """Dokumentierte Grenze (Docstring von compose_devices): nur die Flussliste in einer Zeile wird erkannt, die
    mehrzeilige nicht - ein fehlender Stick bliebe dann unbemerkt. Verhalten festgehalten, nicht gutgeheissen."""
    one_line = "services:\n  z:\n    devices: [\"/dev/ttyACM9:/dev/zigbee\"]\n"
    multi_line = "services:\n  z:\n    devices: [\n      \"/dev/ttyACM9:/dev/zigbee\",\n    ]\n"
    assert bundles.compose_devices(one_line) == ["/dev/ttyACM9"]  # Gegenprobe: derselbe Stick, eine Zeile
    assert bundles.compose_devices(multi_line) == []


def test_store_keeps_manifest_and_signature(tmp_path):
    store = bundles.BundleStore(tmp_path)
    store.install("0.3.0", {"docker-compose.yml": GOOD_COMPOSE.encode(), "mosquitto.conf": b"x",
                            bundles.MANIFEST_NAME: b"{}", bundles.SIGNATURE_NAME: b"sig"})
    assert (tmp_path / "bundles" / "0.3.0" / "manifest.json.sig").read_bytes() == b"sig"
    assert (tmp_path / "bundles" / "0.3.0" / "manifest.json").read_bytes() == b"{}"
    assert store.exists("0.3.0")


class _Done:
    def __init__(self, returncode, stdout):
        self.returncode, self.stdout, self.stderr = returncode, stdout, ""


def test_compose_calls_have_a_timeout_and_a_timeout_is_a_compose_error(tmp_path):
    """Ein haengender `docker`-Aufruf (Netz weg mitten im `pull`, Daemon blockiert) hielt den Updater endlos fest.
    Jeder Aufruf traegt eine Frist; laeuft sie ab (Attrappe `run` wirft TimeoutExpired, wie `subprocess.run`), ist es ein
    ComposeError, bei `ps` eine leere Antwort."""
    seen = []

    def hanging(cmd, **kw):
        seen.append((cmd[cmd.index("-f") + 2:], kw.get("timeout")))  # Teil nach der Compose-Datei
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    runner = bundles.ComposeRunner(tmp_path, run=hanging)
    with pytest.raises(bundles.ComposeError, match="pull"):
        runner.pull("0.3.0")
    with pytest.raises(bundles.ComposeError, match="up"):
        runner.up("0.3.0")
    assert runner.health("0.3.0") == {}
    assert seen == [(["pull"], 900), (["up", "-d", "--remove-orphans"], 300), (["ps", "--format", "json"], 60)]


def test_a_successful_compose_call_passes_its_timeout_to_run_too(tmp_path):
    """Gegenprobe: auch ohne Ablauf geht die Frist an `run` (sonst waere sie ohne Wirkung)."""
    seen = []

    def run(cmd, **kw):
        seen.append(kw.get("timeout"))
        return _Done(0, "[]")

    runner = bundles.ComposeRunner(tmp_path, run=run)
    runner.pull("0.3.0")
    runner.up("0.3.0")
    runner.health("0.3.0")
    assert seen == [900, 300, 60]


def test_prune_calls_have_a_timeout_and_a_timeout_stops_the_clean_up_quietly(tmp_path):
    """`image ls` und `image rm` tragen je 120 s; ein Ablauf bricht das Aufraeumen ab wie ein fehlendes docker
    (OSError): kein Fehler, nichts weiter geloescht. Aufraeumen ist Beiwerk, das Update ist dann schon gesund."""
    keep = {"ghcr.io/x/gw@sha256:" + "a" * 64}
    listed = "\n".join(["ghcr.io/x/gw@sha256:" + "f" * 64, "ghcr.io/x/gw@sha256:" + "e" * 64])
    calls = []

    def run(cmd, **kw):
        calls.append((cmd[:3], kw.get("timeout")))
        return _Done(0, listed if cmd[:3] == ["docker", "image", "ls"] else "")

    bundles.ComposeRunner(tmp_path, run=run).prune(keep)
    assert calls == [(["docker", "image", "ls"], 120), (["docker", "image", "rm"], 120), (["docker", "image", "rm"], 120)]

    for hanging_command in (["docker", "image", "ls"], ["docker", "image", "rm"]):
        seen = []

        def hanging(cmd, hanging_command=hanging_command, seen=seen, **kw):
            seen.append(cmd[:3])
            if cmd[:3] == hanging_command:
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
            return _Done(0, listed)

        bundles.ComposeRunner(tmp_path, run=hanging).prune(keep)  # wirft nicht
        # beim haengenden `rm` endet das Aufraeumen nach dem ersten Versuch, das zweite Image bleibt
        assert seen == ([["docker", "image", "ls"]] if hanging_command[2] == "ls"
                        else [["docker", "image", "ls"], ["docker", "image", "rm"]])


def test_has_image_asks_docker_for_the_exact_reference(tmp_path):
    seen = []

    def run(cmd, **kw):
        seen.append((cmd, kw.get("timeout")))
        return _Done(0, "[]")

    assert bundles.ComposeRunner(tmp_path, run=run).has_image(IMAGE) is True
    assert seen == [(["docker", "image", "inspect", IMAGE], 120)]


def test_has_image_is_false_without_the_image_or_without_a_working_docker(tmp_path):
    assert bundles.ComposeRunner(tmp_path, run=lambda cmd, **kw: _Done(1, "")).has_image(IMAGE) is False

    def missing(cmd, **kw):
        raise FileNotFoundError("docker")

    def hanging(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    assert bundles.ComposeRunner(tmp_path, run=missing).has_image(IMAGE) is False
    assert bundles.ComposeRunner(tmp_path, run=hanging).has_image(IMAGE) is False


def test_docker_load_hat_eine_frist(monkeypatch):
    """Ein haengender `docker load` (Datentraeger, Daemon) hielte den Updater und den Erststart endlos fest: Standard
    600 s, die Frist geht an subprocess.run, ihr Ablauf ist ein LoadError."""
    seen = []

    def run(cmd, **kw):
        seen.append((cmd, kw.get("timeout")))
        return _Done(0, "Loaded image")

    monkeypatch.setattr(bundles.subprocess, "run", run)
    bundles.docker_load(Path("/x/y.tar"))
    bundles.docker_load(Path("/x/y.tar"), timeout=5)
    assert seen == [(["docker", "load", "-i", "/x/y.tar"], 600), (["docker", "load", "-i", "/x/y.tar"], 5)]

    def hanging(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    monkeypatch.setattr(bundles.subprocess, "run", hanging)
    with pytest.raises(bundles.LoadError, match="Frist von 600 s"):
        bundles.docker_load(Path("/x/y.tar"))
    with pytest.raises(bundles.LoadError, match="Frist von 5 s"):
        bundles.docker_load(Path("/x/y.tar"), timeout=5)


def test_docker_load_failures_are_load_errors_with_the_file_name_only(monkeypatch):
    failed = _Done(1, "")
    failed.stderr = "no space left on device"
    monkeypatch.setattr(bundles.subprocess, "run", lambda cmd, **kw: failed)
    with pytest.raises(bundles.LoadError, match=r"y\.tar: no space left"):
        bundles.docker_load(Path("/geheim/pfad/y.tar"))

    def missing(cmd, **kw):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(bundles.subprocess, "run", missing)
    with pytest.raises(bundles.LoadError, match=r"y\.tar: FileNotFoundError") as error:
        bundles.docker_load(Path("/geheim/pfad/y.tar"))
    assert "geheim" not in str(error.value)


def test_the_bundle_fixture_of_the_installer_tests_is_a_bundle_the_device_accepts():
    """tests/fixtures/bundle (install_checks.sh, test_image_scripts.py) folgt dem Format des Geraets: Manifest mit archiv,
    Compose-Datei mit dem Archiv-Image und pull_policy: never, Pruefsummen der Dateien stimmen."""
    ordner = Path(__file__).parent / "fixtures" / "bundle"
    manifest = bundles.parse_manifest((ordner / "manifest.json").read_bytes())
    bundles.check_compose((ordner / "docker-compose.yml").read_text(), manifest.archiv)
    for name, sha in manifest.files.items():
        assert hashlib.sha256((ordner / name).read_bytes()).hexdigest() == sha, name
    assert manifest.images["gateway"] == manifest.archiv.ref
