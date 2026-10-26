import ast
import base64
import dataclasses
import errno
import hashlib
import http.server
import importlib
import json
import os
import random
import socket
import ssl
import stat
import subprocess
import threading
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import create_autospec

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature
from kette_helper import release, roots_pem, signiere
from kette_helper import root as neue_root

from smartheat_gateway.version import GATEWAY_VERSION
from smartheat_host import bundles, hoststatus, kanal, kette, updater

ARCHIV_INHALT = b"oci-archiv-der-tests" * 50  # Inhalt egal: der Updater prueft Groesse und SHA-256, docker load ist eine Attrappe
ARCHIV_DIGEST = "sha256:" + "d" * 64
IMAGE = "smartheat-gateway@" + ARCHIV_DIGEST  # das Image des Bundles 0.3.0, kommt aus dem Archiv
ALTES_IMAGE = "smartheat-gateway@sha256:" + "a" * 64  # das Image des laufenden Bundles 0.2.0, schon geladen
ARCHIV = {"datei": bundles.ARCHIVE_NAME, "sha256": hashlib.sha256(ARCHIV_INHALT).hexdigest(),
          "bytes": len(ARCHIV_INHALT), "name": "smartheat-gateway", "digest": ARCHIV_DIGEST}
COMPOSE = f"services:\n  runtime:\n    image: {IMAGE}\n    pull_policy: never\n".encode()
ALTE_COMPOSE = f"services:\n  runtime:\n    image: {ALTES_IMAGE}\n    pull_policy: never\n".encode()
MOSQ = b"listener 1883\n"
BASE = "https://release.example.test/0.3.0/"
JETZT = datetime(2026, 6, 1, 12, tzinfo=UTC)  # die synchrone Uhr der Tests
ZERTIFIKAT_VON = datetime(2026, 3, 1, tzinfo=UTC)
ZERTIFIKAT_BIS = datetime(2027, 3, 1, tzinfo=UTC)


class FakeKanal:
    """offline=True: der Broker ist nicht erreichbar (Netz weg, z. B. Router nach Stromausfall). Meldungen gehen
    lokal immer durch, ausser broken=True (Datentraeger nicht schreibbar)."""

    def __init__(self, desired):
        self.desired_answer, self.results, self.offline, self.broken = desired, [], False, False
        self.desired_calls = self.reach_calls = 0

    def desired(self):
        self.desired_calls += 1
        if isinstance(self.desired_answer, Exception):
            raise self.desired_answer
        return self.desired_answer

    def update_result(self, version, result, reason):
        if self.broken:
            raise kanal.KanalFehler("nicht schreibbar")
        self.results.append((version, result, reason))

    def erreichbar(self):
        self.reach_calls += 1
        return not self.offline


class FakeCompose:
    """`images`: was der Docker des Geraets kennt (name@digest); `events` haelt die Reihenfolge von has_image, pull und up
    (die World haengt Abruf und Laden des Archivs an dieselbe Liste)."""

    def __init__(self, fail_up=(), fail_pull=False):
        self.calls, self.fail_up, self.fail_pull = [], set(fail_up), fail_pull
        self.on_up = None
        self.images, self.events = {ALTES_IMAGE}, []

    def has_image(self, ref):
        self.events.append(("has_image", ref, ref in self.images))
        return ref in self.images

    def pull(self, version):
        self.calls.append(("pull", version))
        self.events.append(("pull", version))
        if self.fail_pull:
            raise bundles.ComposeError("kein Netz")

    def up(self, version):
        self.calls.append(("up", version))
        self.events.append(("up", version))
        if self.on_up:
            self.on_up(version)
        if version in self.fail_up:
            raise bundles.ComposeError("boom")

    def prune(self, keep):
        self.calls.append(("prune", frozenset(keep)))


class World:
    def __init__(self, tmp_path, *, manifest_over=None, compose=COMPOSE, healthy=True, version="0.2.0",
                 url=BASE + "manifest.json", tamper=False, fail_up=(), fail_pull=False, health_seconds=None):
        self.root_key, self.root_cert = neue_root()
        self.roots = roots_pem(self.root_cert)
        self.release_key, self.release_cert = release(self.root_key, self.root_cert, von=ZERTIFIKAT_VON,
                                                      bis=ZERTIFIKAT_BIS)
        self.now = JETZT  # None: die Uhr ist nicht synchron
        files = {"docker-compose.yml": compose, "mosquitto.conf": MOSQ}
        manifest = {"version": "0.3.0", "min_updater_version": "0.2.0",
                    "files": {n: hashlib.sha256(d).hexdigest() for n, d in files.items()},
                    "images": {"gateway": IMAGE}, "archiv": dict(ARCHIV)}
        manifest.update(manifest_over or {})
        raw = json.dumps(manifest).encode()
        self.served = {BASE + "manifest.json": raw, **{BASE + n: d for n, d in files.items()},
                       BASE + bundles.ARCHIVE_NAME: ARCHIV_INHALT}
        if tamper:
            self.served[BASE + "docker-compose.yml"] = compose + b"#x\n"
        self.api = FakeKanal({"version": "0.3.0", "manifest_url": url,
                            "manifest_sha256": hashlib.sha256(raw).hexdigest(),
                            "signature": self.sign(raw)})
        self.compose = FakeCompose(fail_up, fail_pull)
        self.events = self.compose.events  # Abruf und Laden des Archivs stehen in derselben Reihenfolge
        self.archive_fetches, self.loaded = [], []
        self.fetch_breaks_after = None  # Byte, nach dem der Abruf des Archivs abbricht (Rest bleibt fuer den naechsten)
        self.load_error, self.load_adds_image = None, True
        self.store = bundles.BundleStore(tmp_path)
        self.store.install("0.2.0", {"docker-compose.yml": ALTE_COMPOSE, "mosquitto.conf": MOSQ})  # laufendes Bundle
        self.state_path = tmp_path / "updater" / "state.json"
        updater.State(current="0.2.0").save(self.state_path)
        self.healthy = healthy
        self.fetched = []
        self.clock = [0.0]
        self.extra = {} if health_seconds is None else {"health_seconds": health_seconds}
        self.context = {"updater": version, "roots": kette.roots_identity(self.roots)}
        self.updater = self.rebuild(version=version)

    def sign(self, data, *, key=None, cert=None):
        """Signaturtext (smartheat-kette-1) ueber `data`, Standard: mit dem Release-Zertifikat der World."""
        return signiere(key or self.release_key, cert or self.release_cert, data)

    def abgelehnt(self, version="0.3.0"):
        """Eintrag in `rejected` fuer das jetzige Angebot: "<version>@<sha256 (Fingerabdruck des Release-Zertifikats
        + manifest_sha256)>", unabhaengig vom Text und von den Signaturbytes."""
        angebot = self.api.desired_answer
        try:  # eine unlesbare Signatur traegt kein Zertifikat: leerer Fingerabdruck
            pem = json.loads(angebot["signature"])["zertifikat"].encode()
            abdruck = x509.load_pem_x509_certificate(pem).fingerprint(hashes.SHA256()).hex()
        except ValueError:
            abdruck = ""
        return f"{version}@{hashlib.sha256((abdruck + angebot['manifest_sha256']).encode()).hexdigest()}"

    def installiere_angebot(self, version="0.3.0"):
        """Legt das angebotene Bundle samt Signatur im Speicher ab (wie ein Update, das vor `up` stand)."""
        self.store.install(version, {"docker-compose.yml": COMPOSE, "mosquitto.conf": MOSQ,
                                     bundles.MANIFEST_NAME: self.served[BASE + "manifest.json"],
                                     bundles.SIGNATURE_NAME: self.api.desired_answer["signature"].encode()})

    def neues_zertifikat(self, *, von, bis):
        """Weiteres Release-Zertifikat derselben Root: (Schluessel, Zertifikat)."""
        return release(self.root_key, self.root_cert, von=von, bis=bis)

    def rebuild(self, *, version="0.2.0", roots_text=None):
        """Neuer Updater ueber derselben Zustandsdatei (Neustart, neue Host-Version, neuer Schluesselbund)."""
        return updater.Updater(
            kanal=self.api, store=self.store, compose=self.compose, fetch=self.fetch, fetch_datei=self.fetch_datei,
            docker_load=self.docker_load, roots_text=roots_text or self.roots, uhr=lambda: self.now, state_path=self.state_path, version=version,
            health=lambda version: self.healthy, clock=lambda: self.clock[0], sleep=self.sleep, **self.extra,
        )

    def fetch(self, url, max_bytes):
        """Wie `fetch_url`: eine Antwort ueber `max_bytes` ist ein FetchError; `fetched` haelt jede Anfrage fest."""
        self.fetched.append(url)
        if url not in self.served:
            raise updater.FetchError(url)
        if len(self.served[url]) > max_bytes:
            raise updater.FetchError(f"{url}: zu gross")
        return self.served[url]

    def fetch_datei(self, url, max_bytes, ziel, frist):
        """Wie `fetch_url_to_file`: schreibt nach `ziel` und gibt die SHA-256 zurueck; eine Antwort ueber `max_bytes` ist
        TooLarge. Bricht der Abruf ab (`fetch_breaks_after`), bleibt der Anfang als `ziel.part` liegen, ein weiterer Abruf
        setzt dahinter fort (wie das Original)."""
        self.events.append(("fetch_datei", url))
        self.archive_fetches.append((url, max_bytes, ziel, frist))
        if url not in self.served:
            raise updater.FetchError(url)
        data = self.served[url]
        if len(data) > max_bytes:
            raise updater.TooLarge(f"{url}: zu gross")
        part = ziel.with_name(ziel.name + ".part")
        if self.fetch_breaks_after is not None:
            part.write_bytes(data[:self.fetch_breaks_after])
            self.fetch_breaks_after = None
            raise updater.FetchError(f"{url}: abgebrochen")
        resumed = part.read_bytes() if part.exists() else b""
        ziel.write_bytes(resumed + data[len(resumed):])
        part.unlink(missing_ok=True)
        return hashlib.sha256(ziel.read_bytes()).hexdigest()

    def docker_load(self, pfad):
        self.events.append(("docker_load", pfad.name))
        assert pfad.is_file()
        self.loaded.append((pfad.name, pfad.read_bytes()))
        if self.load_error:
            raise bundles.LoadError(f"{pfad.name}: {self.load_error}")
        if self.load_adds_image:
            self.compose.images.add(IMAGE)

    def sleep(self, seconds):
        self.clock[0] += seconds

    def state(self):
        return updater.State.load(self.state_path)


def test_healthy_update(tmp_path):
    world = World(tmp_path)
    assert world.updater.run_once() == "aktualisiert"
    state = world.state()
    assert (state.current, state.previous, state.in_progress) == ("0.3.0", "0.2.0", None)
    assert world.api.results == [("0.3.0", "ok", "")]
    assert ("pull", "0.3.0") in world.compose.calls and ("up", "0.3.0") in world.compose.calls
    assert world.store.exists("0.3.0")
    assert world.compose.calls[-1] == ("prune", frozenset({IMAGE, ALTES_IMAGE}))  # auch das Image der vorigen Version


def test_state_file_is_world_readable_and_survives_load(tmp_path):
    world = World(tmp_path)
    world.updater.run_once()
    assert stat.S_IMODE(world.state_path.stat().st_mode) == 0o644
    assert json.loads(world.state_path.read_text()) == {
        "current": "0.3.0", "previous": "0.2.0", "rejected": [], "in_progress": None,
        "rejected_context": {"updater": "0.2.0", "roots": kette.roots_identity(world.roots)}, "pending_reports": [],
        "zertifikat_untergrenze": ZERTIFIKAT_VON.isoformat()}


def test_in_progress_is_persisted_before_the_switch(tmp_path):
    """Stromausfall waehrend des Umschaltens: die Datei muss schon vor compose up den Auftrag tragen."""
    world = World(tmp_path)
    seen = []
    world.compose.on_up = lambda version: seen.append(world.state().in_progress) if version == "0.3.0" else None
    world.updater.run_once()
    assert seen == [{"version": "0.3.0", "previous": "0.2.0", "since": seen[0]["since"],
                     "zertifikat_ab": ZERTIFIKAT_VON.isoformat()}]
    assert isinstance(seen[0]["since"], float)


def test_unhealthy_update_rolls_back_within_ten_minutes(tmp_path):
    world = World(tmp_path, healthy=False)
    assert world.updater.run_once() == "zurueckgerollt"
    assert world.compose.calls[-1] == ("up", "0.2.0")
    assert world.clock[0] >= updater.HEALTH_SECONDS  # Standardfrist 10 min
    assert world.clock[0] < updater.HEALTH_SECONDS + updater.HEALTH_POLL_SECONDS
    state = world.state()
    assert (state.current, state.rejected, state.in_progress) == ("0.2.0", [world.abgelehnt()], None)
    assert world.api.results == [("0.3.0", "rollback", "ungesund")]
    assert world.updater.run_once() == "aktuell"  # abgelehnt bleibt abgelehnt


def test_health_deadline_is_configurable(tmp_path):
    world = World(tmp_path, healthy=False, health_seconds=30)
    assert world.updater.run_once() == "zurueckgerollt"
    assert 30 <= world.clock[0] < 30 + updater.HEALTH_POLL_SECONDS


def test_health_that_arrives_late_is_accepted(tmp_path):
    world = World(tmp_path, healthy=False)
    original_sleep = world.sleep

    def sleep(seconds):
        original_sleep(seconds)
        world.healthy = world.clock[0] >= 100

    world.updater._sleep = sleep
    assert world.updater.run_once() == "aktualisiert"


def test_start_failure_rolls_back(tmp_path):
    world = World(tmp_path, fail_up={"0.3.0"})
    assert world.updater.run_once() == "zurueckgerollt"
    assert world.api.results == [("0.3.0", "rollback", "start_fehlgeschlagen")]
    assert world.compose.calls[-1] == ("up", "0.2.0") and world.clock[0] == 0.0  # kein Warten auf Gesundheit
    assert world.state().current == "0.2.0"


@pytest.mark.parametrize(("kwargs", "reason"), [
    ({"tamper": True}, "manifest_ungueltig"),
    ({"manifest_over": {"version": "0.4.0"}}, "manifest_ungueltig"),
    ({"url": "http://release.example.test/0.3.0/manifest.json"}, "manifest_ungueltig"),
    ({"manifest_over": {"min_updater_version": "9.0.0"}}, "updater_zu_alt"),
    ({"compose": b"services:\n  runtime:\n    image: ghcr.io/x/gw:latest\n"}, "manifest_ungueltig"),
])
def test_refused_bundles_are_never_started(tmp_path, kwargs, reason):
    world = World(tmp_path, **kwargs)
    if "url" in kwargs:  # http-URL: der Fetcher wuerde liefern, der Updater darf trotzdem nicht laden
        world.served[kwargs["url"]] = world.served[BASE + "manifest.json"]
    assert world.updater.run_once() == "abgelehnt"
    assert not any(call[0] in ("up", "pull") for call in world.compose.calls)
    assert not world.store.exists("0.3.0")  # nichts davon landet im Bundle-Ordner
    assert world.api.results == [("0.3.0", "rollback", reason)]
    assert world.state().current == "0.2.0" and world.state().rejected == [world.abgelehnt()]


def test_manifest_with_wrong_sha_is_refused(tmp_path):
    world = World(tmp_path)
    world.api.desired_answer["manifest_sha256"] = "0" * 64
    assert world.updater.run_once() == "abgelehnt"
    assert world.api.results == [("0.3.0", "rollback", "manifest_ungueltig")]


def test_http_manifest_is_loaded_when_allowed(tmp_path):
    url = "http://release.example.test/0.3.0/manifest.json"
    world = World(tmp_path, url=url)
    world.served[url] = world.served[BASE + "manifest.json"]
    for name in (*bundles.MANIFEST_FILES, bundles.ARCHIVE_NAME):
        world.served["http://release.example.test/0.3.0/" + name] = world.served[BASE + name]
    world.updater._allow_http = True
    assert world.updater.run_once() == "aktualisiert"


def _serve_manifest(world, raw, *, signed=None):
    """Bietet `raw` als Manifest an: die SHA-256 im Angebot passt zu `raw`, die Signatur gilt `signed` (Standard `raw`)."""
    world.served[BASE + "manifest.json"] = raw
    world.api.desired_answer["manifest_sha256"] = hashlib.sha256(raw).hexdigest()
    world.api.desired_answer["signature"] = world.sign(raw if signed is None else signed)


def _manifest_with(world, **changes):
    manifest = json.loads(world.served[BASE + "manifest.json"])
    manifest.update(changes)
    return json.dumps(manifest).encode()


def _assert_nothing_installed(world, version="0.3.0"):
    assert not any(call[0] in ("pull", "up") for call in world.compose.calls)
    assert not world.store.exists(version) and not world.store.dir(version).exists()  # auch kein halbes Bundle
    assert not (world.store.root / "bundles" / f".tmp-{version}").exists()


EVIL_COMPOSE = b"services:\n  runtime:\n    image: ghcr.io/evil/gw@sha256:" + b"c" * 64 + b"\n"


@pytest.mark.parametrize("attack", ["images", "compose"])
def test_manifest_changed_after_signing_is_refused_even_with_a_matching_sha(tmp_path, attack):
    """F-gateway-host-05/-43: ein Angreifer mit dem Server (Angebot, Manifest, Dateien) ersetzt das Manifest nach dem
    Signieren und zieht die SHA-256 im Angebot nach; "compose" tauscht zusaetzlich die Compose-Datei samt SHA im Manifest
    aus (in sich stimmig, mit Digest). Allein die Signatur ueber die urspruenglichen Bytes haelt ihn auf."""
    world = World(tmp_path)
    original = world.served[BASE + "manifest.json"]
    if attack == "images":
        changed = _manifest_with(world, images={"gateway": "ghcr.io/evil/gw@sha256:" + "c" * 64})
    else:
        files = json.loads(original)["files"] | {"docker-compose.yml": hashlib.sha256(EVIL_COMPOSE).hexdigest()}
        changed = _manifest_with(world, files=files)
        world.served[BASE + "docker-compose.yml"] = EVIL_COMPOSE
    assert changed != original
    _serve_manifest(world, changed, signed=original)
    assert world.updater.run_once() == "abgelehnt"
    assert world.api.results == [("0.3.0", "rollback", "signatur_ungueltig")]
    assert "signatur_ungueltig" in updater.REASONS  # Grund aus der festen Liste
    assert world.fetched == [BASE + "manifest.json"]  # keine Bundle-Datei geladen
    _assert_nothing_installed(world)
    assert world.state().rejected == [world.abgelehnt()] and world.state().current == "0.2.0"
    # Gegenprobe: dieselben Bytes, mit dem Schluessel signiert, werden angenommen - allein die Signatur trennt
    (tmp_path / "zwei").mkdir()
    signed = World(tmp_path / "zwei")
    if attack == "compose":
        signed.served[BASE + "docker-compose.yml"] = EVIL_COMPOSE
    _serve_manifest(signed, changed)
    assert signed.updater.run_once() == "aktualisiert"


MANIFEST_LIMIT = 65536  # Byte (64 KiB)
FILE_LIMIT = 1048576  # Byte (1 MiB)


def _padded(raw: bytes, size: int) -> bytes:
    assert len(raw) <= size
    return raw + b" " * (size - len(raw))  # Leerraum am Ende bleibt gueltiges JSON


def test_manifest_of_exactly_64_kib_is_accepted(tmp_path):
    world = World(tmp_path)
    raw = _padded(world.served[BASE + "manifest.json"], MANIFEST_LIMIT)
    assert len(raw) == 65536
    _serve_manifest(world, raw)
    assert world.updater.run_once() == "aktualisiert"
    assert (world.store.dir("0.3.0") / "manifest.json").stat().st_size == 65536


def test_manifest_one_byte_over_64_kib_is_temporary_and_nothing_is_unpacked(tmp_path):
    """Uebergroesse ist ein Abruffehler (FetchError): "vorlaeufig", nicht abgelehnt, keine Meldung, keine weitere Datei."""
    world = World(tmp_path)
    raw = _padded(world.served[BASE + "manifest.json"], MANIFEST_LIMIT + 1)
    assert len(raw) == 65537
    _serve_manifest(world, raw)  # SHA und Signatur passen: allein die Groesse haelt es auf
    assert world.updater.run_once() == "vorlaeufig"
    assert world.fetched == [BASE + "manifest.json"]
    assert world.api.results == [] and world.state().rejected == []
    _assert_nothing_installed(world)


