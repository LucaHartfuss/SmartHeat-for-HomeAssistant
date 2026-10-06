"""Updater des Gateways (Spec G2b-1 4, G2 8.4): fragt alle 15 min die Soll-Version ab, laedt und prueft Manifest
(https, SHA-256, minisign gegen release.pub, Version, min_updater_version) und Bundle-Dateien, schaltet per Compose um,
wartet bis zu 10 min auf "gesund" und rollt sonst auf das vorige Bundle zurueck. Fristen ueber die monotone Uhr (die
Wanduhr springt beim Boot). Zustand in updater/state.json (atomar, 0644), Format:

    {"current": str|null, "previous": str|null, "rejected": [str],
     "rejected_context": {"updater": str, "key": str}|null,
     "in_progress": {"version": str, "previous": str|null, "since": float, "rollback": str (optional)}|null}

in_progress wird vor `compose up` geschrieben und uebersteht einen Stromausfall: recover() startet das Bundle dann
erneut (up ist idempotent) und wartet auf Gesundheit. Ist "rollback" gesetzt (Grund), scheiterte der Rueckweg auf
`previous`; recover()/run_once wiederholen ihn, bevor sonst etwas geschieht, und melden das Ergebnis erst danach.
rejected gilt nur im Kontext rejected_context (Version des Updaters und Schluessel-ID aus release.pub oder
"placeholder"): aendert sich eines von beiden, wird rejected geleert (ein neuer Updater oder der echte Schluessel
darf es neu versuchen)."""
import hashlib
import http.client
import json
import logging
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from smartheat_gateway.files import write_json
from smartheat_gateway.version import GATEWAY_VERSION
from smartheat_host import bundles, device_api, minisign

logger = logging.getLogger(__name__)

REASONS = ("manifest_ungueltig", "signatur_ungueltig", "updater_zu_alt", "start_fehlgeschlagen", "ungesund")
HEALTH_SECONDS = 600
HEALTH_POLL_SECONDS = 10
PERIOD_SECONDS = 900
JITTER_SECONDS = 300
FIRST_DELAY_SECONDS = 120
MAX_MANIFEST_BYTES = 65536
MAX_FILE_BYTES = 1048576
MAX_REJECTED = 20
REQUIRED_HEALTHY = ("agent", "runtime")


class FetchError(Exception):
    pass


class _Refused(Exception):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


def _text_or_none(value) -> str | None:
    return value if isinstance(value, str) else None


@dataclass
class State:
    current: str | None = None
    previous: str | None = None
    rejected: list[str] = field(default_factory=list)
    in_progress: dict | None = None
    rejected_context: dict | None = None

    @classmethod
    def load(cls, path: Path) -> "State":
        """Kaputt oder unbrauchbar gilt als leer; ein in_progress ohne Versionsangabe wird verworfen."""
        try:
            data = json.loads(path.read_text())
            in_progress = data.get("in_progress")
            if not (isinstance(in_progress, dict) and isinstance(in_progress.get("version"), str)):
                in_progress = None
            rejected = [item for item in data.get("rejected") or [] if isinstance(item, str)]
            context = data.get("rejected_context")  # fehlt in aelteren Dateien
            if not (isinstance(context, dict) and all(isinstance(value, str) for value in context.values())):
                context = None
            return cls(_text_or_none(data.get("current")), _text_or_none(data.get("previous")), rejected, in_progress,
                       context)
        except (OSError, ValueError, AttributeError, TypeError):
            return cls()

    def save(self, path: Path) -> None:
        write_json(path, asdict(self))


def fetch_url(url: str, max_bytes: int, allow_http: bool) -> bytes:
    scheme = urllib.parse.urlsplit(url).scheme
    if scheme != "https" and not (allow_http and scheme == "http"):
        raise FetchError(f"nur https erlaubt: {url}")
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            data = response.read(max_bytes + 1)
    except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
        raise FetchError(f"{url}: {type(error).__name__}") from None
    if len(data) > max_bytes:
        raise FetchError(f"{url}: zu gross")
    return data


