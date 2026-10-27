import hashlib
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml
from oci_helper import ANNOTATION, make_oci_archive

from build_bundle import (
    GATEWAY_SERVICES,
    MIN_UPDATER_VERSION,
    build,
    main,
    min_updater_version,
    oci_digest,
    render_compose,
)
from smartheat_host import bundles

GW = Path(__file__).resolve().parents[2]
COMPOSE = GW / "compose" / "docker-compose.yml"
MOSQUITTO_CONF = GW / "compose" / "mosquitto.conf"
IMAGE = "smartheat-gateway@sha256:" + "a" * 64


class Archivdatei(type(Path())):
    """Pfad eines Test-Archivs mit seinem Index-Digest."""
    digest: str


@pytest.fixture
def archive(tmp_path):
    """Ein Archiv wie von buildx (ohne Namensannotation); `archive.digest` ist sein Index-Digest."""
    path = Archivdatei(tmp_path / "image.oci.tar")
    path.digest = make_oci_archive(path)
    return path


def _index(path: Path) -> dict:
    with tarfile.open(path) as tar:
        return json.loads(tar.extractfile("index.json").read())


def _blobs(path: Path) -> dict[str, bytes]:
    with tarfile.open(path) as tar:
        return {m.name: tar.extractfile(m).read() for m in tar if m.name.startswith("blobs/") and m.isfile()}


def test_render_replaces_build_by_the_digest_for_every_gateway_service():
    text = render_compose(COMPOSE.read_text(), IMAGE)
    services = yaml.safe_load(text)["services"]
    for name in GATEWAY_SERVICES:
        assert services[name]["image"] == IMAGE and "build" not in services[name], name
        assert services[name]["pull_policy"] == "never", name  # das Image kommt aus dem Archiv, nie aus einer Registry
    for name in ("mosquitto", "zigbee2mqtt"):
        assert "pull_policy" not in services[name], name  # Images aus Registries werden gezogen
    assert services["mosquitto"]["image"].startswith("eclipse-mosquitto:2@sha256:")
    assert services["zigbee2mqtt"]["image"].startswith("koenkk/zigbee2mqtt:2.14.2@sha256:")
    archiv = bundles.Archiv(bundles.ARCHIVE_NAME, "e" * 64, 1000, "smartheat-gateway", "sha256:" + "a" * 64)
    bundles.check_compose(text, archiv)  # das Geraet akzeptiert genau diese Datei


def test_render_keeps_variables_and_string_values():
    text = render_compose(COMPOSE.read_text(), IMAGE)
    assert "${SHG_ROOT:-/var/lib/smartheat}" in text
    assert "${SHG_DEVICE_API_URL:?SHG_DEVICE_API_URL fehlt}" in text
    services = yaml.safe_load(text)["services"]
    assert services["init"]["restart"] == "no"  # String, kein YAML-1.1-Bool
    assert isinstance(services["init"]["restart"], str)
    assert services["init"]["network_mode"] == "none"
    assert services["runtime"]["ports"] == ["0.0.0.0:80:8080", "127.0.0.1:8081:8081"]
    assert services["mosquitto"]["user"] == "1000:1000"
    assert set(GATEWAY_SERVICES) == {"init", "runtime"}


def test_render_has_no_yaml_anchors_or_extension_keys():
    text = render_compose(COMPOSE.read_text(), IMAGE)
    assert not any(marker in text for marker in ("&id", "*id", "x-hardening", "x-gateway", "x-after-init"))
    assert [key for key in yaml.safe_load(text) if key.startswith("x-")] == []


def test_render_keeps_hardening_and_dependencies_of_the_source():
    source = yaml.safe_load(COMPOSE.read_text())["services"]
    rendered = yaml.safe_load(render_compose(COMPOSE.read_text(), IMAGE))["services"]
    for name, service in source.items():
        for key, value in service.items():
            if key not in ("build", "image", "pull_policy"):
                assert rendered[name][key] == value, (name, key)