def _padded_file(base: bytes, size: int) -> bytes:
    assert len(base) + 2 <= size
    return base + b"#" + b"x" * (size - len(base) - 2) + b"\n"  # Kommentarzeile: Compose und Konfiguration bleiben gueltig


def _serve_file(world, name, data):
    files = json.loads(world.served[BASE + "manifest.json"])["files"] | {name: hashlib.sha256(data).hexdigest()}
    world.served[BASE + name] = data
    _serve_manifest(world, _manifest_with(world, files=files))


FILE_BASES = {"docker-compose.yml": COMPOSE, "mosquitto.conf": MOSQ}


@pytest.mark.parametrize("name", FILE_BASES)
def test_bundle_file_of_exactly_1_mib_is_accepted(tmp_path, name):
    world = World(tmp_path)
    data = _padded_file(FILE_BASES[name], FILE_LIMIT)
    assert len(data) == 1048576
    _serve_file(world, name, data)
    assert world.updater.run_once() == "aktualisiert"
    assert (world.store.dir("0.3.0") / name).stat().st_size == 1048576


@pytest.mark.parametrize(("name", "fetched"), [
    ("docker-compose.yml", ["manifest.json", "docker-compose.yml"]),
    ("mosquitto.conf", ["manifest.json", "docker-compose.yml", "mosquitto.conf"]),
])
def test_bundle_file_one_byte_over_1_mib_is_temporary_and_nothing_is_unpacked(tmp_path, name, fetched):
    world = World(tmp_path)
    data = _padded_file(FILE_BASES[name], FILE_LIMIT + 1)
    assert len(data) == 1048577
    _serve_file(world, name, data)  # SHA im Manifest und Signatur passen: allein die Groesse haelt es auf
    assert world.updater.run_once() == "vorlaeufig"
    assert world.fetched == [BASE + item for item in fetched]  # danach wird nichts mehr geladen
    assert world.api.results == [] and world.state().rejected == []
    assert world.state().current == "0.2.0" and world.state().in_progress is None
    _assert_nothing_installed(world)


def _tree(root: Path, skip=("bundles", "updater")) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*"))
            if path.is_file() and path.relative_to(root).parts[0] not in skip}


def _host_files(root: Path):
    """Dateien ausserhalb der Bundle-Ablage, die ein Angebot nie ersetzen darf: Host-Dienste, Umgebung, Daten."""
    for name, content in (("host/gateway.env", b"SECRET=1\n"), ("host/smartheat_host/updater.py", b"# host\n"),
                          ("data/software_soll.json", b"{}"), ("etc/passwd", b"root:x:0:0\n")):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(content)


@pytest.mark.parametrize("extra", [
    "../host/gateway.env", "host/gateway.env", "../../host/smartheat_host/updater.py", "smartheat_host/updater.py",
    "/etc/passwd", "https://evil.test/updater.py",
])
@pytest.mark.parametrize("replace", [False, True])
def test_an_offer_that_names_other_files_is_refused_and_touches_nothing_outside_the_bundle_store(
        tmp_path, extra, replace):
    """Ein Angebot kann Host-Dienste nicht ersetzen (das geht nur mit neuem Image oder
    Installer). Das signierte Manifest darf genau docker-compose.yml und mosquitto.conf nennen; jeder andere Name
    (Pfad aus dem Bundle heraus, absolut, URL), zusaetzlich oder an Stelle einer der beiden, wird abgelehnt, bevor eine
    Datei abgerufen wird. Dass ein gelungenes Update ausserhalb der Bundle-Ablage nichts schreibt, zeigt der Folgetest."""
    world = World(tmp_path)
    evil = b"# ersetzt den Host-Dienst\n"
    files = json.loads(world.served[BASE + "manifest.json"])["files"]
    if replace:
        del files["mosquitto.conf"]
    files[extra] = hashlib.sha256(evil).hexdigest()
    for url in (BASE + extra, "https://release.example.test/host/gateway.env", "https://release.example.test" + extra):
        world.served[url] = evil
    _serve_manifest(world, _manifest_with(world, files=files))
    assert world.updater.run_once() == "abgelehnt"
    assert world.api.results == [("0.3.0", "rollback", "manifest_ungueltig")]
    assert world.fetched == [BASE + "manifest.json"]
    _assert_nothing_installed(world)  # und von den angebotenen Dateien wurde keine einzige abgerufen (`fetched`)


def test_a_complete_update_changes_only_the_bundle_store_and_the_state(tmp_path):
    """Gegenprobe zum vorigen Test: auch ein gelungenes Update schreibt nur unter bundles/ und updater/."""
    world = World(tmp_path)
    _host_files(tmp_path)
    before = _tree(tmp_path)
    assert world.updater.run_once() == "aktualisiert"
    assert _tree(tmp_path) == before
    assert sorted(path.name for path in world.store.dir("0.3.0").iterdir()) == [
        "docker-compose.yml", "manifest.json", "manifest.json.sig", "mosquitto.conf"]


def test_the_running_version_is_current_without_download_report_or_state_change(tmp_path):
    """F-gateway-host-01/-06/-43: ein vollstaendiges Angebot fuer die laufende Version (0.2.0) ist "aktuell"."""
    world = World(tmp_path)
    offer = dict(world.api.desired_answer)
    world.api.desired_answer = offer | {"version": "0.2.0"}
    state_before = world.state_path.read_bytes()
    assert world.updater.run_once() == "aktuell"
    assert world.fetched == [] and world.compose.calls == [] and world.api.results == []
    assert world.state_path.read_bytes() == state_before and world.api.desired_calls == 1
    # Gegenprobe: dasselbe Angebot fuer eine neuere Version wird geladen
    world.api.desired_answer = offer
    assert world.updater.run_once() == "aktualisiert"
    assert world.fetched != []


@pytest.mark.parametrize("offer", [
    {"version": "0.3.0"},
    {"version": "0.3.0", "manifest_url": None, "manifest_sha256": "", "signature": None},
])
def test_a_newer_version_without_any_bundle_field_is_current_without_download(tmp_path, offer):
    world = World(tmp_path)
    world.api.desired_answer = offer
    state_before = world.state_path.read_bytes()
    assert world.updater.run_once() == "aktuell"
    assert world.fetched == [] and world.compose.calls == [] and world.api.results == []
    assert world.state_path.read_bytes() == state_before


@pytest.mark.parametrize(("field", "value", "reason"), [
    ("manifest_url", BASE + "manifest.json", "manifest_ungueltig"),
    ("manifest_sha256", "a" * 64, "manifest_ungueltig"),
    ("signature", "x", "manifest_ungueltig"),
])
def test_one_bundle_field_alone_makes_the_version_an_offer(tmp_path, field, value, reason):
    """Gegenprobe zum vorigen Test: schon ein einziges Bundle-Feld ist ein Angebot (und hier ein unvollstaendiges)."""
    world = World(tmp_path)
    world.api.desired_answer = {"version": "0.3.0", field: value}
    assert world.updater.run_once() == "abgelehnt"
    assert world.api.results == [("0.3.0", "rollback", reason)]
    assert world.fetched == [] and world.compose.calls == []


def _assert_signature_refused(world):
    """Das Angebot wird mit `signatur_ungueltig` abgelehnt, ohne Bundle-Datei zu laden und ohne etwas zu starten."""
    assert world.updater.run_once() == "abgelehnt"
    assert world.api.results == [("0.3.0", "rollback", "signatur_ungueltig")]
    assert world.fetched == [BASE + "manifest.json"]
    _assert_nothing_installed(world)
    assert world.state().zertifikat_untergrenze is None


def test_wrong_signature_is_refused(tmp_path):
    world = World(tmp_path)
    other_key, other_cert = world.neues_zertifikat(von=ZERTIFIKAT_VON, bis=ZERTIFIKAT_BIS)
    world.api.desired_answer["signature"] = world.sign(world.served[BASE + "manifest.json"] + b" ", key=other_key,
                                                       cert=other_cert)  # Signatur ueber andere Bytes
    _assert_signature_refused(world)


def test_signature_of_another_key_than_the_certificates_is_refused(tmp_path):
    world = World(tmp_path)
    other_key, _ = world.neues_zertifikat(von=ZERTIFIKAT_VON, bis=ZERTIFIKAT_BIS)
    world.api.desired_answer["signature"] = world.sign(world.served[BASE + "manifest.json"], key=other_key)
    _assert_signature_refused(world)


def test_placeholder_release_roots_refuse_every_update(tmp_path):
    world = World(tmp_path)
    world.updater = world.rebuild(roots_text="-----SMARTHEAT-PLATZHALTER-----\n")
    _assert_signature_refused(world)


def test_abgelaufenes_zertifikat_wird_abgelehnt(tmp_path):
    world = World(tmp_path)
    world.now = ZERTIFIKAT_BIS + timedelta(seconds=1)
    _assert_signature_refused(world)
    (tmp_path / "zwei").mkdir()
    gueltig = World(tmp_path / "zwei")
    gueltig.now = ZERTIFIKAT_BIS
    assert gueltig.updater.run_once() == "aktualisiert"  # Gegenprobe: bis zum letzten Augenblick gueltig


def test_ein_noch_nicht_gueltiges_zertifikat_wird_abgelehnt(tmp_path):
    world = World(tmp_path)
    world.now = ZERTIFIKAT_VON - timedelta(seconds=1)
    _assert_signature_refused(world)


def test_bootstrap_anker_signiertes_bundle_wird_abgelehnt(tmp_path):
    """Ein Zertifikat, das ein anderer Anker als der Schluesselbund des Geraets ausgestellt hat (etwa der Bootstrap-Anker
    der Geraete-Identitaet), ist kein Release: nur die Roots in `release-roots.pem` zaehlen."""
    world = World(tmp_path)
    anker_key, anker_cert = neue_root(cn="Bootstrap-Anker")
    fremd_key, fremd_cert = release(anker_key, anker_cert, von=ZERTIFIKAT_VON, bis=ZERTIFIKAT_BIS)
    world.api.desired_answer["signature"] = world.sign(world.served[BASE + "manifest.json"], key=fremd_key,
                                                       cert=fremd_cert)
    _assert_signature_refused(world)


@pytest.mark.parametrize("error", [kanal.KanalFehler("x")])
def test_server_trouble_is_temporary(tmp_path, error):
    world = World(tmp_path)
    world.api.desired_answer = error
    assert world.updater.run_once() == "vorlaeufig"
    assert world.state().rejected == [] and world.api.results == []


def test_missing_desired_file_is_temporary(tmp_path):
    world = World(tmp_path)
    world.api.desired_answer = kanal.KanalFehler("Soll-Software noch nicht bekannt")
    assert world.updater.run_once() == "vorlaeufig" and world.compose.calls == []


def test_missing_download_is_temporary(tmp_path):
    world = World(tmp_path)
    del world.served[BASE + "mosquitto.conf"]
    assert world.updater.run_once() == "vorlaeufig"
    assert world.state().rejected == []
    assert not world.store.exists("0.3.0") and not (tmp_path / "bundles" / "0.3.0").exists()  # kein halbes Bundle


def test_failed_pull_is_temporary_and_leaves_no_in_progress(tmp_path):
    world = World(tmp_path, fail_pull=True)
    assert world.updater.run_once() == "vorlaeufig"
    state = world.state()
    assert (state.current, state.rejected, state.in_progress) == ("0.2.0", [], None)
    assert not any(call[0] == "up" for call in world.compose.calls) and world.api.results == []


def test_no_update_offered(tmp_path):
    world = World(tmp_path)
    world.api.desired_answer = {"version": "0.2.0"}
    assert world.updater.run_once() == "aktuell"
    world.api.desired_answer = {}
    assert world.updater.run_once() == "aktuell"


def test_rejected_list_is_capped(tmp_path):
    world = World(tmp_path)
    updater.State(current="0.2.0", rejected=[f"0.1.{i}" for i in range(updater.MAX_REJECTED)],
                  rejected_context=world.context).save(world.state_path)
    world.api.desired_answer["signature"] = "kaputt"
    assert world.updater.run_once() == "abgelehnt"
    rejected = world.state().rejected
    assert len(rejected) == updater.MAX_REJECTED and rejected[-1] == world.abgelehnt() and "0.1.0" not in rejected


def test_recover_finishes_or_rolls_back_an_interrupted_update(tmp_path):
    world = World(tmp_path)
    world.installiere_angebot()  # 0.2.0 legt World an
    updater.State(current="0.2.0", in_progress={"version": "0.3.0", "previous": "0.2.0", "since": 0.0}).save(
        world.state_path)
    world.healthy = False
    world.updater.recover()
    state = world.state()
    assert (state.current, state.in_progress, state.rejected) == ("0.2.0", None, [world.abgelehnt()])
    assert world.compose.calls[-1] == ("up", "0.2.0")
    assert world.api.results == [("0.3.0", "rollback", "ungesund")]


def _interrupted(world, previous="0.2.0", **extra):
    world.installiere_angebot()
    updater.State(current=previous, rejected_context=world.context,
                  in_progress={"version": "0.3.0", "previous": previous, "since": 0.0, **extra}).save(world.state_path)


def test_recover_restarts_the_new_bundle_before_trusting_health(tmp_path):
    """Stromausfall vor/waehrend up: die alten Container kommen zurueck und wirken gesund (healthy=True), das neue
    Bundle muss trotzdem hochgefahren werden, sonst liefe nie das, was als current gilt."""
    world = World(tmp_path)
    _interrupted(world)
    world.updater.recover()
    state = world.state()
    assert (state.current, state.previous, state.in_progress) == ("0.3.0", "0.2.0", None)
    assert world.api.results == [("0.3.0", "ok", "")]
    assert world.compose.calls[0] == ("up", "0.3.0")


def test_recover_rolls_back_when_the_restart_fails(tmp_path):
    world = World(tmp_path, fail_up={"0.3.0"})
    _interrupted(world)
    world.updater.recover()
    state = world.state()
    assert (state.current, state.in_progress, state.rejected) == ("0.2.0", None, [world.abgelehnt()])
    assert world.compose.calls == [("up", "0.3.0"), ("up", "0.2.0")]
    assert world.api.results == [("0.3.0", "rollback", "start_fehlgeschlagen")]


def test_failed_rollback_is_kept_and_retried_before_anything_else(tmp_path):
    world = World(tmp_path, healthy=False, fail_up={"0.3.0", "0.2.0"})
    erwartet = world.abgelehnt()
    assert world.updater.run_once() == "vorlaeufig"
    state = world.state()
    assert state.in_progress == {"version": "0.3.0", "previous": "0.2.0", "since": state.in_progress["since"],
                                 "rollback": "start_fehlgeschlagen"}
    assert (state.current, state.rejected) == ("0.2.0", []) and world.api.results == []  # noch nichts gemeldet
    # naechster Durchlauf: Rueckweg wird zuerst wiederholt, weiter scheitert er -> kein Abruf, kein Bericht
    world.api.desired_answer = RuntimeError("darf nicht abgefragt werden")
    assert world.updater.run_once() == "vorlaeufig"
    assert world.compose.calls[-1] == ("up", "0.2.0") and world.state().in_progress is not None
    # Rueckweg gelingt: abgelehnt, genau einmal gemeldet
    world.compose.fail_up.clear()
    world.api.desired_answer = {"version": "0.2.0"}
    assert world.updater.run_once() == "aktuell"
    state = world.state()
    assert (state.current, state.in_progress, state.rejected) == ("0.2.0", None, [erwartet])
    assert world.api.results == [("0.3.0", "rollback", "start_fehlgeschlagen")]


def test_pending_rollback_survives_a_restart(tmp_path):
    world = World(tmp_path)
    _interrupted(world, rollback="ungesund")
    world.rebuild().recover()
    state = world.state()
    assert (state.current, state.in_progress, state.rejected) == ("0.2.0", None, [world.abgelehnt()])
    assert world.compose.calls == [("up", "0.2.0")]  # kein erneuter Start des abgelehnten Bundles
    assert world.api.results == [("0.3.0", "rollback", "ungesund")]


def test_rollback_without_a_previous_bundle_is_logged_and_reported(tmp_path, caplog):
    world = World(tmp_path, healthy=False)
    _interrupted(world, previous=None)
    updater.State(current=None, rejected_context=world.context,
                  in_progress={"version": "0.3.0", "previous": None, "since": 0.0}).save(world.state_path)
    world.updater.recover()
    state = world.state()
    assert (state.current, state.in_progress, state.rejected) == (None, None, [world.abgelehnt()])
    assert world.compose.calls == [("up", "0.3.0")]  # kein Rueckweg-up
    assert world.api.results == [("0.3.0", "rollback", "ungesund")]
    assert "Kein Rueckweg moeglich" in caplog.text


def _reject_once(world):
    world.api.desired_answer["signature"] = "kaputt"
    assert world.updater.run_once() == "abgelehnt"
    assert world.state().rejected == [world.abgelehnt()]


def test_rejected_is_kept_for_the_same_updater_and_key(tmp_path):
    world = World(tmp_path)
    _reject_once(world)
    assert world.rebuild().run_once() == "aktuell"
    assert world.state().rejected == [world.abgelehnt()]


def test_rejected_is_cleared_after_an_updater_upgrade(tmp_path):
    world = World(tmp_path, manifest_over={"min_updater_version": "0.3.0"})
    assert world.updater.run_once() == "abgelehnt"
    assert world.api.results == [("0.3.0", "rollback", "updater_zu_alt")]
    assert world.rebuild().run_once() == "aktuell"
    assert world.rebuild(version="0.3.0").run_once() == "aktualisiert"
    assert world.state().rejected == []


def test_rejected_is_cleared_after_a_release_roots_change(tmp_path):
    world = World(tmp_path)
    placeholder = "-----SMARTHEAT-PLATZHALTER-----\n"
    placeholder_updater = world.rebuild(roots_text=placeholder)
    assert placeholder_updater.run_once() == "abgelehnt"
    assert world.state().rejected_context == {"updater": "0.2.0", "roots": "placeholder"}
    assert world.rebuild(roots_text=placeholder).run_once() == "aktuell"  # gleicher Bund: bleibt abgelehnt
    assert world.rebuild().run_once() == "aktualisiert"  # echter Bund: neuer Versuch
    assert world.state().rejected_context == world.context


def test_rejected_is_cleared_after_an_added_root(tmp_path):
    """Eine hinzugekommene Root aendert die Kennung des Schluesselbunds: die Ablehnung gilt nicht mehr."""
    world = World(tmp_path)
    _reject_once(world)
    _, zweite_root = neue_root(cn="Zweite Root")
    mehr = world.rebuild(roots_text=world.roots + roots_pem(zweite_root))
    assert mehr.run_once() == "abgelehnt"  # das unveraendert kaputte Angebot wird neu versucht und neu abgelehnt
    assert [reason for _, _, reason in world.api.results] == ["signatur_ungueltig", "signatur_ungueltig"]
    assert world.state().rejected_context["roots"] != world.context["roots"]


# --- Uhr, Untergrenze und Widerruf (SG-04, FR-25) ---


def _nichts_versucht(world):
    """Kein Abruf, keine Meldung, kein Start, nichts abgelehnt, die Untergrenze unberuehrt."""
    assert world.fetched == [] and world.compose.calls == [] and world.api.results == []
    state = world.state()
    assert state.rejected == [] and state.in_progress is None and state.pending_reports == []


def test_ohne_synchrone_uhr_wird_nichts_versucht_und_nichts_abgelehnt(tmp_path):
    world = World(tmp_path)
    world.now = None
    assert world.updater.run_once() == "wartet_auf_zeit"
    assert world.updater.run_once() == "wartet_auf_zeit"
    _nichts_versucht(world)
    assert world.state().zertifikat_untergrenze is None
    world.now = JETZT  # Gegenprobe: mit Uhr geht dasselbe Angebot durch
    assert world.updater.run_once() == "aktualisiert"


def test_die_laufende_version_braucht_keine_uhr(tmp_path):
    world = World(tmp_path)
    world.now = None
    world.api.desired_answer = {"version": "0.2.0"}
    assert world.updater.run_once() == "aktuell"


@pytest.fixture
def status_datei(tmp_path):
    return tmp_path / "host" / "status.json"


def test_zeit_aus_status_liefert_die_zeit_bei_synchroner_uhr_und_frischem_status(tmp_path, status_datei):
    _host_status(tmp_path, zeit_synchron=True)
    jetzt = updater.zeit_aus_status(status_datei)
    assert jetzt is not None and jetzt.tzinfo is not None and abs(datetime.now(UTC) - jetzt) < timedelta(seconds=5)
    assert updater.zeit_aus_status(tmp_path / "host" / "gibt-es-nicht.json") is None