def fetch_health(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            data = json.loads(response.read(65536))
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def health_ok(data: dict | None, containers: dict[str, str]) -> bool:
    """Gesund (G2 8.4, Plan G2b-1 Praezisierung 2): Serverkontakt laut /healthz, Laufzeit nicht im Zustand "startet"
    und die Compose-Healthchecks von Agent und Laufzeit melden "healthy". Der Healthcheck der Laufzeit (Lebenszeichen
    der Konfigurationswache) unterscheidet eine ruhende ("nicht eingerichtet": Status abgeraeumt, runtime_status None)
    von einer abstuerzenden Laufzeit."""
    if not isinstance(data, dict) or data.get("server_ok") is not True or data.get("runtime_status") == "startet":
        return False
    return all(containers.get(name) == "healthy" for name in REQUIRED_HEALTHY)


def _key_identity(public_key_text: str) -> str:
    """Schluessel-ID aus release.pub (hex) oder "placeholder"; Teil des Kontexts der abgelehnten Versionen."""
    try:
        return minisign.load_public_key(public_key_text).key_id.hex()
    except minisign.SignatureError:
        return "placeholder" if minisign.PLACEHOLDER_MARKER in public_key_text else "ungueltig"


class Updater:
    def __init__(self, *, api_factory: Callable[[], object], store: bundles.BundleStore, compose,
                 fetch: Callable[[str, int], bytes], public_key_text: str, state_path: Path, version: str,
                 health: Callable[[str], bool], clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, allow_http: bool = False,
                 health_seconds: float = HEALTH_SECONDS) -> None:
        self._api_factory, self._store, self._compose, self._fetch = api_factory, store, compose, fetch
        self._public_key_text, self._state_path, self._version = public_key_text, state_path, version
        self._health, self._clock, self._sleep, self._allow_http = health, clock, sleep, allow_http
        self._health_seconds = health_seconds
        self._context = {"updater": version, "key": _key_identity(public_key_text)}

    def _load(self) -> State:
        """Zustand laden; eine Ablehnung gilt nur fuer denselben Updater und denselben Release-Schluessel."""
        state = State.load(self._state_path)
        if state.rejected_context != self._context:
            state.rejected = []
        state.rejected_context = dict(self._context)
        return state

    # --- Ablauf ---

    def recover(self) -> None:
        """Nach einem Neustart: ein unterbrochenes Update zu Ende bringen oder zuruecknehmen. Nach einem Stromausfall
        laufen eventuell noch die alten Container (restart: unless-stopped) und wirken gesund; deshalb wird das neue
        Bundle erst erneut hochgefahren (up ist idempotent), bevor die Gesundheit zaehlt."""
        state = self._load()
        progress = state.in_progress
        if not progress:
            return
        version = progress["version"]
        if isinstance(progress.get("rollback"), str):
            logger.warning("Rueckweg von %s war nicht abgeschlossen, wiederhole ihn", version)
            self._rollback(state, version, progress["rollback"])
            return
        logger.warning("Unterbrochenes Update auf %s gefunden, starte es erneut und pruefe Gesundheit", version)
        try:
            self._compose.up(version)
        except bundles.ComposeError as error:
            logger.error("Start von %s gescheitert: %s", version, error)
            self._rollback(state, version, "start_fehlgeschlagen")
            return
        if self._wait_healthy(version):
            self._finish(state, version)
        else:
            self._rollback(state, version, "ungesund")

    def run_once(self) -> str:
        if State.load(self._state_path).in_progress:  # offener Auftrag (z. B. gescheiterter Rueckweg) zuerst
            self.recover()
            if State.load(self._state_path).in_progress:
                return "vorlaeufig"
        state = self._load()
        try:
            desired = self._api_factory().desired()  # type: ignore[attr-defined]
        except device_api.DeviceApiError as error:  # auch NotAuthenticated (401): vorlaeufig, nie ein Grund zum Ablehnen
            logger.info("Soll-Version nicht abrufbar (%s), naechster Durchlauf", error)
            return "vorlaeufig"
        version = desired.get("version")
        if (not isinstance(version, str) or version == state.current or version in state.rejected
                or not all(desired.get(key) for key in ("manifest_url", "manifest_sha256", "signature"))):
            return "aktuell"
        try:
            files = self._download(desired, version)
        except FetchError as error:
            logger.warning("Bundle %s nicht ladbar (%s), naechster Durchlauf", version, error)
            return "vorlaeufig"
        except _Refused as refused:
            logger.error("Bundle %s abgelehnt: %s", version, refused)
            self._reject(state, version, refused.reason)
            return "abgelehnt"
        self._store.install(version, files)  # erst nach allen Pruefungen, atomar
        try:
            self._compose.pull(version)
        except bundles.ComposeError as error:
            logger.warning("docker compose pull fuer %s gescheitert (%s), naechster Durchlauf", version, error)
            return "vorlaeufig"
        state.in_progress = {"version": version, "previous": state.current, "since": time.time()}
        state.save(self._state_path)  # vor dem Umschalten: ein Stromausfall findet den Auftrag wieder
        try:
            self._compose.up(version)
        except bundles.ComposeError as error:
            logger.error("Start von %s gescheitert: %s", version, error)
            return self._rolled_back(state, version, "start_fehlgeschlagen")
        if self._wait_healthy(version):
            self._finish(state, version)
            return "aktualisiert"
        return self._rolled_back(state, version, "ungesund")

    # --- Schritte ---

    def _download(self, desired: dict, version: str) -> dict[str, bytes]:
        url = desired["manifest_url"]
        scheme = urllib.parse.urlsplit(url).scheme
        if scheme != "https" and not (self._allow_http and scheme == "http"):
            raise _Refused("manifest_ungueltig", f"Manifest-URL muss https sein: {url}")
        raw = self._fetch(url, MAX_MANIFEST_BYTES)
        if hashlib.sha256(raw).hexdigest() != desired["manifest_sha256"]:
            raise _Refused("manifest_ungueltig", "SHA-256 des Manifests passt nicht")
        try:
            minisign.verify(minisign.load_public_key(self._public_key_text), raw, desired["signature"])
        except minisign.SignatureError as error:
            raise _Refused("signatur_ungueltig", str(error)) from None
        try:
            manifest = bundles.parse_manifest(raw)
        except bundles.ManifestError as error:
            raise _Refused("manifest_ungueltig", str(error)) from None
        if manifest.version != version:
            raise _Refused("manifest_ungueltig", f"Manifest nennt {manifest.version}, Soll ist {version}")
        if bundles.parse_version(manifest.min_updater_version) > bundles.parse_version(self._version):
            raise _Refused("updater_zu_alt", f"Bundle verlangt Updater {manifest.min_updater_version}")
        files = {}
        for name, sha in manifest.files.items():
            data = self._fetch(urllib.parse.urljoin(url, name), MAX_FILE_BYTES)
            if hashlib.sha256(data).hexdigest() != sha:
                raise _Refused("manifest_ungueltig", f"SHA-256 von {name} passt nicht")
            files[name] = data
        try:
            bundles.check_compose(files["docker-compose.yml"].decode("utf-8"))
        except (bundles.ManifestError, UnicodeDecodeError) as error:
            raise _Refused("manifest_ungueltig", str(error)) from None
        return files

    def _wait_healthy(self, version: str) -> bool:
        deadline = self._clock() + self._health_seconds
        while True:
            if self._health(version):
                return True
            if self._clock() >= deadline:
                return False
            self._sleep(HEALTH_POLL_SECONDS)

    def _report(self, version: str, result: str, reason: str) -> None:
        try:
            self._api_factory().update_result(version, result, reason)  # type: ignore[attr-defined]
        except device_api.DeviceApiError as error:
            logger.warning("update_result fuer %s nicht gemeldet: %s", version, error)

    def _reject(self, state: State, version: str, reason: str) -> None:
        state.rejected = (state.rejected + [version])[-MAX_REJECTED:]
        state.save(self._state_path)
        self._report(version, "rollback", reason)

    def _finish(self, state: State, version: str) -> None:
        previous = state.in_progress.get("previous") if state.in_progress else state.current
        state.previous, state.current, state.in_progress = previous, version, None
        state.save(self._state_path)
        keep: set[str] = set()
        for kept in (version, previous):
            if kept and self._store.exists(kept):
                keep |= set(bundles.compose_images((self._store.dir(kept) / "docker-compose.yml").read_text()))
        if keep:
            self._compose.prune(keep)
        self._report(version, "ok", "")
        logger.info("Update auf %s abgeschlossen", version)

    def _rolled_back(self, state: State, version: str, reason: str) -> str:
        return "zurueckgerollt" if self._rollback(state, version, reason) else "vorlaeufig"

    def _rollback(self, state: State, version: str, reason: str) -> bool:
        """Zurueck auf das vorige Bundle. Scheitert `up`, bleibt in_progress (mit "rollback") erhalten und der
        naechste recover()/run_once wiederholt den Rueckweg; abgelehnt und gemeldet wird erst, wenn er gelang."""
        progress = state.in_progress or {}
        previous = progress.get("previous") if state.in_progress else state.current
        if previous and self._store.exists(previous):
            try:
                self._compose.up(previous)
            except bundles.ComposeError as error:
                logger.error("Rueckweg auf %s gescheitert (%s), wird wiederholt", previous, error)
                since = progress.get("since")
                state.in_progress = {"version": version, "previous": previous,
                                     "since": since if isinstance(since, float) else time.time(), "rollback": reason}
                state.save(self._state_path)
                return False
        else:
            logger.error("Kein Rueckweg moeglich fuer %s (voriges Bundle: %s), laufende Container bleiben unveraendert",
                         version, previous or "keines")
        state.current, state.in_progress = previous, None
        logger.error("Update auf %s zurueckgerollt (%s)", version, reason)
        self._reject(state, version, reason)
        return True


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO)
    args = sys.argv[1:] if argv is None else argv
    root = Path(os.environ.get("SHG_ROOT", "/var/lib/smartheat"))
    allow_http = os.environ.get("SHG_UPDATER_ALLOW_HTTP") == "1"
    health_url = os.environ.get("SHG_UPDATER_HEALTH_URL", "http://127.0.0.1/healthz")
    base_url = os.environ["SHG_DEVICE_API_URL"]
    public_key_text = Path(os.environ.get("SHG_RELEASE_PUB", "/opt/smartheat/host/release.pub")).read_text()

    def api_factory():
        # Je Durchlauf neu: der Geraeteschluessel entsteht erst, wenn der Agent laeuft (fehlt er: DeviceApiError).
        return device_api.DeviceApi(base_url, device_api.load_device_key(root / "data" / "device"))

    store = bundles.BundleStore(root)
    compose = bundles.ComposeRunner(root, project=os.environ.get("SHG_COMPOSE_PROJECT", "smartheat"))
    run = Updater(
        api_factory=api_factory, store=store, compose=compose,
        fetch=lambda url, limit: fetch_url(url, limit, allow_http), public_key_text=public_key_text,
        state_path=root / "updater" / "state.json", version=GATEWAY_VERSION,
        health=lambda version: health_ok(fetch_health(health_url), compose.health(version)), allow_http=allow_http,
        health_seconds=float(os.environ.get("SHG_UPDATER_HEALTH_SECONDS", HEALTH_SECONDS)),
    )
    run.recover()
    if "--once" in args:
        print(run.run_once())
        return
    time.sleep(FIRST_DELAY_SECONDS)
    while True:
        logger.info("Durchlauf: %s", run.run_once())
        time.sleep(PERIOD_SECONDS + random.uniform(0, JITTER_SECONDS))


if __name__ == "__main__":
    main()