def test_build_schreibt_archiv_und_manifest(tmp_path, archive):
    out = tmp_path / "out"
    manifest = build(COMPOSE, MOSQUITTO_CONF, archive, "0.2.0", out)
    raw = (out / "manifest.json").read_bytes()
    parsed = bundles.parse_manifest(raw)  # das Geraet nimmt genau dieses Manifest an
    assert parsed.version == "0.2.0" and parsed.min_updater_version == MIN_UPDATER_VERSION == "0.8.0"
    assert sorted(p.name for p in out.iterdir()) == ["docker-compose.yml", "gateway-image.oci.tar", "manifest.json",
                                                     "mosquitto.conf"]
    ablage = out / bundles.ARCHIVE_NAME
    assert parsed.archiv == bundles.Archiv(
        bundles.ARCHIVE_NAME, hashlib.sha256(ablage.read_bytes()).hexdigest(), ablage.stat().st_size,
        "smartheat-gateway", archive.digest)
    assert manifest["archiv"] == {"datei": "gateway-image.oci.tar", "sha256": parsed.archiv.sha256,
                                  "bytes": ablage.stat().st_size, "name": "smartheat-gateway", "digest": archive.digest}
    ref = "smartheat-gateway@" + archive.digest
    assert manifest["images"]["gateway"] == ref
    assert set(manifest["images"]) == {"gateway", "eclipse-mosquitto", "zigbee2mqtt"}
    assert parsed.images == manifest["images"]
    text = (out / "docker-compose.yml").read_text()
    assert yaml.safe_load(text)["services"]["runtime"]["image"] == ref
    bundles.check_compose(text, parsed.archiv)


def test_min_updater_version_ist_0_8_0_ohne_und_0_9_0_mit_system():
    """Der Updater 0.8.0 des Labor-Pi 4 nimmt Container-Bundles ohne Systemteil weiter an; mit `system` verlangt das Bundle
    den Updater, der den Systemwechsel kennt."""
    assert min_updater_version(False) == "0.8.0" and min_updater_version(True) == "0.9.0"


@pytest.fixture
def system_datei(tmp_path):
    pfad = tmp_path / "gateway.raucb"
    pfad.write_bytes(b"signiertes-rauc-bundle" * 100)
    return pfad


def test_build_mit_system_bindet_die_datei_im_manifest(tmp_path, archive, system_datei):
    out = tmp_path / "out"
    manifest = build(COMPOSE, MOSQUITTO_CONF, archive, "0.2.0", out, system_datei)
    parsed = bundles.parse_manifest((out / "manifest.json").read_bytes())  # das Geraet nimmt genau dieses Manifest an
    assert parsed.min_updater_version == "0.9.0"
    assert sorted(p.name for p in out.iterdir()) == ["docker-compose.yml", "gateway-image.oci.tar", "manifest.json",
                                                     "mosquitto.conf", "system.raucb"]
    assert (out / "system.raucb").read_bytes() == system_datei.read_bytes()
    assert manifest["system"] == {"datei": "system.raucb", "sha256": hashlib.sha256(system_datei.read_bytes()).hexdigest(),
                                  "groesse": system_datei.stat().st_size, "compatible": "smartheat-gateway-pi5"}
    assert parsed.system == bundles.SystemBundle(
        "system.raucb", manifest["system"]["sha256"], system_datei.stat().st_size, bundles.SYSTEM_COMPATIBLE)
    assert bundles.SYSTEM_COMPATIBLE == "smartheat-gateway-pi5"


def test_build_ohne_system_hat_kein_system_feld_und_0_8_0(tmp_path, archive):
    manifest = build(COMPOSE, MOSQUITTO_CONF, archive, "0.2.0", tmp_path / "out")
    assert "system" not in manifest and manifest["min_updater_version"] == "0.8.0"
    assert not (tmp_path / "out" / "system.raucb").exists()