@pytest.mark.parametrize(("alter", "gilt"), [
    (timedelta(minutes=14, seconds=59), True),
    (timedelta(minutes=15), True),
    (timedelta(minutes=15, seconds=1), False),
    (timedelta(hours=3), False),
])
def test_veralteter_status_gilt_als_nicht_synchron(tmp_path, status_datei, alter, gilt):
    """Der Hoststatus schreibt je Minute; ein Status ueber 15 min alt (Dienst steht, Datei von gestern) sagt nichts mehr
    ueber die Uhr."""
    _host_status(tmp_path, zeit_synchron=True, ts=JETZT)
    ergebnis = updater.zeit_aus_status(status_datei, jetzt=lambda: JETZT + alter)
    assert ergebnis == (JETZT + alter if gilt else None)


def test_unsynchroner_status_gibt_keine_zeit(tmp_path, status_datei):
    _host_status(tmp_path, zeit_synchron=False)
    assert updater.zeit_aus_status(status_datei) is None


def test_ein_status_mit_zeitstempel_aus_der_zukunft_gibt_keine_zeit(tmp_path, status_datei):
    """Die Uhr wurde seit dem Schreiben zurueckgestellt: der Status ist nicht mehr von dieser Uhr."""
    _host_status(tmp_path, zeit_synchron=True, ts=JETZT)
    assert updater.zeit_aus_status(status_datei, jetzt=lambda: JETZT - timedelta(minutes=5)) is None
    assert updater.zeit_aus_status(status_datei, jetzt=lambda: JETZT - timedelta(seconds=61)) is None
    assert updater.zeit_aus_status(status_datei, jetzt=lambda: JETZT - timedelta(seconds=60)) is not None  # NTP-Korrektur


@pytest.mark.parametrize("inhalt", [
    None,  # Datei fehlt
    "kein json",
    "[]",
    json.dumps({"zeit_synchron": True}),
    json.dumps({"ts": "2026-06-01T12:00:00+00:00"}),
    json.dumps({"zeit_synchron": "ja", "ts": "2026-06-01T12:00:00+00:00"}),
    json.dumps({"zeit_synchron": True, "ts": "gestern"}),
    json.dumps({"zeit_synchron": True, "ts": 12}),
    json.dumps({"zeit_synchron": True, "ts": "2026-06-01T12:00:00"}),  # ohne Zeitzone
])
def test_unbrauchbarer_status_gibt_keine_zeit(tmp_path, status_datei, inhalt):
    if inhalt is not None:
        status_datei.parent.mkdir(parents=True)
        status_datei.write_text(inhalt)
    assert updater.zeit_aus_status(status_datei, jetzt=lambda: datetime(2026, 6, 1, 12, 5, tzinfo=UTC)) is None


def test_zeit_aus_status_nimmt_die_zeit_des_aufrufs(tmp_path, status_datei):
    status_datei.parent.mkdir(parents=True)
    status_datei.write_text(json.dumps({"zeit_synchron": True, "ts": "2026-06-01T12:00:00+00:00"}))
    jetzt = datetime(2026, 6, 1, 12, 5, tzinfo=UTC)
    assert updater.zeit_aus_status(status_datei, jetzt=lambda: jetzt) == jetzt


def test_uhr_unter_der_untergrenze_wartet(tmp_path):
    world = World(tmp_path)
    updater.State(current="0.2.0", zertifikat_untergrenze=ZERTIFIKAT_VON.isoformat()).save(world.state_path)
    world.now = ZERTIFIKAT_VON - timedelta(days=1)  # die Uhr zeigt eine Zeit vor dem juengsten angenommenen Zertifikat
    assert world.updater.run_once() == "wartet_auf_zeit"
    _nichts_versucht(world)
    assert world.state().zertifikat_untergrenze == ZERTIFIKAT_VON.isoformat()
    world.now = ZERTIFIKAT_VON  # Gegenprobe: genau auf der Untergrenze geht es
    assert world.updater.run_once() == "aktualisiert"


def test_die_untergrenze_wird_erst_nach_dem_wechsel_gesetzt(tmp_path):
    world = World(tmp_path, healthy=False)
    seen = []
    world.compose.on_up = lambda version: seen.append(world.state().zertifikat_untergrenze)
    assert world.updater.run_once() == "zurueckgerollt"
    assert seen == [None, None]  # weder vor dem Start noch vor dem Rueckweg
    assert world.state().zertifikat_untergrenze is None  # ein gescheitertes Update hebt sie nicht
    (tmp_path / "zwei").mkdir()
    gut = World(tmp_path / "zwei")
    assert gut.updater.run_once() == "aktualisiert"
    assert gut.state().zertifikat_untergrenze == ZERTIFIKAT_VON.isoformat()


def test_neu_signierte_fassung_derselben_version_wird_wieder_versucht(tmp_path):
    world = World(tmp_path)
    world.now = ZERTIFIKAT_BIS + timedelta(days=1)  # das Zertifikat der ersten Fassung ist abgelaufen
    assert world.updater.run_once() == "abgelehnt"
    ersteres = world.abgelehnt()
    assert world.state().rejected == [ersteres]
    assert world.updater.run_once() == "aktuell"  # dieselbe Fassung bleibt abgelehnt, keine zweite Meldung
    assert len(world.api.results) == 1
    # eine neu signierte Fassung derselben Version (gleiche Bytes, neues Zertifikat) wird wieder versucht
    key, cert = world.neues_zertifikat(von=ZERTIFIKAT_BIS, bis=ZERTIFIKAT_BIS + timedelta(days=365))
    world.api.desired_answer["signature"] = world.sign(world.served[BASE + "manifest.json"], key=key, cert=cert)
    assert world.abgelehnt() != ersteres
    assert world.updater.run_once() == "aktualisiert"
    assert world.state().current == "0.3.0" and world.api.results[-1] == ("0.3.0", "ok", "")


P256_ORDNUNG = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


def _umformatiert(signatur_text: str) -> str:
    return json.dumps(json.loads(signatur_text), indent=2, sort_keys=True) + "\n"


def _s_zu_n_minus_s(signatur_text: str) -> str:
    """Dieselbe ECDSA-Signatur in der zweiten Form (r, n - s): gleiche Aussage, andere Bytes."""
    dokument = json.loads(signatur_text)
    r, s = decode_dss_signature(base64.b64decode(dokument["signatur"]))
    dokument["signatur"] = base64.b64encode(encode_dss_signature(r, P256_ORDNUNG - s)).decode()
    return json.dumps(dokument)


@pytest.mark.parametrize("abwandlung", [_umformatiert, _s_zu_n_minus_s])
def test_abgelehntes_angebot_bleibt_abgelehnt_wenn_nur_die_signaturbytes_anders_sind(tmp_path, abwandlung):
    """M-1: die Kennung haengt an Zertifikat und Manifest, nicht am Text der Signatur: anderer Leerraum im JSON oder
    s -> n - s ergibt keine neue Kennung, der Server kann die Ablehnung so nicht umgehen."""
    world = World(tmp_path)
    world.now = ZERTIFIKAT_BIS + timedelta(days=1)
    assert world.updater.run_once() == "abgelehnt"
    ersteres = world.abgelehnt()
    angebot = world.api.desired_answer
    angebot["signature"] = abwandlung(angebot["signature"])
    assert world.abgelehnt() == ersteres
    fetched = len(world.fetched)
    assert world.updater.run_once() == "aktuell"
    assert world.state().rejected == [ersteres] and len(world.api.results) == 1 and len(world.fetched) == fetched


def test_dasselbe_zertifikat_mit_anderem_manifest_ist_ein_neues_angebot(tmp_path):
    world = World(tmp_path)
    world.now = ZERTIFIKAT_BIS + timedelta(days=1)
    assert world.updater.run_once() == "abgelehnt"
    ersteres = world.abgelehnt()
    world.api.desired_answer["manifest_sha256"] = "e" * 64
    assert world.abgelehnt() != ersteres
    assert world.updater.run_once() == "abgelehnt"
    assert world.state().rejected == [ersteres, world.abgelehnt()]


def test_eine_neu_signierte_fassung_wird_auch_nach_einem_rueckweg_wieder_versucht(tmp_path):
    """Der Eintrag eines Rueckwegs traegt die Signatur des installierten Bundles, nicht die des spaeteren Angebots."""
    world = World(tmp_path, healthy=False)
    assert world.updater.run_once() == "zurueckgerollt"
    erstes = world.abgelehnt()
    assert world.state().rejected == [erstes]
    key, cert = world.neues_zertifikat(von=ZERTIFIKAT_VON, bis=ZERTIFIKAT_BIS)
    world.api.desired_answer["signature"] = world.sign(world.served[BASE + "manifest.json"], key=key, cert=cert)
    world.healthy = True
    assert world.updater.run_once() == "aktualisiert"


def test_neues_release_zertifikat_derselben_root_braucht_kein_geraeteupdate(tmp_path):
    """Der Schluesselbund im Image bleibt: ein spaeteres Release-Zertifikat derselben Root (neuer Schluessel, spaeterer
    Beginn) wird mit demselben `release-roots.pem` angenommen, auch von einem neu gestarteten Updater."""
    world = World(tmp_path)
    assert world.updater.run_once() == "aktualisiert"
    key, cert = world.neues_zertifikat(von=ZERTIFIKAT_VON + timedelta(days=100), bis=ZERTIFIKAT_BIS + timedelta(days=100))
    world.api.desired_answer = {**world.api.desired_answer, "version": "0.4.0"}
    manifest = json.loads(world.served[BASE + "manifest.json"])
    manifest["version"] = "0.4.0"
    raw = json.dumps(manifest).encode()
    world.served[BASE + "manifest.json"] = raw
    world.api.desired_answer.update(manifest_sha256=hashlib.sha256(raw).hexdigest(),
                                    signature=world.sign(raw, key=key, cert=cert))
    world.now = ZERTIFIKAT_VON + timedelta(days=101)
    assert world.rebuild(version="0.2.0").run_once() == "aktualisiert"
    assert world.state().current == "0.4.0"
    assert world.state().zertifikat_untergrenze == (ZERTIFIKAT_VON + timedelta(days=100)).isoformat()


def test_nach_annahme_eines_neueren_zertifikats_wird_das_aeltere_abgelehnt(tmp_path):
    """FR-25: ein gestohlener Schluessel (aelteres Zertifikat, noch gueltig) kann nach dem Wechsel auf ein neueres
    Zertifikat kein Bundle mehr unterschieben; Gegenprobe: das neuere Zertifikat selbst bleibt annehmbar."""
    world = World(tmp_path)
    neu_von = ZERTIFIKAT_VON + timedelta(days=30)
    neu_key, neu_cert = world.neues_zertifikat(von=neu_von, bis=ZERTIFIKAT_BIS)
    manifest_030 = world.served[BASE + "manifest.json"]
    world.api.desired_answer["signature"] = world.sign(manifest_030, key=neu_key, cert=neu_cert)
    world.now = neu_von + timedelta(days=1)
    assert world.updater.run_once() == "aktualisiert"
    assert world.state().zertifikat_untergrenze == neu_von.isoformat()
    # Angebot 0.4.0, signiert mit dem aelteren (gestohlenen) Zertifikat: abgelehnt, nichts geladen ausser dem Manifest
    manifest = json.loads(manifest_030)
    manifest["version"] = "0.4.0"
    raw = json.dumps(manifest).encode()
    world.served[BASE + "manifest.json"] = raw
    world.fetched.clear()
    world.compose.calls.clear()
    world.api.desired_answer.update(version="0.4.0", manifest_sha256=hashlib.sha256(raw).hexdigest(),
                                    signature=world.sign(raw))
    assert world.updater.run_once() == "abgelehnt"
    assert world.api.results[-1] == ("0.4.0", "rollback", "signatur_ungueltig")
    assert world.fetched == [BASE + "manifest.json"] and world.compose.calls == []
    assert world.state().current == "0.3.0"
    # Gegenprobe: dasselbe Angebot mit dem neueren Zertifikat geht durch
    world.api.desired_answer["signature"] = world.sign(raw, key=neu_key, cert=neu_cert)
    assert world.updater.run_once() == "aktualisiert"
    assert world.state().current == "0.4.0"


def _untergrenze_setzen(world, wert):
    state = world.state()
    state.zertifikat_untergrenze = wert
    state.save(world.state_path)


def _interrupted_gesund(world, *, ab, untergrenze=None):
    """Ein unterbrochenes Update auf 0.3.0 (Zertifikat ab `ab`), das beim Wiederaufnehmen gesund wird."""
    _interrupted(world, zertifikat_ab=ab.isoformat() if isinstance(ab, datetime) else ab)
    _untergrenze_setzen(world, untergrenze)
    world.updater.recover()
    assert world.state().current == "0.3.0" and world.state().in_progress is None


def test_untergrenze_steigt_nur(tmp_path):
    spaeter = ZERTIFIKAT_VON + timedelta(days=60)
    for name in "abcd":
        (tmp_path / name).mkdir()
    # ein Wechsel mit einem Zertifikat ab ZERTIFIKAT_VON senkt eine hoehere Untergrenze nie
    niedriger = World(tmp_path / "a")
    _interrupted_gesund(niedriger, ab=ZERTIFIKAT_VON, untergrenze=spaeter.isoformat())
    assert niedriger.state().zertifikat_untergrenze == spaeter.isoformat()
    # ein Zertifikat ab einem spaeteren Zeitpunkt hebt sie
    hoeher = World(tmp_path / "b")
    _interrupted_gesund(hoeher, ab=spaeter + timedelta(days=1), untergrenze=spaeter.isoformat())
    assert hoeher.state().zertifikat_untergrenze == (spaeter + timedelta(days=1)).isoformat()
    # ohne bisherige Untergrenze setzt der erste Wechsel sie
    erste = World(tmp_path / "c")
    _interrupted_gesund(erste, ab=ZERTIFIKAT_VON)
    assert erste.state().zertifikat_untergrenze == ZERTIFIKAT_VON.isoformat()
    # ein unbrauchbarer Wert im Auftrag aendert sie nicht
    kaputt = World(tmp_path / "d")
    _interrupted_gesund(kaputt, ab="kaputt", untergrenze=spaeter.isoformat())
    assert kaputt.state().zertifikat_untergrenze == spaeter.isoformat()


@pytest.mark.parametrize("wert", ["gestern", 5, "2026-03-01T00:00:00", ["x"]])
def test_eine_unbrauchbare_untergrenze_in_der_zustandsdatei_gilt_als_leer(tmp_path, wert):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"current": "0.2.0", "zertifikat_untergrenze": wert}))
    state = updater.State.load(path)
    assert state.current == "0.2.0" and state.zertifikat_untergrenze is None


def test_eine_untergrenze_mit_zeitzone_wird_geladen(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"current": "0.2.0", "zertifikat_untergrenze": "2026-03-01T00:00:00+00:00"}))
    assert updater.State.load(path).zertifikat_untergrenze == "2026-03-01T00:00:00+00:00"


def test_state_from_an_older_file_without_context_loads(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"current": "0.2.0", "previous": None, "rejected": ["0.3.0"], "in_progress": None}))
    state = updater.State.load(path)
    assert (state.rejected, state.rejected_context) == (["0.3.0"], None)


def test_recover_without_interrupted_update_does_nothing(tmp_path):
    world = World(tmp_path)
    world.updater.recover()
    assert world.compose.calls == [] and world.api.results == []


def test_corrupt_state_counts_as_empty(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{kaputt")
    assert updater.State.load(path) == updater.State()
    path.write_text("[1, 2]")
    assert updater.State.load(path) == updater.State()
    assert updater.State.load(tmp_path / "fehlt.json") == updater.State()


def test_state_without_a_usable_in_progress_entry_is_cleaned(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"current": "0.2.0", "in_progress": {"previous": "0.1.0"}}))
    assert updater.State.load(path).in_progress is None


# --- Umgebung statt Bundle: Server nicht erreichbar, Stick fehlt ---


def _offline_until(world, seconds, healthy_from=None):
    """Netz weg bis Uhrzeit `seconds` (monoton), gesund ab `healthy_from` (None: nie)."""
    original_sleep = world.sleep

    def sleep(step):
        original_sleep(step)
        world.api.offline = world.clock[0] < seconds
        world.healthy = healthy_from is not None and world.clock[0] >= healthy_from

    world.updater._sleep = sleep


def test_health_countdown_pauses_while_the_server_is_unreachable(tmp_path):
    """Stromausfall im ganzen Haus: der Router kommt nach dem Pi. Solange der Host die Geraete-API nicht erreicht, zaehlt
    die Frist nicht; wird das Bundle danach gesund, ist das Update gelungen."""
    world = World(tmp_path, healthy=False)
    _offline_until(world, 1500, healthy_from=1700)
    world.api.offline = False  # desired des Durchlaufs selbst geht noch durch
    assert world.updater.run_once() == "aktualisiert"
    assert world.clock[0] >= 1700 > updater.HEALTH_SECONDS
    state = world.state()
    assert (state.current, state.rejected, state.pending_reports) == ("0.3.0", [], [])
    assert world.api.results == [("0.3.0", "ok", "")]


def test_reachable_but_unhealthy_still_rolls_back_after_ten_minutes(tmp_path):
    world = World(tmp_path, healthy=False)
    _offline_until(world, 0)
    assert world.updater.run_once() == "zurueckgerollt"
    assert updater.HEALTH_SECONDS <= world.clock[0] < updater.HEALTH_SECONDS + updater.HEALTH_POLL_SECONDS
    assert world.state().rejected == [world.abgelehnt()]
    assert world.api.reach_calls > 1  # Erreichbarkeit wird je Takt geprueft
    assert world.api.results == [("0.3.0", "rollback", "ungesund")]  # abgelehnt, nicht server_unerreichbar


def test_unreachable_until_the_cap_rolls_back_without_rejecting_and_keeps_the_report(tmp_path):
    world = World(tmp_path, healthy=False)
    _offline_until(world, 10 ** 9)
    world.api.broken = True  # Datentraeger nicht schreibbar: die Meldung bleibt in pending_reports
    assert world.updater.run_once() == "zurueckgerollt"
    assert updater.HEALTH_CAP_SECONDS <= world.clock[0] < updater.HEALTH_CAP_SECONDS + updater.HEALTH_POLL_SECONDS
    assert world.compose.calls[-1] == ("up", "0.2.0")
    state = world.state()
    assert (state.current, state.rejected, state.in_progress) == ("0.2.0", [], None)  # nicht abgelehnt
    assert world.api.results == []  # nicht schreibbar: nichts gemeldet ...
    assert state.pending_reports == [{"version": "0.3.0", "result": "rollback", "reason": "server_unerreichbar"}]  # ... gemerkt
    # Netz wieder da: zuerst die offene Meldung, dann der neue Versuch derselben Version
    world.api.offline, world.api.broken, world.healthy = False, False, True
    world.updater._sleep = world.sleep
    assert world.rebuild().run_once() == "aktualisiert"
    assert world.api.results == [("0.3.0", "rollback", "server_unerreichbar"), ("0.3.0", "ok", "")]
    assert world.state().pending_reports == []


def test_cap_rollback_that_fails_keeps_the_no_reject_marker(tmp_path):
    world = World(tmp_path, healthy=False, fail_up={"0.2.0"})
    _offline_until(world, 10 ** 9)
    assert world.updater.run_once() == "vorlaeufig"
    assert world.state().in_progress["rollback"] == "server_unerreichbar"
    world.compose.fail_up.clear()
    world.updater.recover()
    state = world.state()
    assert (state.current, state.rejected, state.in_progress) == ("0.2.0", [], None)


def test_report_that_cannot_be_sent_is_kept_and_sent_first_next_run(tmp_path):
    world = World(tmp_path)
    world.api.desired_answer["signature"] = "kaputt"
    original = world.api.update_result

    def broken(*args):
        raise kanal.KanalFehler("update_result")

    world.api.update_result = broken
    assert world.updater.run_once() == "abgelehnt"
    assert world.state().pending_reports == [{"version": "0.3.0", "result": "rollback", "reason": "signatur_ungueltig"}]
    world.api.update_result = original
    world.api.desired_answer = {"version": "0.2.0"}
    assert world.rebuild().run_once() == "aktuell"
    assert world.api.results == [("0.3.0", "rollback", "signatur_ungueltig")]
    assert world.state().pending_reports == []


