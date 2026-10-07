"""Updater des Gateways (Spec G2b-1 4, G2 8.4): fragt alle 15 min die Soll-Version ab, laedt und prueft Manifest
(https, SHA-256, minisign gegen release.pub, Version, min_updater_version) und Bundle-Dateien, schaltet per Compose um,
wartet bis zu 10 min auf "gesund" und rollt sonst auf das vorige Bundle zurueck. Nur streng neuere Versionen werden
geladen (Downgrade-Schutz, Grund version_zu_alt; der Betreiber gibt fuer einen Rueckweg der Flotte den alten Stand als
neue Version heraus). Fristen ueber die monotone Uhr (die Wanduhr springt beim Boot).
Zustand in updater/state.json (atomar, 0644), Format:

    {"current": str|null, "previous": str|null, "rejected": [str],
     "rejected_context": {"updater": str, "key": str}|null,
     "in_progress": {"version": str, "previous": str|null, "since": float, "rollback": str (optional),
                     "reject": false (optional)}|null,
     "pending_reports": [{"version": str, "result": str, "reason": str}]}

in_progress wird vor `compose up` geschrieben und uebersteht einen Stromausfall: recover() startet das Bundle dann
erneut (up ist idempotent) und wartet auf Gesundheit. Ist "rollback" gesetzt (Grund), scheiterte der Rueckweg auf
`previous`; recover()/run_once wiederholen ihn, bevor sonst etwas geschieht, und melden das Ergebnis erst danach
("reject": false = die Version kommt danach nicht nach rejected, siehe unten).
rejected gilt nur im Kontext rejected_context (Version des Updaters und Schluessel-ID aus release.pub oder
"placeholder"): aendert sich eines von beiden, wird rejected geleert (ein neuer Updater oder der echte Schluessel
darf es neu versuchen).

Umgebung ist kein Fehler des Bundles (Final-Review FW-1/FW-2):
- Die Gesundheitsfrist ruht, solange der Host die Geraete-API nicht erreicht (je Takt ein signiertes `desired`;
  DeviceApiError inkl. NotAuthenticated, z. B. Uhr nach dem Stromausfall noch nicht synchron, gilt als nicht
  erreichbar) - nach einem Stromausfall im ganzen Haus kommt der Router oft erst nach dem Pi. Hoechstens
  HEALTH_CAP_SECONDS insgesamt; ist der Server dann noch immer nicht erreichbar, geht es zurueck auf das vorige Bundle,
  ohne die Version abzulehnen (sie wird erneut versucht, solange der Server sie will), gemeldet als rollback/ungesund.
- Fehlt ein Geraetepfad aus `devices:` des Ziel-Bundles (Zigbee-Stick abgezogen), wird nicht umgeschaltet
  ("vorlaeufig"); ein unterbrochenes Update wartet in recover() ebenso, statt zurueckzurollen.
- update_result geht ueber die Warteschlange pending_reports: erst mit dem Zustand gespeichert, dann gesendet; was nicht
  durchgeht (offline), wird zu Beginn des naechsten Durchlaufs vor allem anderen erneut gesendet, in Reihenfolge
  (mindestens einmal: ein Stromausfall zwischen Senden und Speichern sendet doppelt)."""
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

REASONS = ("manifest_ungueltig", "signatur_ungueltig", "updater_zu_alt", "version_zu_alt",
           "start_fehlgeschlagen", "ungesund")
HEALTH_SECONDS = 600
HEALTH_POLL_SECONDS = 10
HEALTH_CAP_SECONDS = 3600  # Gesamtgrenze der Gesundheitspruefung, wenn die Frist wegen Unerreichbarkeit ruht
PERIOD_SECONDS = 900
JITTER_SECONDS = 300
FIRST_DELAY_SECONDS = 120
MAX_MANIFEST_BYTES = 65536
MAX_FILE_BYTES = 1048576
MAX_REJECTED = 20
MAX_PENDING_REPORTS = 20
OFFER_KEYS = ("manifest_url", "manifest_sha256", "signature")
_HEALTHY, _UNHEALTHY, _UNREACHABLE = "gesund", "ungesund", "unerreichbar"
REQUIRED_HEALTHY = ("agent", "runtime")