def test_build_mit_system_ist_reproduzierbar(tmp_path, archive, system_datei):
    build(COMPOSE, MOSQUITTO_CONF, archive, "0.2.0", tmp_path / "a", system_datei)
    build(COMPOSE, MOSQUITTO_CONF, archive, "0.2.0", tmp_path / "b", system_datei)
    for name in ("manifest.json", "system.raucb"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()


def test_build_mit_fehlendem_system_hinterlaesst_nichts(tmp_path, archive):
    with pytest.raises(OSError):
        build(COMPOSE, MOSQUITTO_CONF, archive, "0.2.0", tmp_path / "out", tmp_path / "gibt-es-nicht.raucb")
    assert not (tmp_path / "out").exists()


def test_build_mit_zu_grossem_system_wird_abgelehnt(tmp_path, archive, system_datei, monkeypatch):
    monkeypatch.setattr(bundles, "MAX_SYSTEM_BYTES", system_datei.stat().st_size - 1)
    with pytest.raises(bundles.ManifestError, match="system.groesse"):
        build(COMPOSE, MOSQUITTO_CONF, archive, "0.2.0", tmp_path / "out", system_datei)
    assert not (tmp_path / "out").exists()


def test_cli_mit_system(tmp_path, archive, system_datei):
    code = main(["--compose", str(COMPOSE), "--mosquitto-conf", str(MOSQUITTO_CONF), "--archive", str(archive),
                 "--system", str(system_datei), "--version", "1.2.3", "--out", str(tmp_path / "out")])
    assert code == 0 and (tmp_path / "out" / "system.raucb").is_file()
    assert bundles.parse_manifest((tmp_path / "out" / "manifest.json").read_bytes()).system is not None


def test_das_archiv_im_bundle_traegt_den_namen_unter_dem_der_docker_es_findet(tmp_path, archive):
    """`docker load` benennt das Image nach der Annotation io.containerd.image.name der index.json; nur unter dem
    normalisierten Namen `docker.io/library/smartheat-gateway@<Index-Digest>` findet der Docker des Geraets (containerd-
    Speicher) `smartheat-gateway@<Digest>` und listet es mit Repository und Digest (Probe 2026-10-26). Alles andere
    im Archiv, vor allem die Blobs und damit der Index-Digest, bleibt unveraendert."""
    out = tmp_path / "out"
    build(COMPOSE, MOSQUITTO_CONF, archive, "0.2.0", out)
    neu = out / bundles.ARCHIVE_NAME
    eintraege = _index(neu)["manifests"]
    assert len(eintraege) == 1 and eintraege[0]["digest"] == archive.digest
    assert eintraege[0]["annotations"][ANNOTATION] == "docker.io/library/smartheat-gateway@" + archive.digest
    assert eintraege[0]["annotations"]["org.opencontainers.image.created"] == "2026-10-26T12:00:00Z"  # Rest bleibt
    assert _blobs(neu) == _blobs(archive)
    assert ANNOTATION not in json.dumps(_index(archive))  # das Original bleibt unberuehrt
    assert oci_digest(neu) == oci_digest(archive) == archive.digest


def test_ein_archiv_mit_dem_richtigen_namen_wird_unveraendert_uebernommen(tmp_path, archive):
    """Der Neubau aus dem Archiv des Bundles (verify_bundle.py) ergibt dieselben Bytes."""
    erst = tmp_path / "erst"
    build(COMPOSE, MOSQUITTO_CONF, archive, "0.2.0", erst)
    zweit = tmp_path / "zweit"
    build(COMPOSE, MOSQUITTO_CONF, erst / bundles.ARCHIVE_NAME, "0.2.0", zweit)
    for name in ("docker-compose.yml", "gateway-image.oci.tar", "manifest.json", "mosquitto.conf"):
        assert (erst / name).read_bytes() == (zweit / name).read_bytes(), name


def test_ein_archiv_mit_fremdem_namen_wird_abgelehnt(tmp_path):
    fremd = tmp_path / "fremd.tar"
    make_oci_archive(fremd, annotations={ANNOTATION: "docker.io/library/anderes@sha256:" + "a" * 64})
    with pytest.raises(SystemExit, match="Name"):
        build(COMPOSE, MOSQUITTO_CONF, fremd, "0.2.0", tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_manifest_hashes_match_the_written_files(tmp_path, archive):
    build(COMPOSE, MOSQUITTO_CONF, archive, "0.2.0", tmp_path / "out")
    out = tmp_path / "out"
    parsed = bundles.parse_manifest((out / "manifest.json").read_bytes())
    for name in bundles.MANIFEST_FILES:
        assert parsed.files[name] == hashlib.sha256((out / name).read_bytes()).hexdigest(), name
    assert (out / "mosquitto.conf").read_bytes() == MOSQUITTO_CONF.read_bytes()
    bundles.check_compose((out / "docker-compose.yml").read_text(), parsed.archiv)


def test_build_is_reproducible(tmp_path, archive):
    build(COMPOSE, MOSQUITTO_CONF, archive, "0.2.0", tmp_path / "a")
    build(COMPOSE, MOSQUITTO_CONF, archive, "0.2.0", tmp_path / "b")
    for name in ("docker-compose.yml", "gateway-image.oci.tar", "manifest.json", "mosquitto.conf"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes(), name


@pytest.mark.parametrize(("fehler", "text"), [
    ({"blob_fehlt": True}, "Blob"),
    ({"blob_verfaelscht": True}, "Blob"),
    ({"eintraege": 2}, "genau einen"),
    ({"eintraege": 0}, "genau einen"),
    ({"index_json": b"kaputt"}, "index.json"),
    ({"index_json": b'{"manifests": [{"digest": "sha256:abc"}]}'}, "Digest"),
    ({"index_json": b'{"manifests": [{"digest": 5}]}'}, "Digest"),
    ({"index_json": b'{"manifests": "x"}'}, "genau einen"),
    ({"index_json": b"[]"}, "index.json"),
])
def test_ein_archiv_ohne_stimmigen_index_wird_abgelehnt(tmp_path, fehler, text):
    kaputt = tmp_path / "kaputt.tar"
    make_oci_archive(kaputt, **fehler)
    with pytest.raises(SystemExit, match=text):
        build(COMPOSE, MOSQUITTO_CONF, kaputt, "0.2.0", tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_eine_datei_die_kein_tar_ist_wird_abgelehnt(tmp_path):
    keins = tmp_path / "keins.tar"
    keins.write_bytes(b"kein tar")
    with pytest.raises(SystemExit, match="Archiv"):
        build(COMPOSE, MOSQUITTO_CONF, keins, "0.2.0", tmp_path / "out")
    with pytest.raises(SystemExit, match="Archiv"):
        build(COMPOSE, MOSQUITTO_CONF, tmp_path / "fehlt.tar", "0.2.0", tmp_path / "out")


def test_ein_archiv_ueber_der_grenze_hinterlaesst_nichts(tmp_path, archive, monkeypatch):
    monkeypatch.setattr(bundles, "MAX_ARCHIVE_BYTES", 1000)  # das Test-Archiv ist rund 10 KB gross
    with pytest.raises(bundles.ManifestError):
        build(COMPOSE, MOSQUITTO_CONF, archive, "0.2.0", tmp_path / "out")
    assert not (tmp_path / "out").exists()  # weder das halbe Archiv noch ein Manifest
    vorhanden = tmp_path / "vorhanden"
    vorhanden.mkdir()
    with pytest.raises(bundles.ManifestError):
        build(COMPOSE, MOSQUITTO_CONF, archive, "0.2.0", vorhanden)
    assert list(vorhanden.iterdir()) == []  # ein schon vorhandener Ordner bleibt, leer


def test_invalid_version_is_refused(tmp_path, archive):
    with pytest.raises(bundles.ManifestError):
        build(COMPOSE, MOSQUITTO_CONF, archive, "v0.2", tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_cli_writes_the_bundle(tmp_path, archive, capsys):
    code = main(["--compose", str(COMPOSE), "--mosquitto-conf", str(MOSQUITTO_CONF), "--archive", str(archive),
                 "--version", "1.2.3", "--out", str(tmp_path / "out")])
    assert code == 0
    assert set(json.loads(capsys.readouterr().out)) == set(bundles.MANIFEST_FILES)
    assert bundles.parse_manifest((tmp_path / "out" / "manifest.json").read_bytes()).version == "1.2.3"
    assert (tmp_path / "out" / bundles.ARCHIVE_NAME).is_file()


# Ohne PYTHONPATH, aber mit PYTHONUSERBASE, falls gesetzt: dort liegen in der VS-Code-Sandbox (flatpak) die
# Pakete der Entwicklungsumgebung (PyYAML); CI und Host setzen es nicht.
_ENV_OHNE_PYTHONPATH = {"PATH": "", **{k: v for k, v in os.environ.items() if k == "PYTHONUSERBASE"}}

def test_script_runs_without_pythonpath(tmp_path, archive):
    script = Path(__file__).resolve().parents[1] / "build_bundle.py"
    result = subprocess.run(
        [sys.executable, str(script), "--compose", str(COMPOSE), "--mosquitto-conf", str(MOSQUITTO_CONF),
         "--archive", str(archive), "--version", "0.2.0", "--out", str(tmp_path / "out")],
        capture_output=True, text=True, env=_ENV_OHNE_PYTHONPATH, check=False)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "out" / "manifest.json").is_file()


def test_device_parser_of_the_updater_finds_the_stick_in_the_bundle(tmp_path, archive):
    """Der Updater liest devices: ohne yaml (bundles.compose_devices); er muss den Stick im erzeugten Bundle finden."""
    build(COMPOSE, MOSQUITTO_CONF, archive, "0.2.0", tmp_path / "out")
    text = (tmp_path / "out" / "docker-compose.yml").read_text()
    expected = [entry.split(":", 1)[0] for service in yaml.safe_load(text)["services"].values()
                for entry in service.get("devices", [])]
    assert expected == ["/dev/zigbee"]
    assert bundles.compose_devices(text) == expected
    assert bundles.compose_devices(COMPOSE.read_text()) == expected  # Quelle (Flussliste) ebenso