def test_pending_reports_go_out_before_recover_reports(tmp_path):
    world = World(tmp_path)
    _interrupted(world)
    state = world.state()
    state.pending_reports = [{"version": "0.2.9", "result": "rollback", "reason": "ungesund"}]
    state.save(world.state_path)
    world.updater.recover()
    assert world.api.results == [("0.2.9", "rollback", "ungesund"), ("0.3.0", "ok", "")]


def test_pending_reports_keep_their_order_while_offline(tmp_path):
    world = World(tmp_path)
    updater.State(current="0.2.0", rejected_context=world.context, pending_reports=[
        {"version": "0.2.8", "result": "rollback", "reason": "ungesund"},
        {"version": "0.2.9", "result": "ok", "reason": ""}]).save(world.state_path)
    world.api.desired_answer = {"version": "0.2.0"}
    world.api.broken = True
    assert world.updater.run_once() == "aktuell"
    assert len(world.state().pending_reports) == 2
    world.api.broken = False
    world.updater.run_once()
    assert world.api.results == [("0.2.8", "rollback", "ungesund"), ("0.2.9", "ok", "")]


def test_updater_verwirft_fremde_gruende_beim_laden(tmp_path, caplog):
    """Die Zustandsdatei ist lokale Eingabe: eine vorgemerkte Meldung mit fremdem Grund geht nie an den Server, das Laden
    wirft nicht (der Rueckweg darf an einer Meldung nicht scheitern)."""
    gueltig = {"version": "0.2.8", "result": "rollback", "reason": "ungesund"}
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"current": "0.2.0", "pending_reports": [
        gueltig, {"version": "0.2.9", "result": "rollback", "reason": "frei"},
        {"version": "0.3.0", "result": "ok", "reason": ""}]}))
    assert updater.State.load(path).pending_reports == [gueltig, {"version": "0.3.0", "result": "ok", "reason": ""}]
    assert "REASONS" in caplog.text and "frei" not in caplog.text  # ein Hinweis, nie der fremde Grund
    caplog.clear()
    path.write_text(json.dumps({"current": "0.2.0", "pending_reports": [gueltig]}))  # Gegenprobe: ohne den fremden
    assert updater.State.load(path).pending_reports == [gueltig] and caplog.records == []


def test_unusable_pending_reports_are_dropped_on_load(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"current": "0.2.0", "pending_reports": [
        {"version": "0.2.9", "result": "ok", "reason": ""}, {"version": 1}, "x", {"result": "ok"}]}))
    assert updater.State.load(path).pending_reports == [{"version": "0.2.9", "result": "ok", "reason": ""}]
    path.write_text(json.dumps({"current": "0.2.0", "pending_reports": "kaputt"}))
    assert updater.State.load(path).pending_reports == []


def _devices_compose(device: str) -> bytes:
    return (COMPOSE.decode() + f"  zigbee2mqtt:\n    image: koenkk/z2m@sha256:{'b' * 64}\n    devices:\n"
            f"    - {device}:/dev/zigbee\n").encode()


def test_missing_device_of_the_target_bundle_is_temporary(tmp_path):
    """Zigbee-Stick abgezogen: `up` wuerde scheitern und der Rueckweg ebenso - also gar nicht erst umschalten."""
    stick = tmp_path / "zigbee"
    world = World(tmp_path, compose=_devices_compose(str(stick)))
    assert world.updater.run_once() == "vorlaeufig"
    state = world.state()
    assert (state.current, state.rejected, state.in_progress) == ("0.2.0", [], None)
    assert not any(call[0] in ("pull", "up") for call in world.compose.calls) and world.api.results == []
    stick.touch()  # Stick wieder da
    assert world.updater.run_once() == "aktualisiert"


def test_recover_waits_for_a_missing_device_instead_of_rolling_back(tmp_path):
    stick = tmp_path / "zigbee"
    world = World(tmp_path)
    world.store.install("0.3.0", {"docker-compose.yml": _devices_compose(str(stick)), "mosquitto.conf": MOSQ})
    updater.State(current="0.2.0", rejected_context=world.context,
                  in_progress={"version": "0.3.0", "previous": "0.2.0", "since": 0.0}).save(world.state_path)
    world.updater.recover()
    assert world.compose.calls == [] and world.state().in_progress is not None
    assert world.updater.run_once() == "vorlaeufig"
    stick.touch()
    world.updater.recover()
    assert world.state().current == "0.3.0" and world.state().in_progress is None


def test_a_missing_device_in_a_multiline_flow_list_is_not_noticed(tmp_path):
    """Dokumentierte Grenze (bundles.compose_devices): der fehlende Stick in einer mehrzeiligen Flussliste haelt das
    Update nicht zurueck; dasselbe Bundle mit der einzeiligen Flussliste wartet (Gegenprobe)."""
    stick = tmp_path / "zigbee"  # fehlt
    service = f"  zigbee2mqtt:\n    image: koenkk/z2m@sha256:{'b' * 64}\n    devices"
    one_line = (COMPOSE.decode() + f"{service}: [\"{stick}:/dev/zigbee\"]\n").encode()
    multi_line = (COMPOSE.decode() + f"{service}: [\n      \"{stick}:/dev/zigbee\",\n    ]\n").encode()
    assert World(tmp_path, compose=one_line).updater.run_once() == "vorlaeufig"
    (tmp_path / "zwei").mkdir()
    world = World(tmp_path / "zwei", compose=multi_line)
    assert world.updater.run_once() == "aktualisiert"
    assert ("up", "0.3.0") in world.compose.calls


# --- Felder von desired ---


@pytest.mark.parametrize("answer", [{"version": 3}, {"version": None}, {"version": ["0.3.0"]}, {"manifest_url": "x"}])
def test_missing_or_non_text_version_counts_as_current(tmp_path, answer):
    world = World(tmp_path)
    world.api.desired_answer = answer
    assert world.updater.run_once() == "aktuell"
    assert world.api.results == [] and world.state().rejected == []


@pytest.mark.parametrize(("field", "value", "reason"), [
    ("manifest_url", 42, "manifest_ungueltig"),
    ("manifest_url", None, "manifest_ungueltig"),
    ("manifest_url", "https://[kaputt/manifest.json", "manifest_ungueltig"),
    ("manifest_url", "https:///manifest.json", "manifest_ungueltig"),
    ("manifest_sha256", 7, "manifest_ungueltig"),
    ("manifest_sha256", "abc", "manifest_ungueltig"),
    ("manifest_sha256", None, "manifest_ungueltig"),
    ("signature", {"x": 1}, "signatur_ungueltig"),
    ("signature", None, "signatur_ungueltig"),
])
def test_non_text_or_malformed_offer_fields_are_refused(tmp_path, field, value, reason):
    world = World(tmp_path)
    world.api.desired_answer[field] = value
    assert world.updater.run_once() == "abgelehnt"
    assert world.api.results == [("0.3.0", "rollback", reason)]
    assert not any(call[0] in ("pull", "up") for call in world.compose.calls)


def test_manifest_sha_is_compared_case_insensitively(tmp_path):
    world = World(tmp_path)
    world.api.desired_answer["manifest_sha256"] = world.api.desired_answer["manifest_sha256"].upper()
    assert world.updater.run_once() == "aktualisiert"


# --- Bundle-Ordner ---


def test_bundle_dir_keeps_manifest_and_signature(tmp_path):
    world = World(tmp_path)
    assert world.updater.run_once() == "aktualisiert"
    target = world.store.dir("0.3.0")
    assert (target / "manifest.json").read_bytes() == world.served[BASE + "manifest.json"]
    assert (target / "manifest.json.sig").read_text() == world.api.desired_answer["signature"]
    assert sorted(p.name for p in target.iterdir()) == [
        "docker-compose.yml", "manifest.json", "manifest.json.sig", "mosquitto.conf"]


HEALTHY = {"runtime": "healthy"}


@pytest.mark.parametrize(("data", "containers", "ok"), [
    ({"server_ok": True, "runtime_status": "regelt"}, HEALTHY, True),
    ({"server_ok": True, "runtime_status": "notbetrieb"}, HEALTHY, True),
    ({"server_ok": True, "runtime_status": None}, HEALTHY, True),       # nicht eingerichtet: Status abgeraeumt
    ({"server_ok": True, "runtime_status": "startet"}, HEALTHY, False),
    ({"server_ok": False, "runtime_status": "regelt"}, HEALTHY, False),
    ({"server_ok": None, "runtime_status": "regelt"}, HEALTHY, False),
    ({"runtime_status": "regelt"}, HEALTHY, False),
    ({"server_ok": True, "runtime_status": "regelt"}, {"runtime": "unhealthy"}, False),
    ({"server_ok": True, "runtime_status": "regelt"}, {"runtime": "starting"}, False),
    ({"server_ok": True, "runtime_status": "regelt"}, {"zigbee2mqtt": "healthy"}, False),  # Laufzeit fehlt/startet neu
    ({"server_ok": True, "runtime_status": "regelt"}, {}, False),
    (None, HEALTHY, False),
])
def test_health_rule(data, containers, ok):
    assert updater.health_ok(data, containers) is ok


def test_fetch_refuses_http_unless_allowed():
    with pytest.raises(updater.FetchError, match="https"):
        updater.fetch_url("http://example.test/x", 10, allow_http=False)
    with pytest.raises(updater.FetchError, match="https"):
        updater.fetch_url("file:///etc/passwd", 10, allow_http=True)


def _redirected_response(final_url: str, body: bytes = b"inhalt"):
    """Antwort des Openers, nachdem urllib einer Weiterleitung gefolgt ist: `geturl()` nennt die Adresse, von der der
    Inhalt tatsaechlich kam."""
    response = create_autospec(http.client.HTTPResponse, instance=True)
    response.__enter__.return_value = response
    response.geturl.return_value = final_url
    response.read1.side_effect = [body, b""]  # der Abruf liest mit read1 in Bloecken bis zum Ende
    return response


@pytest.mark.parametrize(("final_url", "allow_http"), [
    ("http://evil.test/x", False), ("file:///etc/passwd", False), ("ftp://evil.test/x", False),
    ("file:///etc/passwd", True), ("ftp://evil.test/x", True),  # auch der Schalter erlaubt nur http
])
def test_fetch_refuses_an_answer_from_a_scheme_that_is_not_allowed_without_reading_it(
        monkeypatch, final_url, allow_http):
    """Zweite Sicherung (der Redirect-Handler ist die erste): kommt die Antwort trotzdem von einer Adresse mit nicht
    erlaubtem Schema, wird sie verworfen, ungelesen. Attrappe an der Grenze, dem Opener von urllib (autospec): ein echter
    Server kann die Weiterleitung nicht an der ersten Sicherung vorbei erzwingen."""
    response = _redirected_response(final_url)
    opener = create_autospec(urllib.request.OpenerDirector, instance=True)
    opener.open.return_value = response
    monkeypatch.setattr(urllib.request, "build_opener", create_autospec(urllib.request.build_opener, return_value=opener))
    with pytest.raises(updater.FetchError, match="Weiterleitung auf ein Schema"):
        updater.fetch_url("https://release.example.test/x", 100, allow_http=allow_http)
    response.read1.assert_not_called()
    # Gegenproben: eine Antwort von https wird gelesen; http nur mit dem Schalter (Labor)
    opener.open.return_value = _redirected_response("https://cdn.example.test/x")
    assert updater.fetch_url("https://release.example.test/x", 100, allow_http=False) == b"inhalt"
    if final_url.startswith("http:"):
        opener.open.return_value = _redirected_response(final_url)
        assert updater.fetch_url("https://release.example.test/x", 100, allow_http=True) == b"inhalt"


@pytest.fixture
def redirecting_servers():
    """Zwei lokale HTTP-Server: der erste antwortet mit 302 auf den zweiten, der zweite haelt jede Anfrage fest."""
    seen = []

    class Target(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ziel")

        def log_message(self, *args):
            pass

    target = http.server.HTTPServer(("127.0.0.1", 0), Target)
    target_url = f"http://127.0.0.1:{target.server_port}/ziel"

    class Redirector(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", target_url)
            self.end_headers()

        def log_message(self, *args):
            pass

    redirector = http.server.HTTPServer(("127.0.0.1", 0), Redirector)
    servers = [target, redirector]
    threads = [threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
               for server in servers]
    for thread in threads:
        thread.start()
    yield f"http://127.0.0.1:{redirector.server_port}/start", seen
    for server in servers:
        server.shutdown()
        server.server_close()
    for thread in threads:
        thread.join()


def test_the_opener_refuses_a_redirect_to_a_scheme_that_is_not_allowed_before_asking(redirecting_servers):
    """Der erste Server ist http (der Test braucht dafuer einen Abruf ohne die Eingangspruefung von fetch_url): der Opener
    von `fetch_url` folgt der Weiterleitung auf die zweite http-Adresse nicht; der zweite Server sieht keine Anfrage."""
    start, seen = redirecting_servers
    with pytest.raises(updater.FetchError, match="Weiterleitung auf ein Schema"):
        updater.checked_opener(allow_http=False).open(start, timeout=5)
    assert seen == []
    # Gegenprobe: mit dem Schalter folgt der Opener, und der zweite Server sieht genau eine Anfrage
    with updater.checked_opener(allow_http=True).open(start, timeout=5) as response:
        assert response.read() == b"ziel"
    assert seen == ["/ziel"]


def test_a_refused_redirect_closes_the_answer_that_carried_it():
    """Wirft `redirect_request`, schliesst urllib die 302-Antwort nicht (das tut es erst nach einem Erfolg): der Handler
    schliesst sie selbst, sonst bleibt der Socket bis zur Muellsammlung offen. Attrappe an der Grenze, der Antwort `fp`."""
    class Antwort:
        closed = 0

        def close(self):
            self.closed += 1

    request = urllib.request.Request("https://release.example.test/x")
    handler = updater._SchemeCheckedRedirects(allow_http=False)
    refused = Antwort()
    with pytest.raises(updater.FetchError, match="Weiterleitung auf ein Schema"):
        handler.redirect_request(request, refused, 302, "Found", {}, "http://evil.test/x")
    assert refused.closed == 1
    # Gegenprobe: ein erlaubtes Ziel liefert die Folgeanfrage; die Antwort wird auch dann geschlossen (ungelesen, M-1)
    followed = Antwort()
    follow_up = handler.redirect_request(request, followed, 302, "Found", {}, "https://cdn.example.test/x")
    assert follow_up.full_url == "https://cdn.example.test/x" and followed.closed == 1


@pytest.fixture
def ftp_redirector():
    """Lokaler HTTP-Server, der mit 302 auf eine ftp-Adresse (Port 1: nichts hoert zu) weiterleitet."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "ftp://127.0.0.1:1/x")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/start"
    server.shutdown()
    server.server_close()
    thread.join()


def test_the_opener_refuses_a_redirect_to_ftp_even_with_the_http_switch(ftp_redirector):
    """urllib folgt auch einer Weiterleitung auf ftp (ein Verbindungsfehler waere ein URLError, kein FetchError des
    Handlers): mit dem Schalter ist nur http erlaubt, ftp wird abgelehnt, bevor eine Verbindung versucht wird."""
    with pytest.raises(updater.FetchError, match="Weiterleitung auf ein Schema"):
        updater.checked_opener(allow_http=True).open(ftp_redirector, timeout=5)


def test_fetch_url_follows_a_redirect_when_http_is_allowed(redirecting_servers):
    start, seen = redirecting_servers
    assert updater.fetch_url(start, 100, allow_http=True) == b"ziel"
    assert seen == ["/ziel"]


@pytest.fixture
def local_server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"server_ok": true, "runtime_status": null}' if self.path == "/healthz" else b"x" * 100
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join()


def test_fetch_url_limits_size_and_reads_when_allowed(local_server):
    assert updater.fetch_url(local_server + "/file", 100, allow_http=True) == b"x" * 100
    with pytest.raises(updater.FetchError, match="gross"):
        updater.fetch_url(local_server + "/file", 99, allow_http=True)


@pytest.fixture
def agent_server():
    """Leitet /a auf /b weiter und merkt sich den User-Agent jeder Anfrage."""
    seen: list[tuple[str, str | None]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("User-Agent")))
            if self.path == "/a":
                self.send_response(302)
                self.send_header("Location", "/b")
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", seen
    server.shutdown()
    server.server_close()
    thread.join()


def test_fetch_url_names_the_updater_as_user_agent_also_after_a_redirect(agent_server):
    """Labor 2026-10-20 (H2): die Browser-Integritaetspruefung von Cloudflare sperrt den Standard-User-Agent von urllib
    (Python-urllib, HTTP 403, Fehler 1010); der Updater nennt sich selbst, auch auf dem Ziel einer Weiterleitung."""
    base, seen = agent_server
    assert updater.fetch_url(base + "/a", 100, allow_http=True) == b"ok"
    assert seen == [("/a", f"SmartHeat-Gateway-Updater/{GATEWAY_VERSION}"),
                    ("/b", f"SmartHeat-Gateway-Updater/{GATEWAY_VERSION}")]


def test_fetch_url_network_errors_are_fetch_errors():
    with pytest.raises(updater.FetchError):
        updater.fetch_url("http://127.0.0.1:1/x", 10, allow_http=True)


@pytest.fixture
def trickling_server():
    """Lokaler HTTP-Server, der eine Antwort von 15 Bytes (Pfad /m) mit einem Byte je 0,2 s ausliefert (insgesamt 3 s): jede
    einzelne Leseoperation kommt weit vor der Zeitgrenze des Sockets zurueck, nur die Summe ist lang."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            count = 3 if self.path == "/kurz" else 15  # /kurz: kurze Gegenprobe (0,6 s)
            self.send_response(200)
            self.send_header("Content-Length", str(count))
            self.end_headers()
            for _ in range(count):
                try:
                    self.wfile.write(b"x")
                    self.wfile.flush()
                except OSError:  # der Abrufer hat aufgegeben
                    return
                time.sleep(0.2)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/m"
    server.shutdown()
    server.server_close()
    thread.join()


def test_fetch_ends_after_the_total_deadline_of_a_trickling_server(monkeypatch, trickling_server):
    """Der Socket-Timeout (30 s) gilt je Leseoperation; ein Server, der ein Byte je 0,2 s schickt, hielt den Abruf
    und damit den ganzen Updater beliebig lange fest. Die Gesamtfrist (hier 0,5 s statt 300 s) beendet ihn mit FetchError."""
    monkeypatch.setattr(updater, "ABRUF_GESAMT_SECONDS", 0.5)
    started = time.monotonic()
    with pytest.raises(updater.FetchError, match="Gesamtfrist"):
        updater.fetch_url(trickling_server, 100, allow_http=True)
    assert time.monotonic() - started < 2
    # Gegenprobe: eine kurze tropfende Antwort kommt mit der ueblichen Frist vollstaendig an
    monkeypatch.setattr(updater, "ABRUF_GESAMT_SECONDS", 300)
    assert updater.fetch_url(trickling_server.replace("/m", "/kurz"), 100, allow_http=True) == b"xxx"


def test_the_total_deadline_is_300_seconds_on_the_monotonic_clock_and_covers_every_read(monkeypatch):
    """Grenze der Frist ohne Warten: eine eingespeiste Uhr springt bei jedem Lesen um 100 s. Drei Lesevorgaenge
    (bis 299 s nach dem Start) genuegen, der vierte (400 s) nicht. Attrappe an der Grenze, dem Opener von urllib."""
    assert updater.ABRUF_GESAMT_SECONDS == 300
    now = [1000.0]
    chunks = iter([b"a", b"b", b"c", b"d", b"e"])

    def read1(size):
        now[0] += 100
        return next(chunks, b"")

    response = _redirected_response("https://release.example.test/x")
    response.read1.side_effect = read1
    opener = create_autospec(urllib.request.OpenerDirector, instance=True)
    opener.open.return_value = response
    monkeypatch.setattr(urllib.request, "build_opener", create_autospec(urllib.request.build_opener, return_value=opener))
    with pytest.raises(updater.FetchError, match="Gesamtfrist"):
        updater.fetch_url("https://release.example.test/x", 100, allow_http=False, clock=lambda: now[0])
    assert now[0] == 1300.0  # nach dem Lesen bei +300 s (Frist erreicht) ist Schluss, kein weiteres Lesen
    # Gegenprobe: bei +299 s ist die Frist noch nicht erreicht
    now[0], chunks = 1000.0, iter([b"abc"])
    response.read1.side_effect = lambda size: (now.__setitem__(0, now[0] + 299), next(chunks, b""))[1]
    assert updater.fetch_url("https://release.example.test/x", 100, allow_http=False, clock=lambda: now[0]) == b"abc"


@pytest.mark.parametrize(("address", "public"), [
    ("192.168.178.1", False), ("fd00::1", False), ("169.254.1.1", False), ("100.64.0.1", False),
    ("::ffff:127.0.0.1", False), ("127.0.0.1", False), ("10.0.0.1", False), ("172.18.0.1", False), ("::1", False),
    ("fe80::1%eth0", False), ("::ffff:192.168.178.1", False), ("nicht-eine-adresse", False), ("", False),
    ("8.8.8.8", True), ("2003::1", True), ("::ffff:8.8.8.8", True),
    # NAT64 (RFC 6052, 64:ff9b::/96): entscheidend ist die eingebettete IPv4-Adresse in den letzten 32 Bit
    ("64:ff9b::c0a8:b201", False), ("64:ff9b::7f00:1", False), ("64:ff9b::808:808", True),
    # betreiberlokales Praefix 64:ff9b:1::/48: immer abgelehnt, die Praefixlaenge waehlt der Betreiber; auch dort, wo die
    # Lesart als /48 (8.8.8.8 in Bits 48-63 und 72-87) oder als /96 (letzte 32 Bit) eine globale Adresse ergaebe
    ("64:ff9b:1:808:0:808:0:0", False), ("64:ff9b:1::808:808", False), ("64:ff9b:1::c0a8:b201", False),
    ("64:ff9b:1:ffff:ffff:ffff:ffff:ffff", False),
])
def test_only_global_addresses_are_allowed(address, public):
    """Gegenadressen, die ins LAN, auf den eigenen Rechner oder in Sonderbereiche zeigen, sind nicht erlaubt; eine
    IPv4-Adresse in IPv6-Schreibweise zaehlt wie die IPv4-Adresse (auch dann, wenn die Python-Version sie selbst anders
    einstuft); Unlesbares gilt als nicht erlaubt."""
    assert updater.is_public_address(address) is public


@pytest.fixture
def byte_counter():
    """TCP-Server auf 127.0.0.1 (kein TLS): liest hoechstens einen Block der Gegenseite und merkt sich die Zahl der Bytes
    (0, wenn sie auflegt, ohne etwas gesendet zu haben), schliesst dann."""
    received: list[int] = []
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()

    def serve():
        try:
            connection, _ = listener.accept()
        except OSError:
            return
        with connection:
            connection.settimeout(5)
            try:
                received.append(len(connection.recv(4096)))
            except OSError:
                received.append(-1)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    port = listener.getsockname()[1]
    yield port, received, thread
    if thread.is_alive():  # niemand hat sich verbunden: accept() mit einer Verbindung entlassen
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
    thread.join()
    listener.close()


def test_fetch_never_sends_a_byte_to_a_local_address(byte_counter):
    """Ein Angebot (Manifest-URL) aus einem kompromittierten Server darf keinen Abruf ins LAN des Kunden oder an
    den eigenen Rechner ausloesen. Geprueft wird die Gegenadresse des verbundenen Sockets, vor dem TLS-Handshake: der
    lokale Server sieht null Bytes (nicht einmal ein ClientHello), der Abruf ist ein FetchError."""
    port, received, thread = byte_counter
    with pytest.raises(updater.FetchError, match="nicht oeffentlich"):
        updater.fetch_url(f"https://127.0.0.1:{port}/m", 100, allow_http=False)
    thread.join(timeout=5)
    assert received == [0]


def test_with_the_test_switch_the_address_is_not_checked(byte_counter):
    """Gegenprobe: `allow_http` (nur Tests, Labor) schaltet die Pruefung ab; dann geht das ClientHello an den
    lokalen Server (der Abruf scheitert danach am fehlenden TLS, auch als FetchError, aber erst nach dem Senden)."""
    port, received, thread = byte_counter
    with pytest.raises(updater.FetchError) as error:
        updater.fetch_url(f"https://127.0.0.1:{port}/m", 100, allow_http=True)
    thread.join(timeout=5)
    assert received[0] > 0 and "nicht oeffentlich" not in str(error.value)


class _FakeSocket:
    def __init__(self, peer, own=("203.0.113.9", 40000)):
        self.peer, self.own, self.closed, self.sent = peer, own, False, b""

    def getpeername(self):
        return self.peer

    def getsockname(self):
        return self.own

    def sendall(self, data):
        self.sent += data

    def close(self):
        self.closed = True


@pytest.mark.parametrize(("peer", "allowed"), [
    (("192.168.178.1", 443), False), (("fd00::1", 443, 0, 0), False), (("::ffff:127.0.0.1", 443, 0, 0), False),
    (("169.254.1.1", 443), False), (("100.64.0.1", 443), False), ("/run/socket", False), (None, False),
    (("8.8.8.8", 443), True), (("2003::1", 443, 0, 0), True),
])
def test_every_connection_is_checked_on_the_connected_socket(monkeypatch, peer, allowed):
    """Die Pruefung sitzt an `_create_connection` der Verbindung und sieht den tatsaechlich verbundenen Socket
    (nicht den Namen aus der URL, der beim Aufloesen auf eine andere Adresse zeigen kann). Jede Verbindung des Openers hat
    sie, auch die zu einem Weiterleitungsziel (urllib baut je Anfrage eine neue). Attrappe an der Grenze:
    `socket.create_connection`. Kann der Peer nicht bestimmt werden (None, Unix-Socket), gilt er als nicht erlaubt."""
    fake = _FakeSocket(peer)
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: fake)
    connection = updater._FetchHTTPSConnection("release.example.test", 443, timeout=5,
                                                 deadline=updater._Deadline(time.monotonic), public_only=True)
    if allowed:
        assert connection._create_connection(("release.example.test", 443), 5, None) is fake and not fake.closed
    else:
        with pytest.raises(updater.FetchError, match="nicht oeffentlich"):
            connection._create_connection(("release.example.test", 443), 5, None)
        assert fake.closed and fake.sent == b""


@pytest.mark.parametrize(("peer", "own", "allowed"), [
    (("2003:1:2:3::5", 443, 0, 0), ("2003:1:2:3::5", 40000, 0, 0), False),  # der Rechner selbst
    (("2003:1:2:3::1", 443, 0, 0), ("2003:1:2:3:aaaa::9", 40000, 0, 0), False),  # gleiches /64 (Router, LAN-Geraet)
    (("2003:1:2:3:ffff:ffff:ffff:ffff", 443, 0, 0), ("2003:1:2:3::9", 40000, 0, 0), False),  # Rand des /64
    (("8.8.8.8", 443), ("8.8.8.8", 40000), False),  # die eigene IPv4-Adresse
    (("::ffff:8.8.4.4", 443, 0, 0), ("8.8.4.4", 40000), False),  # gemappt gegen IPv4
    (("8.8.4.4", 443), ("::ffff:8.8.4.4", 40000, 0, 0), False),  # IPv4 gegen gemappt
    (("2003:1:2:4::1", 443, 0, 0), ("2003:1:2:3::5", 40000, 0, 0), True),  # fremdes /64: erlaubt
    (("2003:1:2:3::1", 443, 0, 0), ("2003:1:2:2:ffff::5", 40000, 0, 0), True),  # Nachbar-/64
    (("8.8.8.8", 443), ("8.8.4.4", 40000), True),  # IPv4: nur die eigene Adresse, kein Netz
    (("8.8.8.8", 443), ("2003:1:2:3::5", 40000, 0, 0), True),
])
def test_the_gateway_itself_and_its_own_ipv6_prefix_are_not_targets(monkeypatch, peer, own, allowed):
    """Im IPv6-Netz haben der Router, LAN-Geraete und der Rechner selbst globale Adressen aus demselben
    /64; `is_global` laesst sie durch. Abgelehnt wird deshalb zusaetzlich die eigene Adresse des Sockets und, bei IPv6,
    alles im /64 der eigenen Adresse. Nicht abgedeckt: Geraete in anderen IPv6-Praefixen des Kundennetzes (andere /64) mit
    globaler Adresse; sie bleiben erreichbar (siehe gateway-host.md, Bekannte Grenzen)."""
    fake = _FakeSocket(peer, own)
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: fake)
    connection = updater._FetchHTTPSConnection("release.example.test", 443, timeout=5,
                                                 deadline=updater._Deadline(time.monotonic), public_only=True)
    if allowed:
        assert connection._create_connection(("release.example.test", 443), 5, None) is fake and not fake.closed
    else:
        with pytest.raises(updater.FetchError, match="nicht oeffentlich"):
            connection._create_connection(("release.example.test", 443), 5, None)
        assert fake.closed and fake.sent == b""