class FetchError(Exception):
    pass


class _Refused(Exception):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


def _text_or_none(value) -> str | None:
    return value if isinstance(value, str) else None


def _report_entries(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    keys = ("version", "result", "reason")
    return [{key: item[key] for key in keys} for item in value
            if isinstance(item, dict) and all(isinstance(item.get(key), str) for key in keys)]


@dataclass
class State:
    current: str | None = None
    previous: str | None = None
    rejected: list[str] = field(default_factory=list)
    in_progress: dict | None = None
    rejected_context: dict | None = None
    pending_reports: list[dict] = field(default_factory=list)

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
                       context, _report_entries(data.get("pending_reports")))
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
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as error:
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


def _older_or_equal(version: str, current: str | None) -> bool:
    """Downgrade-Schutz (Plan G2b-2 Task 5): nur streng neuere Soll-Versionen. Der eigene Rueckweg (_rollback auf
    previous) ist davon unberuehrt; unlesbare Versionen entscheidet die Manifest-Pruefung."""
    if current is None:
        return False
    try:
        return bundles.parse_version(version) <= bundles.parse_version(current)
    except bundles.ManifestError:
        return False


class Updater:
    def __init__(self, *, api_factory: Callable[[], object], store: bundles.BundleStore, compose,
                 fetch: Callable[[str, int], bytes], public_key_text: str, state_path: Path, version: str,
                 health: Callable[[str], bool], clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, allow_http: bool = False,
                 health_seconds: float = HEALTH_SECONDS, path_exists: Callable[[str], bool] = os.path.exists) -> None:
        self._api_factory, self._store, self._compose, self._fetch = api_factory, store, compose, fetch
        self._public_key_text, self._state_path, self._version = public_key_text, state_path, version
        self._health, self._clock, self._sleep, self._allow_http = health, clock, sleep, allow_http
        self._health_seconds, self._path_exists = health_seconds, path_exists
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
        self._flush(state)  # offene Meldungen vor allen neuen
        progress = state.in_progress
        if not progress:
            return
        version = progress["version"]
        if isinstance(progress.get("rollback"), str):
            logger.warning("Rueckweg von %s war nicht abgeschlossen, wiederhole ihn", version)
            self._rollback(state, version, progress["rollback"], reject=progress.get("reject") is not False)
            return
        if self._store.exists(version):
            missing = self._missing_devices((self._store.dir(version) / "docker-compose.yml").read_text())
            if missing:
                logger.warning("Unterbrochenes Update auf %s wartet: Geraet fehlt (%s)", version, ", ".join(missing))
                return
        logger.warning("Unterbrochenes Update auf %s gefunden, starte es erneut und pruefe Gesundheit", version)
        try:
            self._compose.up(version)
        except bundles.ComposeError as error:
            logger.error("Start von %s gescheitert: %s", version, error)
            self._rollback(state, version, "start_fehlgeschlagen")
            return
        self._settle(state, version)

    def run_once(self) -> str:
        if State.load(self._state_path).in_progress:  # offener Auftrag (z. B. gescheiterter Rueckweg) zuerst
            self.recover()  # sendet zuerst die offenen Meldungen
            if State.load(self._state_path).in_progress:
                return "vorlaeufig"
        else:
            self._flush(self._load())  # offene Meldungen vor allem anderen
        state = self._load()
        try:
            desired = self._api_factory().desired()  # type: ignore[attr-defined]
        except device_api.DeviceApiError as error:  # auch NotAuthenticated (401): vorlaeufig, nie ein Grund zum Ablehnen
            logger.info("Soll-Version nicht abrufbar (%s), naechster Durchlauf", error)
            return "vorlaeufig"
        version = desired.get("version")
        if not isinstance(version, str) or version == state.current or version in state.rejected:
            return "aktuell"
        if not any(desired.get(key) for key in OFFER_KEYS):  # nur eine Version, kein Bundle angeboten
            return "aktuell"
        if _older_or_equal(version, state.current):
            logger.error("Soll-Version %s ist nicht neuer als %s, abgelehnt (Downgrade-Schutz)", version, state.current)
            self._reject(state, version, "version_zu_alt")
            return "abgelehnt"
        try:
            files = self._download(desired, version)
        except FetchError as error:
            logger.warning("Bundle %s nicht ladbar (%s), naechster Durchlauf", version, error)
            return "vorlaeufig"
        except _Refused as refused:
            logger.error("Bundle %s abgelehnt: %s", version, refused)
            self._reject(state, version, refused.reason)
            return "abgelehnt"
        missing = self._missing_devices(files["docker-compose.yml"].decode("utf-8"))
        if missing:  # z. B. Zigbee-Stick abgezogen: up und Rueckweg scheiterten beide, kein Fehler des Bundles
            logger.warning("Bundle %s wartet: Geraet fehlt (%s), naechster Durchlauf", version, ", ".join(missing))
            return "vorlaeufig"
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
            return "zurueckgerollt" if self._rollback(state, version, "start_fehlgeschlagen") else "vorlaeufig"
        return self._settle(state, version)

    def _settle(self, state: State, version: str) -> str:
        """Auf Gesundheit warten, dann abschliessen oder zurueckrollen (ohne Ablehnung, wenn nur der Server fehlte)."""
        outcome = self._wait_healthy(version)
        if outcome == _HEALTHY:
            self._finish(state, version)
            return "aktualisiert"
        if outcome == _UNREACHABLE:
            logger.error("Server nach %d s nicht erreichbar, %s wird spaeter erneut versucht", HEALTH_CAP_SECONDS,
                         version)
        done = self._rollback(state, version, "ungesund", reject=outcome == _UNHEALTHY)
        return "zurueckgerollt" if done else "vorlaeufig"

    # --- Schritte ---

    def _download(self, desired: dict, version: str) -> dict[str, bytes]:
        """Prueft und laedt; liefert die Bundle-Dateien plus Manifest und Signatur (alles fuer bundles/<version>/)."""
        url, expected_sha, signature = (desired.get(key) for key in OFFER_KEYS)
        if not isinstance(url, str):
            raise _Refused("manifest_ungueltig", "Manifest-URL fehlt oder ist kein Text")
        try:
            parts = urllib.parse.urlsplit(url)
        except ValueError:
            raise _Refused("manifest_ungueltig", f"Manifest-URL unlesbar: {url!r}") from None
        if parts.scheme != "https" and not (self._allow_http and parts.scheme == "http"):
            raise _Refused("manifest_ungueltig", f"Manifest-URL muss https sein: {url}")
        if not parts.hostname:
            raise _Refused("manifest_ungueltig", f"Manifest-URL ohne Host: {url}")
        if not (isinstance(expected_sha, str) and len(expected_sha) == 64):
            raise _Refused("manifest_ungueltig", "manifest_sha256 fehlt oder ist kein SHA-256")
        if not isinstance(signature, str):
            raise _Refused("signatur_ungueltig", "Signatur fehlt oder ist kein Text")
        raw = self._fetch(url, MAX_MANIFEST_BYTES)
        if hashlib.sha256(raw).hexdigest() != expected_sha.lower():
            raise _Refused("manifest_ungueltig", "SHA-256 des Manifests passt nicht")
        try:
            minisign.verify(minisign.load_public_key(self._public_key_text), raw, signature)
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
        files[bundles.MANIFEST_NAME] = raw
        files[bundles.SIGNATURE_NAME] = signature.encode()
        return files

    def _missing_devices(self, compose_text: str) -> list[str]:
        return [path for path in bundles.compose_devices(compose_text) if not self._path_exists(path)]

    def _reachable(self) -> bool:
        """Erreicht der Host die Geraete-API? Signiertes desired (prueft Netz, DNS, TLS und die Uhr in einem)."""
        try:
            self._api_factory().desired()  # type: ignore[attr-defined]
        except device_api.DeviceApiError:
            return False
        return True

    def _wait_healthy(self, version: str) -> str:
        """gesund | ungesund (Frist abgelaufen) | unerreichbar (Gesamtgrenze erreicht, Server weiter nicht erreichbar).
        Ein Takt, in dem der Server nicht erreichbar war, verschiebt die Frist um seine Dauer."""
        start = last = self._clock()
        deadline = start + self._health_seconds
        cap = start + max(HEALTH_CAP_SECONDS, self._health_seconds)
        while True:
            if self._health(version):
                return _HEALTHY
            reachable = self._reachable()
            now = self._clock()
            if reachable:
                if now >= deadline:
                    return _UNHEALTHY
            else:
                if now >= cap:
                    return _UNREACHABLE
                deadline = min(deadline + (now - last), cap)
            last = now
            self._sleep(HEALTH_POLL_SECONDS)

    # --- Meldungen ---

    def _queue(self, state: State, version: str, result: str, reason: str) -> None:
        """Meldung vormerken; der Aufrufer speichert den Zustand und ruft danach _flush."""
        entry = {"version": version, "result": result, "reason": reason}
        state.pending_reports = (state.pending_reports + [entry])[-MAX_PENDING_REPORTS:]

    def _flush(self, state: State) -> None:
        """Vorgemerkte Meldungen in Reihenfolge senden; beim ersten Fehler aufhoeren, der Rest bleibt fuer spaeter."""
        sent = 0
        for entry in state.pending_reports:
            try:
                self._api_factory().update_result(  # type: ignore[attr-defined]
                    entry["version"], entry["result"], entry["reason"])
            except device_api.DeviceApiError as error:
                logger.warning("update_result fuer %s nicht gemeldet (%s), naechster Durchlauf", entry["version"],
                               error)
                break
            sent += 1
        if sent:
            state.pending_reports = state.pending_reports[sent:]
            state.save(self._state_path)

    def _reject(self, state: State, version: str, reason: str) -> None:
        state.rejected = (state.rejected + [version])[-MAX_REJECTED:]
        self._queue(state, version, "rollback", reason)
        state.save(self._state_path)
        self._flush(state)

    def _finish(self, state: State, version: str) -> None:
        previous = state.in_progress.get("previous") if state.in_progress else state.current
        state.previous, state.current, state.in_progress = previous, version, None
        self._queue(state, version, "ok", "")
        state.save(self._state_path)
        keep: set[str] = set()
        for kept in (version, previous):
            if kept and self._store.exists(kept):
                keep |= set(bundles.compose_images((self._store.dir(kept) / "docker-compose.yml").read_text()))
        if keep:
            self._compose.prune(keep)
        self._flush(state)
        logger.info("Update auf %s abgeschlossen", version)

    def _rollback(self, state: State, version: str, reason: str, reject: bool = True) -> bool:
        """Zurueck auf das vorige Bundle. Scheitert `up`, bleibt in_progress (mit "rollback") erhalten und der
        naechste recover()/run_once wiederholt den Rueckweg; abgelehnt und gemeldet wird erst, wenn er gelang.
        reject=False (Server nur nicht erreichbar): gemeldet, aber nicht nach rejected."""
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
                if not reject:
                    state.in_progress["reject"] = False
                state.save(self._state_path)
                return False
        else:
            logger.error("Kein Rueckweg moeglich fuer %s (voriges Bundle: %s), laufende Container bleiben unveraendert",
                         version, previous or "keines")
        state.current, state.in_progress = previous, None
        logger.error("Update auf %s zurueckgerollt (%s)", version, reason)
        if reject:
            self._reject(state, version, reason)
        else:
            self._queue(state, version, "rollback", reason)
            state.save(self._state_path)
            self._flush(state)
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