def test_environment_proxies_are_ignored(monkeypatch):
    """Ein Proxy aus der Umgebung (https_proxy) wuerde die Verbindung zum Proxy statt zum Ziel aufbauen: die
    Pruefung saehe nur dessen Adresse, der Proxy loeste das Ziel selbst auf. Der Updater ruft immer direkt. Netzfrei:
    `socket.create_connection` (Grenze) zeichnet die Verbindungsziele auf und scheitert; es darf nur das Ziel vorkommen,
    nie der Proxy (ohne diese Regel kaeme zuerst `127.0.0.1:3128`)."""
    attempts = []

    def refuse(address, *args, **kwargs):
        attempts.append(address)
        raise ConnectionRefusedError

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:3128")
    with pytest.raises(updater.FetchError):
        updater.fetch_url("https://release.example.test/m", 100, allow_http=False)
    assert attempts == [("release.example.test", 443)]


# --- Gesamtfrist auch fuer Verbindung, TLS, Kopfzeilen und Weiterleitungen ---

class _StepClock:
    """Eingespeiste monotone Uhr: `now` setzt der Test."""

    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


class _Gone:
    closed = 0

    def close(self):
        self.closed += 1


def test_a_redirect_after_the_total_deadline_is_refused_and_one_before_it_is_followed():
    """Die Gesamtfrist gilt auch fuer Weiterleitungen: ein Ziel, das erst nach Ablauf der Frist erreichbar waere, wird
    nicht angefragt (die 302-Antwort wird geschlossen). Gegenprobe: knapp vor der Frist folgt der Handler."""
    clock = _StepClock()
    deadline = updater._Deadline(clock)
    handler = updater._SchemeCheckedRedirects(allow_http=False, deadline=deadline)
    request = urllib.request.Request("https://release.example.test/x")
    clock.now = 299.0
    followed = _Gone()
    assert handler.redirect_request(request, followed, 302, "Found", {}, "https://cdn.example.test/x").full_url == \
        "https://cdn.example.test/x"
    assert followed.closed == 1  # auch bei einem erlaubten Ziel wird der Koerper der 302-Antwort nie gelesen
    clock.now = 300.0
    refused = _Gone()
    with pytest.raises(updater.FetchError, match="Gesamtfrist"):
        handler.redirect_request(request, refused, 302, "Found", {}, "https://cdn.example.test/x")
    assert refused.closed == 1


def test_an_expired_deadline_opens_no_connection(byte_counter):
    """Eine Weiterleitung oder ein Abruf nach Ablauf der Frist baut keine Verbindung mehr auf: die Uhr springt nach dem
    Anlegen der Frist ueber 300 s, der lokale Server sieht nie eine Verbindung (der Test entlaesst ihn selbst)."""
    port, received, thread = byte_counter
    ticks = iter([0.0])
    clock = lambda: next(ticks, 1000.0)  # noqa: E731 - erster Aufruf legt die Frist an, jeder weitere liegt dahinter
    with pytest.raises(updater.FetchError, match="Gesamtfrist"):
        updater.fetch_url(f"http://127.0.0.1:{port}/m", 100, allow_http=True, clock=clock)
    assert received == []


@pytest.fixture
def slow_header_server():
    """Startet einen Server auf 127.0.0.1, der jede Verbindung mit `handle(connection)` bedient (optional mit TLS).
    Gibt `(url_base, start)` zurueck."""
    servers = []

    def start(handle, context=None):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        stop = threading.Event()

        def serve():
            listener.settimeout(0.1)
            while not stop.is_set():
                try:
                    connection, _ = listener.accept()
                except OSError:
                    continue
                try:
                    connection.settimeout(5)
                    if context is not None:
                        connection = context.wrap_socket(connection, server_side=True)
                    connection.recv(4096)  # die Anfrage
                    handle(connection)
                except OSError:
                    pass
                finally:
                    connection.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        servers.append((listener, stop, thread))
        return listener.getsockname()[1]

    yield start
    for listener, stop, thread in servers:
        stop.set()
        thread.join()
        listener.close()


def _drip_headers(connection):
    """Statuszeile sofort, dann ein Zeichen einer Kopfzeile je 0,2 s, nie das Ende der Kopfzeilen (1,6 s)."""
    connection.sendall(b"HTTP/1.1 200 OK\r\nX-Langsam: ")
    for _ in range(8):
        time.sleep(0.2)
        connection.sendall(b"x")


def test_a_server_that_drips_its_headers_is_bounded_by_the_total_deadline(monkeypatch, slow_header_server):
    """Kopfzeilen, die Zeichen fuer Zeichen kommen, hielten den Abruf fest (jede Leseoperation blieb unter dem Timeout):
    der Socket-Timeout steht vor jedem Lesen auf dem Rest der Gesamtfrist (hier 0,5 s). Klartext-http nur mit dem
    Schalter der Tests."""
    port = slow_header_server(_drip_headers)
    monkeypatch.setattr(updater, "ABRUF_GESAMT_SECONDS", 0.5)
    started = time.monotonic()
    with pytest.raises(updater.FetchError, match="Gesamtfrist"):
        updater.fetch_url(f"http://127.0.0.1:{port}/m", 100, allow_http=True)
    assert time.monotonic() - started < 2


def test_a_server_that_never_answers_is_bounded_by_the_total_deadline(monkeypatch, slow_header_server):
    """Verbindung steht, die Antwort bleibt aus: nach dem Rest der Gesamtfrist (0,5 s), nicht nach 30 s."""
    port = slow_header_server(lambda connection: time.sleep(1.5))
    monkeypatch.setattr(updater, "ABRUF_GESAMT_SECONDS", 0.5)
    started = time.monotonic()
    with pytest.raises(updater.FetchError, match="Gesamtfrist"):
        updater.fetch_url(f"http://127.0.0.1:{port}/m", 100, allow_http=True)
    assert time.monotonic() - started < 2


def test_a_redirect_answer_is_closed_unread_so_its_endless_body_costs_nothing(monkeypatch, slow_header_server):
    """Review M-1: urllib liest nach einer Weiterleitung den Koerper der 302-Antwort ohne Grenze (`fp.read()`). Ein Host, der
    ihn endlos tropfen laesst, hielte den Abruf bis zur Gesamtfrist fest und sammelte Speicher. Der Handler schliesst die
    Antwort nach dem Bau der Folgeanfrage: der Koerper wird nie gelesen, das Ziel kommt sofort."""
    target_port = slow_header_server(lambda connection: connection.sendall(
        b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nziel"))

    def endless_redirect(connection):
        connection.sendall(b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:%d/z\r\nContent-Length: 100000\r\n\r\n"
                           % target_port)
        for _ in range(15):  # tropft 3 s
            time.sleep(0.2)
            connection.sendall(b"x")

    start_port = slow_header_server(endless_redirect)
    started = time.monotonic()
    assert updater.fetch_url(f"http://127.0.0.1:{start_port}/a", 100, allow_http=True) == b"ziel"
    assert time.monotonic() - started < 1.5


@pytest.fixture
def tls_server(tmp_path, monkeypatch, slow_header_server):
    """Lokaler TLS-Server (selbstsigniertes Zertifikat fuer 127.0.0.1, vom Client fuer diesen Test als Vertrauensanker
    geladen; die Adresspruefungen gelten hier als bestanden, Server und Client liegen auf Loopback): der echte Weg von
    `fetch_url` ueber https, mit Kontext, Socket-Klasse mit Frist und Adresspruefung."""
    import datetime
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
            .sign(key, hashes.SHA256()))
    cert_file, key_file = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    real = ssl.create_default_context
    monkeypatch.setattr(ssl, "create_default_context", lambda *args, **kwargs: real(cafile=str(cert_file)))
    monkeypatch.setattr(updater, "is_public_address", lambda address: True)
    monkeypatch.setattr(updater, "is_own_network", lambda peer, own: False)  # Server und Client liegen beide auf Loopback
    return lambda handle: slow_header_server(handle, context)


def test_fetch_url_reads_over_tls_from_a_server_with_a_trusted_certificate(tls_server):
    """Der echte https-Weg (Kontext, Pruefung der Gegenadresse, Socket mit Frist) liefert den Koerper."""
    port = tls_server(lambda connection: connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhallo"))
    assert updater.fetch_url(f"https://127.0.0.1:{port}/m", 100, allow_http=False) == b"hallo"


def test_a_tls_server_that_drips_its_headers_is_bounded_by_the_total_deadline(monkeypatch, tls_server):
    """Wie bei http, auf dem Weg, den der Betrieb nutzt: der TLS-Socket setzt den Timeout vor jedem Lesen."""
    port = tls_server(_drip_headers)
    monkeypatch.setattr(updater, "ABRUF_GESAMT_SECONDS", 0.5)
    started = time.monotonic()
    with pytest.raises(updater.FetchError, match="Gesamtfrist"):
        updater.fetch_url(f"https://127.0.0.1:{port}/m", 100, allow_http=False)
    assert time.monotonic() - started < 2


def test_a_tls_handshake_that_stalls_is_bounded_by_the_total_deadline(monkeypatch):
    """Der Server nimmt die Verbindung an und schweigt (kein ServerHello): der Handshake endet mit der Gesamtfrist."""
    monkeypatch.setattr(updater, "is_public_address", lambda address: True)
    monkeypatch.setattr(updater, "is_own_network", lambda peer, own: False)
    monkeypatch.setattr(updater, "ABRUF_GESAMT_SECONDS", 0.5)
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen()
    try:
        started = time.monotonic()
        with pytest.raises(updater.FetchError, match="Gesamtfrist"):
            updater.fetch_url(f"https://127.0.0.1:{silent.getsockname()[1]}/m", 100, allow_http=False)
        assert time.monotonic() - started < 2
    finally:
        silent.close()


def test_fetch_health(local_server):
    assert updater.fetch_health(local_server + "/healthz") == {"server_ok": True, "runtime_status": None}
    assert updater.fetch_health("http://127.0.0.1:1/healthz") is None


@pytest.fixture
def healthz_bodies():
    """Server, der je Pfad einen vorgegebenen Koerper liefert (Status 200)."""
    bodies = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(bodies[self.path])

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", bodies
    server.shutdown()
    server.server_close()
    thread.join()


def _healthz_object(size: int) -> bytes:
    raw = json.dumps({"server_ok": True, "runtime_status": None}).encode()
    return raw[:-1] + b" " * (size - len(raw)) + b"}"  # Leerraum vor der Klammer: gueltiges JSON


@pytest.mark.parametrize("body", [b"", b"<html>502</html>", b"{\"server_ok\": true", b"[1, 2]", b"\"server_ok\"", b"null",
                                  b"true", b"42"])
def test_healthz_that_is_not_a_json_object_is_not_healthy(healthz_bodies, body):
    url, bodies = healthz_bodies
    bodies["/ok"], bodies["/x"] = _healthz_object(100), body
    assert updater.fetch_health(url + "/ok") == {"server_ok": True, "runtime_status": None}  # Gegenprobe
    assert updater.health_ok(updater.fetch_health(url + "/ok"), {"runtime": "healthy"}) is True
    assert updater.fetch_health(url + "/x") is None
    assert updater.health_ok(updater.fetch_health(url + "/x"), {"runtime": "healthy"}) is False


def test_healthz_over_64_kib_is_not_healthy_but_exactly_64_kib_is_read(healthz_bodies):
    url, bodies = healthz_bodies
    bodies["/genau"], bodies["/zuviel"] = _healthz_object(65536), _healthz_object(65537)
    assert len(bodies["/genau"]) == 65536 and len(bodies["/zuviel"]) == 65537
    assert updater.fetch_health(url + "/genau") == {"server_ok": True, "runtime_status": None}
    assert updater.health_ok(updater.fetch_health(url + "/genau"), {"runtime": "healthy"}) is True
    assert updater.fetch_health(url + "/zuviel") is None  # abgeschnitten: kein gueltiges JSON mehr
    assert updater.health_ok(updater.fetch_health(url + "/zuviel"), {"runtime": "healthy"}) is False


@pytest.fixture
def reload_updater(monkeypatch):
    """Laedt `updater` neu (die Standard-Uhr `clock=time.monotonic` wird beim Import gebunden) und stellt das Modul nach
    dem Test mit den echten Uhren wieder her."""
    yield lambda: importlib.reload(updater)
    monkeypatch.undo()
    importlib.reload(updater)


def test_the_health_deadline_follows_the_monotonic_clock_not_the_wall_clock(tmp_path, monkeypatch, reload_updater):
    """F-gateway-host-07: die Wanduhr springt beim Boot. `time.monotonic` ist eine steuerbare Uhr (sie laeuft mit
    `sleep`), `time.time` springt bei jeder Abfrage um einen Tag. Der Updater wird unter diesen Attrappen neu geladen und
    ohne eigene Uhr gebaut: seine Standard-Uhr bestimmt die Frist. Sie muss auf der monotonen Uhr ablaufen (drei Takte zu
    10 s bis 30 s); eine Standard-Uhr `time.time` liefe sofort ab (0 Takte) oder, ohne Sprung, nie."""
    monotonic = [0.0]
    wall = [0]

    def jumping_time():
        wall[0] += 1
        return 1.7e9 + 86400.0 * wall[0]

    monkeypatch.setattr(time, "monotonic", create_autospec(time.monotonic, side_effect=lambda: monotonic[0]))
    monkeypatch.setattr(time, "time", create_autospec(time.time, side_effect=jumping_time))
    reload_updater()
    world = World(tmp_path, healthy=False)
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        assert len(waits) < 100, "Die Frist endet nicht"
        monotonic[0] += seconds

    short = updater.Updater(kanal=world.api, store=world.store, compose=world.compose, fetch=world.fetch,
                            fetch_datei=world.fetch_datei, docker_load=world.docker_load,
                            roots_text=world.roots, uhr=lambda: world.now, state_path=world.state_path, version="0.2.0",
                            health=lambda version: False, sleep=sleep, health_seconds=30)  # Standard-Uhr
    assert short.run_once() == "zurueckgerollt"
    assert waits == [10, 10, 10] and monotonic[0] == 30.0


def test_state_file_survives_a_failure_while_it_is_written(tmp_path, monkeypatch):
    """F-gateway-host-08: bricht das Schreiben ab (Stromausfall, Datentraeger voll), bleibt die alte Datei stehen und
    laedt. `os.replace`/`os.fsync` sind die Dateisystem-Grenze (Attrappen mit autospec; die Schreibfunktion testet
    client-kern.md)."""
    path = tmp_path / "updater" / "state.json"
    old = updater.State(current="0.2.0", previous="0.1.0", rejected=["0.1.5"])
    old.save(path)
    before = path.read_bytes()
    new = updater.State(current="0.3.0", previous="0.2.0")
    for name in ("fsync", "replace"):
        with monkeypatch.context() as patch:
            patch.setattr(os, name, create_autospec(getattr(os, name), side_effect=OSError("Stromausfall")))
            with pytest.raises(OSError, match="Stromausfall"):
                new.save(path)
        assert path.read_bytes() == before and updater.State.load(path) == old
    new.save(path)  # Gegenprobe: ohne Fehler ersetzt das Schreiben die Datei
    assert updater.State.load(path) == new and path.read_bytes() != before


def test_the_report_queue_holds_20_entries_and_the_oldest_falls_out(tmp_path):
    """F-gateway-host-10: 21 abgelehnte Versionen in Folge, der Kanal nimmt nichts an (Datentraeger nicht schreibbar)."""
    world = World(tmp_path)
    world.api.broken = True
    offer = dict(world.api.desired_answer)

    def reject(number):
        world.api.desired_answer = offer | {"version": f"0.3.{number}", "manifest_sha256": "0" * 64}
        assert world.updater.run_once() == "abgelehnt"

    for number in range(1, 21):
        reject(number)
    assert [entry["version"] for entry in world.state().pending_reports] == [f"0.3.{n}" for n in range(1, 21)]
    reject(21)  # die 21. Meldung
    pending = world.state().pending_reports
    assert len(pending) == 20
    assert pending[0] == {"version": "0.3.2", "result": "rollback", "reason": "manifest_ungueltig"}  # 0.3.1 fiel weg
    assert pending[-1] == {"version": "0.3.21", "result": "rollback", "reason": "manifest_ungueltig"}
    world.api.broken = False  # der Kanal kommt zurueck: die 20 gehen in Reihenfolge hinaus
    world.api.desired_answer = {"version": "0.2.0"}
    assert world.updater.run_once() == "aktuell"
    assert [version for version, _, _ in world.api.results] == [f"0.3.{n}" for n in range(2, 22)]
    assert world.state().pending_reports == []


def test_a_full_disk_while_installing_is_provisional_and_logged(tmp_path, monkeypatch, caplog):
    """`store.install` scheitert mit ENOSPC (Datentraeger voll). Der Durchlauf endet "vorlaeufig" (naechster Durchlauf
    versucht es erneut), der Fehler steht mit seinem Namen im ERROR-Log, Zustand und Compose bleiben unberuehrt: nichts
    wurde gezogen oder umgeschaltet."""
    world = World(tmp_path)
    before = world.state_path.read_bytes()

    def full(version, files):
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    monkeypatch.setattr(world.store, "install", full)
    with caplog.at_level("INFO"):
        assert world.updater.run_once() == "vorlaeufig"
    errors = [record for record in caplog.records if record.levelname == "ERROR"]
    assert len(errors) == 1 and "ENOSPC" in errors[0].getMessage()
    assert world.state_path.read_bytes() == before and world.compose.calls == []
    assert world.api.results == []  # nichts gemeldet: kein Fehler des Bundles
    # Gegenprobe: ist der Platz wieder da, laeuft derselbe Updater das Update durch
    monkeypatch.undo()
    assert world.updater.run_once() == "aktualisiert"


def test_a_failing_state_file_in_recover_is_logged_and_does_not_raise(tmp_path, monkeypatch, caplog):
    """Auch `recover()` (Start des Dienstes) wirft bei einem Dateifehler nicht: der Dienst kaeme sonst nie hoch."""
    world = World(tmp_path)
    _interrupted(world, previous="0.2.0")
    world.store.install("0.3.0", {"docker-compose.yml": COMPOSE, "mosquitto.conf": MOSQ})

    def full(self, path):
        raise OSError(errno.EIO, "Ein-/Ausgabefehler")

    monkeypatch.setattr(updater.State, "save", full)
    with caplog.at_level("INFO"):
        world.updater.recover()  # wirft nicht
    assert any(record.levelname == "ERROR" and "EIO" in record.getMessage() for record in caplog.records)
    monkeypatch.undo()
    world.updater.recover()  # der naechste Start macht es zu Ende
    assert world.state().current == "0.3.0" and world.state().in_progress is None


def _main_environment(monkeypatch, root: Path, *, offer=None) -> updater.Einstellungen:
    """Nur `SHG_ROOT` ist Umgebung; alles andere reicht der Test als `Einstellungen` an `main` (Rueckgabe)."""
    (root / "data").mkdir(parents=True, exist_ok=True)
    if offer is not None:
        (root / "data" / "software_soll.json").write_text(json.dumps(offer))
    wurzel_key, wurzel = neue_root(von=datetime.now(UTC) - timedelta(days=10))
    (root / "release-roots.pem").write_text(roots_pem(wurzel))
    _host_status(root, zeit_synchron=True)
    monkeypatch.setenv("SHG_ROOT", str(root))
    return updater.Einstellungen(release_roots=root / "release-roots.pem")


def _host_status(root: Path, *, zeit_synchron: bool, ts: datetime | None = None) -> None:
    """`host/status.json` wie der Hoststatus-Dienst sie schreibt (hoststatus.collect/write), Zeitstempel `ts` (Standard:
    jetzt)."""
    stempel = ts or datetime.now(UTC)
    hoststatus.write(root / "host" / "status.json", hoststatus.collect(
        route_file=root / "keine-route", resolve=lambda *args: [], api_host=None, sync_flag=root / "kein-flag",
        timedatectl=lambda: zeit_synchron, now=lambda: stempel))


class _Stop(Exception):
    pass


def test_main_waits_120_seconds_then_runs_every_900_plus_up_to_300_seconds(tmp_path, monkeypatch):
    """F-gateway-host-01: Takt des Dienstes. Echte `Updater`, echter lokaler Kanal (ohne Soll-Datei: "vorlaeufig");
    Uhr (`time.sleep`) und Zufall (`random.uniform`) sind die Grenze: die Folge der Ereignisse und der Wartezeiten ist
    das Ergebnis. Der Zufall liefert erst die obere, dann die untere Schranke seines Bereichs."""
    settings = _main_environment(monkeypatch, tmp_path)
    events = []
    real_recover, real_run_once = updater.Updater.recover, updater.Updater.run_once

    def recover(self):
        # Nur beobachtet, die echte Methode laeuft: dass recover() vor dem ersten Warten kommt, ist die Reihenfolge, die
        # ein unterbrochenes Update nach dem Neustart braucht (up und Gesundheit, bevor ein neuer Durchlauf beginnt).
        # Ein unterbrochenes in_progress braeuchte ein echtes ComposeRunner/docker, das main baut: hier nicht nachstellbar.
        events.append("recover")
        real_recover(self)

    def run_once(self):
        result = real_run_once(self)
        events.append(f"durchlauf:{result}")
        return result

    def sleep(seconds):
        events.append(f"warten:{float(seconds)}")
        if len([event for event in events if event.startswith("warten")]) == 4:
            raise _Stop

    bounds = iter([lambda low, high: high, lambda low, high: low, lambda low, high: (low + high) / 2])
    monkeypatch.setattr(updater.Updater, "recover", recover)
    monkeypatch.setattr(updater.Updater, "run_once", run_once)
    monkeypatch.setattr(time, "sleep", create_autospec(time.sleep, side_effect=sleep))
    monkeypatch.setattr(random, "uniform", create_autospec(random.uniform, side_effect=lambda a, b: next(bounds)(a, b)))
    with pytest.raises(_Stop):
        updater.main([], einstellungen=settings)
    assert events == ["recover", "warten:120.0", "durchlauf:vorlaeufig", "warten:1200.0",
                      "durchlauf:vorlaeufig", "warten:900.0", "durchlauf:vorlaeufig", "warten:1050.0"]


def test_main_keeps_running_after_an_unexpected_error(tmp_path, monkeypatch, caplog):
    """Ein unerwarteter Fehler in einem Durchlauf beendet den Dienst nicht (er laeuft sonst erst nach `RestartSec`
    neu, mit 120 s Wartezeit); er wird mit seiner Spur geloggt, nach der ueblichen Pause kommt der naechste Durchlauf.
    Der Fehler kommt aus `run_once`, der Grenze zum Rest des Dienstes."""
    settings = _main_environment(monkeypatch, tmp_path)
    calls = []

    def run_once(self):
        calls.append("durchlauf")
        if len(calls) == 1:
            raise RuntimeError("unerwartet")
        return "aktuell"

    waits = []

    def sleep(seconds):
        waits.append(float(seconds))
        if len(waits) == 3:
            raise _Stop

    monkeypatch.setattr(updater.Updater, "run_once", run_once)
    monkeypatch.setattr(time, "sleep", create_autospec(time.sleep, side_effect=sleep))
    monkeypatch.setattr(random, "uniform", create_autospec(random.uniform, return_value=0))
    with caplog.at_level("INFO"), pytest.raises(_Stop):
        updater.main([], einstellungen=settings)
    assert calls == ["durchlauf", "durchlauf"] and waits == [120.0, 900.0, 900.0]
    logged = [record for record in caplog.records if record.levelname == "ERROR" and record.exc_info]
    assert len(logged) == 1 and logged[0].exc_info[0] is RuntimeError
    assert "Durchlauf: aktuell" in caplog.text


def test_main_keeps_running_when_the_recovery_at_start_fails_unexpectedly(tmp_path, monkeypatch, caplog):
    """Review M-5: `recover()` beim Start liegt unter demselben Fangnetz wie der Durchlauf; sonst startet systemd den Dienst
    bei einem dauerhaften Fehler alle 30 s neu (mit `compose up` in jedem `recover`), ohne je einen Durchlauf zu machen."""
    settings = _main_environment(monkeypatch, tmp_path)

    def recover(self):
        raise RuntimeError("kaputt beim Start")

    waits = []

    def sleep(seconds):
        waits.append(float(seconds))
        if len(waits) == 2:
            raise _Stop

    monkeypatch.setattr(updater.Updater, "recover", recover)
    monkeypatch.setattr(time, "sleep", create_autospec(time.sleep, side_effect=sleep))
    monkeypatch.setattr(random, "uniform", create_autospec(random.uniform, return_value=0))
    with caplog.at_level("INFO"), pytest.raises(_Stop):
        updater.main([], einstellungen=settings)
    assert waits == [120.0, 900.0] and "Durchlauf: vorlaeufig" in caplog.text
    logged = [record for record in caplog.records if record.levelname == "ERROR" and record.exc_info]
    assert len(logged) == 1 and logged[0].exc_info[0] is RuntimeError


def test_main_keeps_the_state_and_the_data_under_shg_root(tmp_path, monkeypatch, capsys):
    """F-gateway-host-01: `SHG_ROOT` ist die Wurzel von Zustand (`updater/state.json`), Soll- und Meldungsdatei (`data/`)."""
    offer = {"version": "0.3.0", "manifest_url": "http://127.0.0.1:1/manifest.json", "manifest_sha256": "a" * 64,
             "signature": "x"}
    settings = _main_environment(monkeypatch, tmp_path, offer=offer)
    updater.main(["--once"], einstellungen=settings)
    assert capsys.readouterr().out == "abgelehnt\n"
    assert json.loads((tmp_path / "updater" / "state.json").read_text())["rejected"] == [
        "0.3.0@" + hashlib.sha256(b"a" * 64).hexdigest()]  # kein Zertifikat im Text: nur das Manifest zaehlt
    assert json.loads((tmp_path / "data" / "software_meldungen.json").read_text())["meldungen"][0]["version"] == "0.3.0"


def test_main_once_recovers_runs_once_and_does_not_wait(tmp_path, monkeypatch, capsys):
    settings = _main_environment(monkeypatch, tmp_path)
    events = []
    real_recover = updater.Updater.recover
    monkeypatch.setattr(updater.Updater, "recover", lambda self: (events.append("recover"), real_recover(self)))
    monkeypatch.setattr(time, "sleep", create_autospec(time.sleep, side_effect=_Stop))
    updater.main(["--once"], einstellungen=settings)
    assert events == ["recover"] and capsys.readouterr().out == "vorlaeufig\n"


@pytest.mark.parametrize(("allow", "outcome"), [
    (True, "vorlaeufig"),  # http erlaubt: der Abruf wird versucht (Port 1: keine Verbindung)
    (False, "abgelehnt"),
])
def test_main_accepts_an_http_offer_only_with_allow_http_in_the_settings(tmp_path, monkeypatch, capsys, allow, outcome):
    """Der Produktivwert ist "aus"; nur `Einstellungen(allow_http=True)` schaltet http frei
    (Tests, Labor)."""
    offer = {"version": "0.3.0", "manifest_url": "http://127.0.0.1:1/manifest.json", "manifest_sha256": "a" * 64,
             "signature": "x"}
    settings = _main_environment(monkeypatch, tmp_path, offer=offer)
    updater.main(["--once"], einstellungen=dataclasses.replace(settings, allow_http=allow))
    assert capsys.readouterr().out == outcome + "\n"
    reports = tmp_path / "data" / "software_meldungen.json"
    if outcome == "abgelehnt":  # die http-Adresse wurde ohne Abruf abgelehnt und gemeldet
        assert [(m["version"], m["reason"]) for m in json.loads(reports.read_text())["meldungen"]] == [
            ("0.3.0", "manifest_ungueltig")]
    else:  # der Abruf scheiterte am Netz: vorlaeufig, nichts gemeldet
        assert not reports.exists()


@pytest.mark.parametrize("value", ["1", "true", "0", ""])
def test_main_ignores_every_environment_switch_of_the_old_names(tmp_path, monkeypatch, capsys, site, value):
    """`SHG_UPDATER_ALLOW_HTTP`, `SHG_UPDATER_HEALTH_URL`, `SHG_UPDATER_HEALTH_SECONDS`, `SHG_RELEASE_PUB`,
    `SHG_RELEASE_ROOTS` und `SHG_COMPOSE_PROJECT` gelten nicht mehr: in einer Umgebung, die sie setzt (etwa einer gateway.env mit
    `SHG_UPDATER_ALLOW_HTTP=1`), bleibt http abgelehnt, das Projekt `smartheat`, der Schluesselbund der Einstellungen."""
    offer = {"version": "0.3.0", "manifest_url": site.url + "/0.3.0/manifest.json", "manifest_sha256": "a" * 64,
             "signature": "x"}
    settings = _main_environment(monkeypatch, tmp_path, offer=offer)
    monkeypatch.setenv("SHG_UPDATER_ALLOW_HTTP", value)
    monkeypatch.setenv("SHG_UPDATER_HEALTH_URL", site.url + "/gesundheit")
    monkeypatch.setenv("SHG_UPDATER_HEALTH_SECONDS", "0.05")
    monkeypatch.setenv("SHG_RELEASE_PUB", str(tmp_path / "gibt-es-nicht.pub"))
    monkeypatch.setenv("SHG_RELEASE_ROOTS", str(tmp_path / "gibt-es-nicht.pem"))
    monkeypatch.setenv("SHG_COMPOSE_PROJECT", "anders")
    updater.main(["--once"], einstellungen=settings)  # liest release-roots.pem aus den Einstellungen, sonst FileNotFoundError
    assert capsys.readouterr().out == "abgelehnt\n" and site.hits == []
    reports = json.loads((tmp_path / "data" / "software_meldungen.json").read_text())["meldungen"]
    assert [(m["version"], m["reason"]) for m in reports] == [("0.3.0", "manifest_ungueltig")]


@pytest.mark.parametrize(("synchron", "alter"), [(False, timedelta(0)), (True, timedelta(minutes=16))])
def test_main_wartet_ohne_synchrone_uhr_aus_dem_hoststatus(tmp_path, monkeypatch, capsys, site, synchron, alter):
    """`main` liest die Uhr aus `<SHG_ROOT>/host/status.json`: unsynchron oder veraltet heisst kein Versuch und keine
    Ablehnung (das Angebot waere sonst als http-Adresse abgelehnt worden)."""
    offer = {"version": "0.3.0", "manifest_url": site.url + "/0.3.0/manifest.json", "manifest_sha256": "a" * 64,
             "signature": "x"}
    settings = _main_environment(monkeypatch, tmp_path, offer=offer)
    _host_status(tmp_path, zeit_synchron=synchron, ts=datetime.now(UTC) - alter)
    updater.main(["--once"], einstellungen=settings)
    assert capsys.readouterr().out == "wartet_auf_zeit\n" and site.hits == []
    assert not (tmp_path / "data" / "software_meldungen.json").exists()


def test_main_ohne_hoststatus_wartet_auf_die_zeit(tmp_path, monkeypatch, capsys):
    offer = {"version": "0.3.0", "manifest_url": "https://x.example.test/m.json", "manifest_sha256": "a" * 64,
             "signature": "x"}
    settings = _main_environment(monkeypatch, tmp_path, offer=offer)
    (tmp_path / "host" / "status.json").unlink()
    updater.main(["--once"], einstellungen=settings)
    assert capsys.readouterr().out == "wartet_auf_zeit\n"


def test_the_default_settings_are_the_production_values():
    """Die Standardwerte von `Einstellungen` sind die des Geraets (install.sh: Schluessel unter /opt/smartheat/host,
    Projekt `smartheat`, Gesundheitsserver des Gateways auf 127.0.0.1:8081, nicht die Diagnoseseite auf Port 80); http bleibt aus."""
    assert updater.Einstellungen() == updater.Einstellungen(
        release_roots=Path("/opt/smartheat/host/release-roots.pem"), allow_http=False, health_url="http://127.0.0.1:8081/healthz",
        health_seconds=600, compose_project="smartheat")
    assert updater.Einstellungen().health_seconds == updater.HEALTH_SECONDS == 600
    assert updater.Einstellungen().compose_project == bundles.COMPOSE_PROJECT


class _Site:
    """Lokaler HTTP-Server: `routes` bildet einen Pfad auf Bytes oder eine Funktion `() -> bytes` ab; `hits` haelt jede
    angefragte Adresse fest."""

    def __init__(self):
        self.routes, self.hits = {}, []
        site = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                site.hits.append(self.path)
                body = site.routes.get(self.path)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body() if callable(body) else body)

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()


@pytest.fixture
def site():
    site = _Site()
    yield site
    site.server.shutdown()
    site.server.server_close()
    site.thread.join()


@pytest.fixture
def docker(monkeypatch):
    """`docker` an der Unterprozess-Grenze: `subprocess.run` (autospec) wird vor dem Neuladen von `bundles` ersetzt, weil
    `ComposeRunner` es beim Import als Standard bindet. `docker.calls` haelt jeden Aufruf, `docker.runtime_health` ist die
    Antwort von `docker compose ps`."""
    class Docker:
        calls: list = []
        runtime_health = "healthy"
        loaded = False  # nach `docker load` kennt der Docker das neue Image

    def run(command, **kwargs):
        Docker.calls.append(list(command))
        stdout = ""
        if "ps" in command:
            stdout = json.dumps([{"Service": "runtime", "Health": Docker.runtime_health}])
        if command[:2] == ["docker", "load"]:
            Docker.loaded = True
        returncode = 1 if command[:3] == ["docker", "image", "inspect"] and not Docker.loaded else 0
        return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr="")

    Docker.calls = []
    Docker.loaded = False
    monkeypatch.setattr(subprocess, "run", create_autospec(subprocess.run, side_effect=run))
    importlib.reload(bundles)
    yield Docker
    monkeypatch.undo()
    importlib.reload(bundles)


HEALTHY_BODY = json.dumps({"server_ok": True, "runtime_status": None}).encode()
UNHEALTHY_BODY = json.dumps({"server_ok": False, "runtime_status": None}).encode()


def _main_update(monkeypatch, root, site, *, signed_by_other_root=False, release_roots_of_other_root=False,
                 health_path="/healthz", health_body=HEALTHY_BODY, project=None, health_seconds=None,
                 min_updater_version="0.1.0") -> updater.Einstellungen:
    """Bereitet einen echten `main --once` vor: ein signiertes Bundle 0.3.0 auf dem lokalen Server, das Angebot in
    `data/software_soll.json`, `release-roots.pem`, ein erreichbarer Broker (der Server selbst) und die Einstellungen
    (Rueckgabe). Zertifikate gelten um die echte Zeit, `host/status.json` meldet eine synchrone Uhr."""
    jetzt = datetime.now(UTC)
    roots = [neue_root(cn=name, von=jetzt - timedelta(days=10)) for name in ("Root", "Andere Root")]
    keys = [release(*pair, von=jetzt - timedelta(days=1), bis=jetzt + timedelta(days=30)) for pair in roots]
    signer = keys[1 if signed_by_other_root else 0]
    files = {"docker-compose.yml": COMPOSE, "mosquitto.conf": MOSQ}
    raw = json.dumps({"version": "0.3.0", "min_updater_version": min_updater_version,
                      "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
                      "images": {"gateway": IMAGE}, "archiv": dict(ARCHIV)}).encode()
    signature = signiere(*signer, raw)
    site.routes.update({"/0.3.0/manifest.json": raw, **{f"/0.3.0/{name}": data for name, data in files.items()},
                        "/0.3.0/" + bundles.ARCHIVE_NAME: ARCHIV_INHALT, health_path: health_body})
    offer = {"version": "0.3.0", "manifest_url": site.url + "/0.3.0/manifest.json", "signature": signature,
             "manifest_sha256": hashlib.sha256(raw).hexdigest()}
    settings = _main_environment(monkeypatch, root, offer=offer)
    (root / "release-roots.pem").write_text(roots_pem(roots[1][1] if release_roots_of_other_root else roots[0][1]))
    (root / "data" / "device").mkdir()
    (root / "data" / "device" / "endpoint.json").write_text(json.dumps({"host": "127.0.0.1", "port": site.port}))
    settings = dataclasses.replace(settings, allow_http=True, health_url=site.url + health_path)
    if project is not None:
        settings = dataclasses.replace(settings, compose_project=project)
    if health_seconds is not None:
        settings = dataclasses.replace(settings, health_seconds=health_seconds)
    return settings


@pytest.mark.parametrize(("release_roots_of_other_root", "outcome"), [(False, "aktualisiert"), (True, "abgelehnt")])
def test_main_verifies_the_bundle_with_the_roots_of_the_release_roots_setting(
        tmp_path, monkeypatch, capsys, site, docker, fast_sleep, release_roots_of_other_root, outcome):
    """`Einstellungen.release_roots` nennt den Schluesselbund: ein Bundle, dessen Zertifikat eine andere Root ausgestellt hat
    (hier: die Datei traegt nur diese andere Root), wird abgelehnt und nie gestartet, mit der richtigen angenommen."""
    settings = _main_update(monkeypatch, tmp_path, site, release_roots_of_other_root=release_roots_of_other_root)
    updater.main(["--once"], einstellungen=settings)
    assert capsys.readouterr().out == outcome + "\n"
    started = [call for call in docker.calls if "up" in call]
    reports = json.loads((tmp_path / "data" / "software_meldungen.json").read_text())["meldungen"]
    if outcome == "abgelehnt":
        assert started == [] and [(m["result"], m["reason"]) for m in reports] == [("rollback", "signatur_ungueltig")]
    else:
        assert len(started) == 1 and [(m["result"], m["reason"]) for m in reports] == [("ok", "")]


@pytest.mark.parametrize(("hoeher", "outcome"), [(False, "aktualisiert"), (True, "abgelehnt")])
def test_main_runs_the_updater_with_the_gateway_version_as_its_own(
        tmp_path, monkeypatch, capsys, site, docker, fast_sleep, hoeher, outcome):
    """Die laufende Version des Updaters ist `GATEWAY_VERSION` (nicht die des Bundles, nicht ein fester Wert): ein Bundle,
    das genau diese Version verlangt, wird angenommen, eines, das eine Patch-Stelle mehr verlangt, ist `updater_zu_alt` und
    wird nie gestartet. Das Bundle selbst ist 0.3.0 (ueber 0.1.0, unter 0.6.0): eine Verwechslung mit seiner Version oder
    mit einem festen kleinen Wert faellt in je einer der beiden Zeilen auf."""
    major, minor, patch = (int(part) for part in GATEWAY_VERSION.split("."))
    verlangt = f"{major}.{minor}.{patch + 1}" if hoeher else GATEWAY_VERSION
    settings = _main_update(monkeypatch, tmp_path, site, min_updater_version=verlangt)
    updater.main(["--once"], einstellungen=settings)
    assert capsys.readouterr().out == outcome + "\n"
    started = [call for call in docker.calls if "up" in call]
    reports = json.loads((tmp_path / "data" / "software_meldungen.json").read_text())["meldungen"]
    if hoeher:
        assert started == [] and [(m["result"], m["reason"]) for m in reports] == [("rollback", "updater_zu_alt")]
    else:
        assert len(started) == 1 and [(m["result"], m["reason"]) for m in reports] == [("ok", "")]


@pytest.mark.parametrize(("project", "expected"), [("testprojekt", "testprojekt"), (None, "smartheat")])
def test_main_passes_the_compose_project_setting_to_every_docker_compose_call(
        tmp_path, monkeypatch, capsys, site, docker, fast_sleep, project, expected):
    settings = _main_update(monkeypatch, tmp_path, site, project=project)
    updater.main(["--once"], einstellungen=settings)
    assert capsys.readouterr().out == "aktualisiert\n"
    compose_calls = [call for call in docker.calls if call[:2] == ["docker", "compose"]]
    assert {call[call.index("-p") + 1] for call in compose_calls} == {expected}
    assert {word for call in compose_calls for word in call if word in ("pull", "up", "ps")} == {"pull", "up", "ps"}


def test_main_asks_exactly_the_address_of_the_health_url_setting(
        tmp_path, monkeypatch, capsys, site, docker, fast_sleep):
    settings = _main_update(monkeypatch, tmp_path, site, health_path="/gesundheit")
    site.routes["/healthz"] = UNHEALTHY_BODY  # die Standardadresse waere ungesund: sie darf nicht gefragt werden
    updater.main(["--once"], einstellungen=settings)
    assert capsys.readouterr().out == "aktualisiert\n"
    assert "/gesundheit" in site.hits and "/healthz" not in site.hits


@pytest.fixture
def fast_sleep(monkeypatch, reload_updater):
    """Die Takte der Gesundheitspruefung (10 s) laufen in 10 ms; eine Frist, die nicht endet, bricht nach 300 Takten ab.
    `updater` wird neu geladen, weil `Updater` `time.sleep` beim Import als Standard bindet."""
    real_sleep, calls = time.sleep, []

    def sleep(seconds):
        calls.append(seconds)
        assert len(calls) < 300, "Die Frist endet nicht"
        real_sleep(0.01)

    monkeypatch.setattr(time, "sleep", create_autospec(time.sleep, side_effect=sleep))
    reload_updater()
    return calls


def test_main_ends_the_health_wait_after_the_health_seconds_setting(
        tmp_path, monkeypatch, capsys, site, docker, fast_sleep):
    """Ein Bundle, das nie gesund wird: mit 0,05 s Frist endet das Warten und `main` rollt zurueck (Grund `ungesund`),
    nachdem die Frist auf der monotonen Uhr verstrichen ist; mit dem Standard von 600 s bricht die Fixture `fast_sleep`
    nach 300 Takten ab. Die Zahl der Takte haengt von der Last ab und wird nicht zugesichert."""
    settings = _main_update(monkeypatch, tmp_path, site, health_path="/gesundheit", health_body=UNHEALTHY_BODY,
                            health_seconds=0.05)
    started = time.monotonic()
    updater.main(["--once"], einstellungen=settings)
    assert time.monotonic() - started >= 0.05
    assert capsys.readouterr().out == "zurueckgerollt\n"
    reports = json.loads((tmp_path / "data" / "software_meldungen.json").read_text())["meldungen"]
    assert [(m["result"], m["reason"]) for m in reports] == [("rollback", "ungesund")]


def test_main_waits_longer_for_a_bundle_that_turns_healthy_when_the_deadline_is_long(
        tmp_path, monkeypatch, capsys, site, docker, fast_sleep):
    """Gegenprobe zum vorigen Test: dasselbe Bundle, ab der vierten Anfrage gesund, mit 1000 s Frist: aktualisiert."""
    settings = _main_update(monkeypatch, tmp_path, site, health_path="/gesundheit", health_seconds=1000)
    site.routes["/gesundheit"] = lambda: HEALTHY_BODY if site.hits.count("/gesundheit") >= 4 else UNHEALTHY_BODY
    updater.main(["--once"], einstellungen=settings)
    assert capsys.readouterr().out == "aktualisiert\n" and len(fast_sleep) == 3


@pytest.mark.parametrize("current", ["0.3.1", "1.0.0"])
def test_older_desired_version_is_refused_without_download(tmp_path, current):
    world = World(tmp_path)
    updater.State(current=current).save(world.state_path)
    world.served.clear()  # ein Download wuerde scheitern - es darf keinen geben
    assert world.updater.run_once() == "abgelehnt"
    assert world.api.results == [("0.3.0", "rollback", "version_zu_alt")]
    state = world.state()
    assert state.current == current and state.rejected == [world.abgelehnt()]
    assert world.compose.calls == []
    assert world.updater.run_once() == "aktuell"  # bleibt abgelehnt, keine zweite Meldung
    assert len(world.api.results) == 1


def test_unreadable_current_version_does_not_block_updates(tmp_path):
    world = World(tmp_path)
    updater.State(current="kaputt").save(world.state_path)
    assert world.updater.run_once() == "aktualisiert"


def test_downgrade_reason_is_in_the_fixed_list():
    assert "version_zu_alt" in updater.REASONS
    assert updater.RETRY_REASON in updater.REASONS


def test_der_updater_meldet_nur_gruende_aus_reasons():
    """Jeder Grund, den der Updater an _Refused, _reject, _rollback oder _queue gibt, ist ein Name aus REASONS
    oder ein durchgereichter Wert, nie ein Literal; so meldet er nur Gruende, die test_vertrag_im_host.py gegen den
    Vertrag prueft. Ausnahme: _queue meldet das Ergebnis "ok" mit leerem Grund."""
    tree = ast.parse(Path(updater.__file__).read_text(encoding="utf-8"))
    by_value = {name: getattr(updater, name) for name in dir(updater) if name.isupper()}
    position = {"_Refused": 0, "_reject": 2, "_rollback": 2, "_queue": 3}  # Argument-Index ohne self
    seen = set()
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        name = call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", None)
        if name not in position:
            continue
        if len(call.args) > position[name]:
            reason = call.args[position[name]]
        else:
            keywords = [keyword.value for keyword in call.keywords if keyword.arg == "reason"]
            assert keywords, f"Zeile {call.lineno}: {name} ohne erkennbaren Grund (weder Position noch reason=)"
            reason = keywords[0]
        if name == "_queue" and isinstance(reason, ast.Constant) and reason.value == "":
            assert len(call.args) > 2, (
                f"Zeile {call.lineno}: _queue mit leerem Grund braucht das Ergebnis auf Position 2"
            )
            assert isinstance(call.args[2], ast.Constant) and call.args[2].value == "ok", (
                f"Zeile {call.lineno}: leerer Grund nur beim Ergebnis \"ok\""
            )
            continue
        assert not isinstance(reason, ast.Constant), f"Zeile {call.lineno}: Grund als Literal {reason.value!r}"
        if isinstance(reason, ast.Name) and reason.id in by_value:
            assert by_value[reason.id] in updater.REASONS, f"Zeile {call.lineno}: {reason.id} nicht in REASONS"
            seen.add(by_value[reason.id])
    assert seen == set(updater.REASONS)


def test_reachability_is_the_broker_not_the_bundle(tmp_path):
    # Das neue Bundle ist ungesund, der Broker erreichbar -> nach der Frist zurueck und abgelehnt;
    # ist der Broker nicht erreichbar, ruht die Frist und die Version wird nicht abgelehnt.
    world = World(tmp_path, healthy=False)
    assert world.updater.run_once() == "zurueckgerollt"
    assert world.api.results[-1] == ("0.3.0", "rollback", "ungesund") and world.state().rejected == [world.abgelehnt()]
    (tmp_path / "zwei").mkdir()
    offline = World(tmp_path / "zwei", healthy=False)
    offline.api.offline = True
    assert offline.updater.run_once() == "zurueckgerollt"
    assert offline.api.results[-1] == ("0.3.0", "rollback", updater.RETRY_REASON)
    assert offline.state().rejected == []


def test_only_the_runtime_container_must_be_healthy():
    assert updater.REQUIRED_HEALTHY == ("runtime",)
    assert updater.health_ok({"server_ok": True, "runtime_status": "regelt"}, {"runtime": "healthy"})


# --- Eigenes Image als OCI-Archiv im Bundle -----------------------------------------------------------------------

def _archiv_ordner(world) -> Path:
    return world.store.root / "archive"


def _event_namen(world):
    return [event[0] for event in world.events]


def test_update_holt_laedt_und_startet_das_archiv_vor_pull_und_up(tmp_path):
    """Reihenfolge nach der Kettenpruefung: Image fehlt, Archiv holen, laden, Image da, dann pull (nur Registries) und up."""
    world = World(tmp_path)
    assert world.updater.run_once() == "aktualisiert"
    namen = _event_namen(world)
    assert namen[:5] == ["has_image", "fetch_datei", "docker_load", "has_image", "pull"] and namen[5] == "up"
    assert world.events[0][1:] == (IMAGE, False) and world.events[3][1:] == (IMAGE, True)
    url, max_bytes, ziel, _ = world.archive_fetches[0]
    assert url == BASE + bundles.ARCHIVE_NAME  # neben dem Manifest
    assert max_bytes == len(ARCHIV_INHALT) and ziel == world.store.archive_path("0.3.0")
    assert world.loaded == [("0.3.0.oci.tar", ARCHIV_INHALT)]
    assert list(_archiv_ordner(world).iterdir()) == []  # nach dem Laden geloescht, auch kein .part
    assert world.store.dir("0.3.0").is_dir() and not (world.store.dir("0.3.0") / bundles.ARCHIVE_NAME).exists()
    assert IMAGE in world.compose.images and ALTES_IMAGE in world.compose.images


def test_ist_das_image_schon_da_wird_das_archiv_nicht_geholt(tmp_path):
    """Nach einem gescheiterten pull (oder einem anderen Abbruch) ist das Image geladen: kein zweiter Abruf von rund 200 MB."""
    world = World(tmp_path, fail_pull=True)
    assert world.updater.run_once() == "vorlaeufig"
    assert len(world.archive_fetches) == 1 and IMAGE in world.compose.images
    world.compose.fail_pull = False
    assert world.updater.run_once() == "aktualisiert"
    assert len(world.archive_fetches) == 1 and len(world.loaded) == 1  # kein weiterer Abruf, kein weiteres Laden


def test_archiv_mit_falscher_pruefsumme_wird_abgelehnt(tmp_path):
    """Gleiche Groesse, ein Byte anders: der Server (oder der Weg) liefert ein anderes Archiv als signiert."""
    world = World(tmp_path)
    falsch = bytearray(ARCHIV_INHALT)
    falsch[7] ^= 1
    world.served[BASE + bundles.ARCHIVE_NAME] = bytes(falsch)
    assert world.updater.run_once() == "abgelehnt"
    assert world.api.results == [("0.3.0", "rollback", "manifest_ungueltig")]
    assert world.loaded == [] and not any(call[0] in ("pull", "up") for call in world.compose.calls)
    assert list(_archiv_ordner(world).iterdir()) == []  # das falsche Archiv bleibt nicht liegen
    assert not world.store.exists("0.3.0") and not world.store.dir("0.3.0").exists()
    state = world.state()
    assert (state.current, state.in_progress) == ("0.2.0", None) and state.rejected == [world.abgelehnt()]
    assert IMAGE not in world.compose.images
    # Gegenprobe: mit dem richtigen Archiv geht dasselbe Angebot durch
    (tmp_path / "zwei").mkdir()
    assert World(tmp_path / "zwei").updater.run_once() == "aktualisiert"


def test_archiv_ueber_der_grenze_wird_abgelehnt_ohne_rest(tmp_path):
    """(a) Das Manifest nennt mehr als MAX_ARCHIVE_BYTES: abgelehnt, bevor irgendeine Datei oder das Archiv geholt wird.
    (b) Der Server liefert mehr, als das Manifest nennt: TooLarge, abgelehnt, nichts bleibt in archive/ liegen."""
    zu_gross = dict(ARCHIV, bytes=bundles.MAX_ARCHIVE_BYTES + 1)
    world = World(tmp_path, manifest_over={"archiv": zu_gross})
    assert world.updater.run_once() == "abgelehnt"
    assert world.api.results == [("0.3.0", "rollback", "manifest_ungueltig")]
    assert world.fetched == [BASE + "manifest.json"] and world.archive_fetches == []
    _assert_nothing_installed(world)
    (tmp_path / "zwei").mkdir()
    zwei = World(tmp_path / "zwei", manifest_over={"archiv": dict(ARCHIV, bytes=len(ARCHIV_INHALT) - 10)})
    assert zwei.updater.run_once() == "abgelehnt"
    assert zwei.api.results == [("0.3.0", "rollback", "manifest_ungueltig")]
    assert zwei.loaded == [] and list(_archiv_ordner(zwei).iterdir()) == []
    assert not any(call[0] in ("pull", "up") for call in zwei.compose.calls)
    assert zwei.state().rejected == [zwei.abgelehnt()]


def test_frist_waechst_mit_der_groesse():
    """Frist des Archivabrufs: 60 s plus Groesse bei mindestens 2 Mbit/s (250000 Byte/s)."""
    assert updater.MIN_RATE_BYTES_PER_S == 250000
    assert updater.archiv_frist(0) == 60
    assert updater.archiv_frist(250000) == 61
    assert updater.archiv_frist(10 * 250000) == 70
    assert updater.archiv_frist(bundles.MAX_ARCHIVE_BYTES) == 60 + 536870912 / 250000  # rund 36,8 Minuten
    assert updater.archiv_frist(2_000_000) > updater.archiv_frist(1_000_000) > updater.ABRUF_GESAMT_SECONDS - 300


def test_der_updater_uebergibt_dem_abruf_die_frist_seiner_groesse(tmp_path):
    world = World(tmp_path)
    assert world.updater.run_once() == "aktualisiert"
    assert world.archive_fetches[0][3] == 60 + len(ARCHIV_INHALT) / 250000
    (tmp_path / "gross").mkdir()
    gross = World(tmp_path / "gross", manifest_over={"archiv": dict(ARCHIV, bytes=200_000_000)})
    gross.served[BASE + bundles.ARCHIVE_NAME] = b""  # die Antwort ist egal: die Attrappe liefert FetchError/TooLarge
    gross.updater.run_once()
    assert gross.archive_fetches[0][1:2] == (200_000_000,) and gross.archive_fetches[0][3] == 60 + 800


def test_abgebrochener_abruf_wird_beim_naechsten_durchlauf_fortgesetzt(tmp_path):
    """Updater-Seite: ein abgebrochener Abruf ist `vorlaeufig` (keine Ablehnung, nichts installiert, kein Stack-Wechsel);
    der Anfang bleibt als .part liegen, und der naechste Durchlauf setzt dahinter fort. (Die Range-Anfrage selbst:
    test_fetch_url_to_file_setzt_einen_abgebrochenen_abruf_per_range_fort.)"""
    world = World(tmp_path)
    world.fetch_breaks_after = 100
    assert world.updater.run_once() == "vorlaeufig"
    teil = world.store.archive_path("0.3.0").with_name("0.3.0.oci.tar.part")
    assert teil.read_bytes() == ARCHIV_INHALT[:100] and not world.store.archive_path("0.3.0").exists()
    state = world.state()
    assert (state.current, state.rejected, state.in_progress) == ("0.2.0", [], None) and world.api.results == []
    assert not any(call[0] in ("pull", "up") for call in world.compose.calls) and not world.store.exists("0.3.0")
    assert world.updater.run_once() == "aktualisiert"
    assert len(world.archive_fetches) == 2 and world.loaded == [("0.3.0.oci.tar", ARCHIV_INHALT)]
    assert list(_archiv_ordner(world).iterdir()) == []


def test_nach_dem_fortsetzen_mit_falscher_pruefsumme_wird_neu_geladen_statt_abgelehnt(tmp_path):
    """Ein altes .part aus einem anderen Angebot derselben Version (neu signiert) ergibt beim Fortsetzen ein falsches
    Ganzes: das ist kein Fehler des Angebots. Der Rest wird verworfen, `vorlaeufig`, der naechste Durchlauf laedt neu."""
    world = World(tmp_path)
    _archiv_ordner(world).mkdir()
    teil = _archiv_ordner(world) / "0.3.0.oci.tar.part"
    teil.write_bytes(b"Rest aus einem anderen Angebot")
    assert world.updater.run_once() == "vorlaeufig"
    assert list(_archiv_ordner(world).iterdir()) == [] and world.state().rejected == [] and world.api.results == []
    assert world.loaded == []
    assert world.updater.run_once() == "aktualisiert"
    assert world.loaded == [("0.3.0.oci.tar", ARCHIV_INHALT)]


def test_ein_zu_kurzes_archiv_ohne_laengenangabe_wird_als_teil_fortgesetzt(tmp_path):
    """Der Server beendet den Strom ohne Content-Length vor der Zeit: der Abruf gilt als fertig, die Datei ist aber
    kleiner als signiert. Das ist ein Abbruch (`vorlaeufig`), das Stueck wird .part und der naechste Durchlauf setzt fort."""
    world = World(tmp_path)
    original = world.fetch_datei

    def kurz(url, max_bytes, ziel, frist):
        world.served[url] = ARCHIV_INHALT[:80]
        try:
            return original(url, max_bytes, ziel, frist)
        finally:
            world.served[url] = ARCHIV_INHALT

    world.updater._fetch_datei = kurz
    assert world.updater.run_once() == "vorlaeufig"
    teil = world.store.archive_path("0.3.0").with_name("0.3.0.oci.tar.part")
    assert teil.read_bytes() == ARCHIV_INHALT[:80] and not world.store.archive_path("0.3.0").exists()
    assert world.state().rejected == [] and world.loaded == []
    world.updater._fetch_datei = world.fetch_datei
    assert world.updater.run_once() == "aktualisiert"


def test_fehlendes_image_nach_dem_laden_wird_abgelehnt_und_stack_bleibt(tmp_path):
    """`docker load` gelingt, kennt das Image `smartheat-gateway@<Digest>` aber nicht (Archiv mit anderem Index-Digest als
    im Manifest): manifest_ungueltig, nichts installiert, weder pull noch up, das laufende Bundle bleibt."""
    world = World(tmp_path)
    world.load_adds_image = False
    assert world.updater.run_once() == "abgelehnt"
    assert world.api.results == [("0.3.0", "rollback", "manifest_ungueltig")]
    assert len(world.loaded) == 1 and not any(call[0] in ("pull", "up") for call in world.compose.calls)
    assert world.compose.calls == []
    assert not world.store.exists("0.3.0") and not world.store.dir("0.3.0").exists()
    assert list(_archiv_ordner(world).iterdir()) == []
    state = world.state()
    assert (state.current, state.in_progress, state.rejected) == ("0.2.0", None, [world.abgelehnt()])


def test_docker_load_scheitert_vorlaeufig_und_das_archiv_bleibt_fuer_den_naechsten_durchlauf(tmp_path):
    """Datentraeger voll oder Daemon beschaeftigt ist kein Fehler des Angebots: `vorlaeufig`, nichts abgelehnt; das schon
    geprueft geholte Archiv bleibt liegen, der naechste Durchlauf laedt es ohne zweiten Abruf."""
    world = World(tmp_path)
    world.load_error = "no space left on device"
    assert world.updater.run_once() == "vorlaeufig"
    assert world.state().rejected == [] and world.api.results == [] and world.compose.calls == []
    assert world.store.archive_path("0.3.0").read_bytes() == ARCHIV_INHALT and not world.store.exists("0.3.0")
    world.load_error = None
    assert world.updater.run_once() == "aktualisiert"
    assert len(world.archive_fetches) == 1 and len(world.loaded) == 2  # kein zweiter Abruf, zweiter Ladeversuch
    assert list(_archiv_ordner(world).iterdir()) == []


def test_ein_liegengebliebenes_archiv_mit_falscher_pruefsumme_wird_neu_geholt(tmp_path):
    world = World(tmp_path)
    _archiv_ordner(world).mkdir()
    world.store.archive_path("0.3.0").write_bytes(b"halb")
    assert world.updater.run_once() == "aktualisiert"
    assert len(world.archive_fetches) == 1 and world.loaded == [("0.3.0.oci.tar", ARCHIV_INHALT)]


def test_ein_liegengebliebenes_archiv_wird_geloescht_wenn_das_image_schon_da_ist(tmp_path):
    world = World(tmp_path, fail_pull=True)
    world.fetch_breaks_after = None
    assert world.updater.run_once() == "vorlaeufig"  # geladen, Archiv geloescht, pull scheitert
    _archiv_ordner(world).mkdir(exist_ok=True)
    world.store.archive_path("0.3.0").write_bytes(b"Rest")
    world.compose.fail_pull = False
    assert world.updater.run_once() == "aktualisiert"
    assert len(world.archive_fetches) == 1 and list(_archiv_ordner(world).iterdir()) == []


def test_archive_frueherer_versionen_werden_vor_dem_abruf_aufgeraeumt(tmp_path):
    world = World(tmp_path)
    _archiv_ordner(world).mkdir()
    for name in ("0.1.0.oci.tar", "0.2.5.oci.tar.part", "rest"):
        (_archiv_ordner(world) / name).write_bytes(b"x" * 10)
    assert world.updater.run_once() == "aktualisiert"
    assert list(_archiv_ordner(world).iterdir()) == []


def test_ein_archiv_wird_nur_abgerufen_wenn_das_geraet_bereit_ist(tmp_path):
    """Fehlt der Zigbee-Stick, wartet das Update ('vorlaeufig'), und die rund 200 MB werden nicht umsonst geholt."""
    world = World(tmp_path)
    compose = (COMPOSE.decode() + "  zigbee2mqtt:\n    image: koenkk/z2m@sha256:" + "b" * 64 +
               "\n    devices: [\"/dev/ttyACM9:/dev/zigbee\"]\n").encode()
    world.served[BASE + "docker-compose.yml"] = compose
    manifest = json.loads(world.served[BASE + "manifest.json"])
    manifest["files"]["docker-compose.yml"] = hashlib.sha256(compose).hexdigest()
    _serve_manifest(world, json.dumps(manifest).encode())
    world.updater._path_exists = lambda path: False
    assert world.updater.run_once() == "vorlaeufig"
    assert world.archive_fetches == [] and world.events == [] and world.loaded == []
    world.updater._path_exists = lambda path: True
    assert world.updater.run_once() == "aktualisiert"


def test_compose_mit_dem_archivimage_ohne_never_wird_abgelehnt(tmp_path):
    """Auf dem Geraet gilt check_compose mit dem Archiv des Manifests: `compose pull` fragte sonst docker.io nach dem
    eigenen Image."""
    compose = f"services:\n  runtime:\n    image: {IMAGE}\n".encode()
    world = World(tmp_path, compose=compose)
    assert world.updater.run_once() == "abgelehnt"
    assert world.api.results == [("0.3.0", "rollback", "manifest_ungueltig")]
    assert world.archive_fetches == []  # abgelehnt, bevor das Archiv geholt wird
    _assert_nothing_installed(world)


def test_der_rueckweg_nach_einem_ungesunden_update_startet_die_vorige_version_mit_ihrem_image(tmp_path):
    """Image der vorigen Version bleibt (prune behaelt es), der Rueckweg braucht kein Netz und kein Laden."""
    world = World(tmp_path, healthy=False)
    assert world.updater.run_once() == "zurueckgerollt"
    assert world.compose.calls[-1] == ("up", "0.2.0")
    assert ALTES_IMAGE in world.compose.images
    assert len(world.loaded) == 1  # geladen wurde nur das neue Image
    assert not any(call[0] == "prune" for call in world.compose.calls)  # bei einem Rueckweg wird nichts geloescht


# --- fetch_url_to_file: Teilabruf, Grenzen, Frist -------------------------------------------------------------------

class _RangeServer:
    """Lokaler HTTP-Server fuer eine Datei `body` unter /archiv: antwortet auf `Range: bytes=N-` mit 206 und
    Content-Range (ranges=False: ignoriert Range und antwortet mit 200). `cut_after`: die erste Antwort bricht nach so
    vielen Bytes ab (Content-Length bleibt die volle). `length=False`: Antwort ohne Content-Length (HTTP/1.0, Ende durch
    Schliessen). `requests` haelt jeden Range-Kopf (None ohne)."""

    def __init__(self, body: bytes, *, ranges=True, cut_after=None, length=True, advertised=None, drip=0.0,
                 wrong_start=False):
        self.body, self.requests = body, []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                header = self.headers.get("Range")
                owner.requests.append(header)
                start = int(header.split("=")[1].rstrip("-")) if header and ranges else 0
                total = len(owner.body)
                if header and ranges and start >= total:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{total}")
                    self.end_headers()
                    return
                chunk = owner.body[start:]
                self.send_response(206 if header and ranges else 200)
                if header and ranges:
                    shown = 0 if wrong_start else start  # Beginn der Teilantwort passt nicht zur Anfrage
                    self.send_header("Content-Range", f"bytes {shown}-{total - 1}/{total}")
                if length:
                    self.send_header("Content-Length", str(advertised if advertised is not None else len(chunk)))
                self.end_headers()
                send = chunk[:cut_after] if cut_after is not None and len(owner.requests) == 1 else chunk
                try:
                    if drip:
                        for index in range(len(send)):
                            self.wfile.write(send[index:index + 1])
                            self.wfile.flush()
                            time.sleep(drip)
                    else:
                        self.wfile.write(send)
                except OSError:  # der Abrufer hat aufgegeben
                    return

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}/archiv"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


@pytest.fixture
def range_server():
    servers = []

    def make(body, **kwargs):
        servers.append(_RangeServer(body, **kwargs))
        return servers[-1]

    yield make
    for server in servers:
        server.close()


BODY = bytes(range(256)) * 400  # 102400 Byte


def _teil(ziel: Path) -> Path:
    return ziel.with_name(ziel.name + ".part")


def test_fetch_url_to_file_schreibt_die_datei_und_gibt_die_pruefsumme_zurueck(tmp_path, range_server):
    server = range_server(BODY)
    ziel = tmp_path / "archive" / "a.oci.tar"
    ziel.parent.mkdir()
    sha = updater.fetch_url_to_file(server.url, len(BODY), True, ziel, 30)
    assert sha == hashlib.sha256(BODY).hexdigest() and ziel.read_bytes() == BODY
    assert not _teil(ziel).exists() and server.requests == [None]  # ohne .part kein Range-Kopf


def test_fetch_url_to_file_setzt_einen_abgebrochenen_abruf_per_range_fort(tmp_path, range_server):
    """Die Leitung bricht nach 30000 Byte ab: FetchError, der Anfang bleibt als .part (kein Umbenennen), der naechste Abruf
    fragt `Range: bytes=30000-` und haengt an; die Pruefsumme gilt dem ganzen Inhalt."""
    server = range_server(BODY, cut_after=30000)
    ziel = tmp_path / "a.oci.tar"
    with pytest.raises(updater.FetchError, match="abgebrochen"):
        updater.fetch_url_to_file(server.url, len(BODY), True, ziel, 30)
    assert _teil(ziel).read_bytes() == BODY[:30000] and not ziel.exists()
    sha = updater.fetch_url_to_file(server.url, len(BODY), True, ziel, 30)
    assert server.requests == [None, "bytes=30000-"]
    assert sha == hashlib.sha256(BODY).hexdigest() and ziel.read_bytes() == BODY and not _teil(ziel).exists()


def test_fetch_url_to_file_beginnt_neu_wenn_der_server_range_ignoriert(tmp_path, range_server):
    server = range_server(BODY, ranges=False)
    ziel = tmp_path / "a.oci.tar"
    _teil(ziel).write_bytes(b"alter Anfang")
    sha = updater.fetch_url_to_file(server.url, len(BODY), True, ziel, 30)
    assert server.requests == ["bytes=12-"]  # gefragt wurde, geantwortet 200: von vorn
    assert ziel.read_bytes() == BODY and sha == hashlib.sha256(BODY).hexdigest()


def test_fetch_url_to_file_verwirft_eine_teilantwort_mit_falschem_beginn(tmp_path, range_server):
    """206, aber Content-Range beginnt nicht bei der Groesse des .part: angehaengt entstuende ein falsches Ganzes."""
    server = range_server(BODY, wrong_start=True)
    ziel = tmp_path / "a.oci.tar"
    _teil(ziel).write_bytes(BODY[:1000])
    with pytest.raises(updater.FetchError, match="beginnt nicht"):
        updater.fetch_url_to_file(server.url, len(BODY), True, ziel, 30)
    assert not _teil(ziel).exists() and not ziel.exists()


def test_fetch_url_to_file_schliesst_ein_vollstaendiges_part_ohne_erneuten_abruf_ab(tmp_path, range_server):
    """Stromausfall zwischen dem letzten Byte und dem Umbenennen: der Server antwortet 416 (nichts mehr zu liefern)."""
    server = range_server(BODY)
    ziel = tmp_path / "a.oci.tar"
    _teil(ziel).write_bytes(BODY)
    sha = updater.fetch_url_to_file(server.url, len(BODY), True, ziel, 30)
    assert server.requests == [f"bytes={len(BODY)}-"]
    assert ziel.read_bytes() == BODY and sha == hashlib.sha256(BODY).hexdigest() and not _teil(ziel).exists()


def test_fetch_url_to_file_verwirft_ein_part_das_der_server_nicht_fortsetzen_kann(tmp_path, range_server):
    """416 mit anderer Gesamtgroesse: das .part passt nicht zur Datei auf dem Server. Es wird verworfen, der Abruf scheitert
    (FetchError), der naechste Durchlauf beginnt neu."""
    server = range_server(BODY[:1000])
    ziel = tmp_path / "a.oci.tar"
    _teil(ziel).write_bytes(b"x" * 5000)
    with pytest.raises(updater.FetchError):
        updater.fetch_url_to_file(server.url, len(BODY), True, ziel, 30)
    assert not _teil(ziel).exists() and not ziel.exists()


def test_fetch_url_to_file_lehnt_ab_was_ueber_der_grenze_liegt_und_laesst_nichts_zurueck(tmp_path, range_server):
    ziel = tmp_path / "a.oci.tar"
    # laut Content-Length
    server = range_server(BODY)
    with pytest.raises(updater.TooLarge):
        updater.fetch_url_to_file(server.url, len(BODY) - 1, True, ziel, 30)
    assert not _teil(ziel).exists() and not ziel.exists()
    # ohne Content-Length: erst beim Lesen zu merken
    ohne_laenge = range_server(BODY, length=False)
    with pytest.raises(updater.TooLarge):
        updater.fetch_url_to_file(ohne_laenge.url, 50000, True, ziel, 30)
    assert not _teil(ziel).exists() and not ziel.exists()
    # ein altes .part ueber der Grenze wird verworfen, der Abruf beginnt von vorn (kein Range-Kopf)
    klein = range_server(BODY[:1500])
    _teil(ziel).write_bytes(b"x" * 2000)
    sha = updater.fetch_url_to_file(klein.url, 1500, True, ziel, 30)
    assert sha == hashlib.sha256(BODY[:1500]).hexdigest() and klein.requests == [None] and ziel.read_bytes() == BODY[:1500]


def test_fetch_url_to_file_ohne_laengenangabe_gilt_das_ende_des_stroms_als_fertig(tmp_path, range_server):
    server = range_server(BODY, length=False)
    ziel = tmp_path / "a.oci.tar"
    sha = updater.fetch_url_to_file(server.url, len(BODY), True, ziel, 30)
    assert ziel.read_bytes() == BODY and sha == hashlib.sha256(BODY).hexdigest()


def test_fetch_url_to_file_nimmt_http_nur_mit_allow_http_an(tmp_path, range_server):
    server = range_server(BODY)
    ziel = tmp_path / "a.oci.tar"
    with pytest.raises(updater.FetchError, match="https"):
        updater.fetch_url_to_file(server.url, len(BODY), False, ziel, 30)
    assert server.requests == [] and not ziel.exists() and not _teil(ziel).exists()


def test_fetch_url_to_file_frist_beendet_einen_tropfenden_abruf_und_laesst_den_anfang_liegen(tmp_path, range_server):
    """Die Frist (hier 0,6 s statt 60 s plus Groesse) gilt fuer den ganzen Abruf; was bis dahin kam, bleibt als .part."""
    server = range_server(b"x" * 100, drip=0.05)
    ziel = tmp_path / "a.oci.tar"
    started = time.monotonic()
    with pytest.raises(updater.FetchError, match="Gesamtfrist"):
        updater.fetch_url_to_file(server.url, 100, True, ziel, 0.6)
    assert time.monotonic() - started < 3
    assert 0 < _teil(ziel).stat().st_size < 100 and not ziel.exists()


def test_fetch_url_to_file_frist_ist_nicht_die_gesamtfrist_der_kleinen_dateien(tmp_path, monkeypatch, range_server):
    """ABRUF_GESAMT_SECONDS (300 s) gilt fuer Manifest und Dateien; das Archiv hat seine eigene Frist."""
    server = range_server(b"x" * 100, drip=0.01)
    monkeypatch.setattr(updater, "ABRUF_GESAMT_SECONDS", 0.2)
    ziel = tmp_path / "a.oci.tar"
    assert updater.fetch_url_to_file(server.url, 100, True, ziel, 30) == hashlib.sha256(b"x" * 100).hexdigest()
    with pytest.raises(updater.FetchError, match="Gesamtfrist"):
        updater.fetch_url(server.url, 100, allow_http=True)  # Gegenprobe: die kleine Frist wirkt weiter


def test_fetch_url_to_file_netzfehler_sind_fetch_errors_und_das_part_bleibt(tmp_path):
    ziel = tmp_path / "a.oci.tar"
    _teil(ziel).write_bytes(b"abc")
    with pytest.raises(updater.FetchError):
        updater.fetch_url_to_file("http://127.0.0.1:1/x", 10, True, ziel, 5)
    assert _teil(ziel).read_bytes() == b"abc" and not ziel.exists()
