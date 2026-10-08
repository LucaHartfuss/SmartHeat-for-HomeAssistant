"""Treiber `vicare_cloud` (Spec SHG G4 1): Viessmann-ViCare-Cloud als PlantBinding mit derselben BindingDescription wie
der HA-Pfad. Lesen kommt aus dem Cache (Abfrage alle 300 s); alle oeffentlichen Methoden sind thread-sicher ueber einen
RLock fuer Cache und Ueberlagerung, Netzaufrufe laufen ausserhalb der Sperre (Plan G4 Praezisierung 9).

Neigung und Niveau gehen ueber dasselbe Kommando setCurve: der zweite Aufruf einer Schreibgruppe nimmt den eben
geschriebenen Wert des ersten Mitglieds aus der Ueberlagerung, nie den alten Cache (Praezisierung 6). read() zeigt wie
der HA-Pfad den Stand der Cloud. In Logs, Fehlertexten und Ergebnissen stehen nie Token, Verifier oder Seriennummern
(Regel 6)."""
import logging
import threading
import time
from collections.abc import Mapping

import requests

from smartheat_core.binding import BINDINGS, with_poll_interval
from smartheat_gateway.drivers.base import CACHE_MAX_POLLS, KIND_CLOUD, LEVER_ROLES, LOGIN_OAUTH, DriverError
from smartheat_gateway.drivers.vicare_cloud import api as vicare
from smartheat_gateway.drivers.vicare_cloud import capabilities as caps
from smartheat_gateway.drivers.vicare_cloud import oauth
from smartheat_gateway.paths import Paths
from smartheat_gateway.quota import QuotaExhausted, QuotaGuard, QuotaSpec

logger = logging.getLogger(__name__)

QUOTA = QuotaSpec(limit=1450, hard_limit=1200, window_seconds=86400)
FOREIGN_PROGRAMS = ("comfort", "eco")
NOT_LOGGED_IN_TEXT = "Die Anmeldung bei Viessmann ist abgelaufen. Bitte im SmartHeat-Portal die Anmeldung erneuern."
UNREACHABLE_TEXT = "Die Viessmann-Cloud ist nicht erreichbar."
READ_REJECTED_TEXT = "Die Viessmann-Cloud hat die Abfrage abgelehnt."
QUOTA_TEXT = "Das Abfragekontingent bei Viessmann ist erschöpft."
LOGIN_TIMEOUT_TEXT = "Die Anmeldung wurde nicht rechtzeitig abgeschlossen. Bitte noch einmal anmelden."
LOGIN_INCOMPLETE_TEXT = "Die Anmeldung konnte nicht vorbereitet werden. Bitte noch einmal anmelden."
LOGIN_UNREACHABLE_TEXT = "Die Anmeldung bei Viessmann ist gerade nicht erreichbar. Bitte später noch einmal versuchen."
# Rollen, die signals() immer meldet (Pflichtrollen des Hebelsatzes ohne den Raum); die uebrigen nur, wenn vorhanden.
ALWAYS_SIGNALS = ("curve_current", "level_current", "shift_current", "mode_select", "outdoor_temp")
CURVE_LEVERS = ("curve", "level")
MIN_POLL_SECONDS = 120.0  # Abfrage + Inventur + Schreiben bleiben unter dem Tageskontingent (Audit 4, A4-34)


def _check_parameter(parameter: dict) -> None:
    """Prueft nur vorhandene Schluessel; ein leerer Parameter (Anmeldedaten vergessen) ist erlaubt."""
    poll = parameter.get("poll_seconds")
    if poll is not None and (isinstance(poll, bool) or not isinstance(poll, int | float) or poll < MIN_POLL_SECONDS):
        raise ValueError(f"poll_seconds muss mindestens {MIN_POLL_SECONDS:g} s sein")
    for key in ("installation_id", "heizkreis"):
        value = parameter.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ValueError(f"{key} muss eine nicht negative ganze Zahl sein")
    for key in ("gateway_serial", "device_id"):
        value = parameter.get(key)
        if value is not None and (not isinstance(value, str) or not value):
            raise ValueError(f"{key} muss ein nicht leerer Text sein")


class ViCareCloudDriver:
    driver_id = "vicare_cloud"
    kind = KIND_CLOUD
    login_kind = LOGIN_OAUTH
    LEVER_SETS = (caps.LEVER_SET,)
    REJECTION_REASONS = caps.REJECTIONS
    # Audit 4, A4-08: Parameter, die die Anlage bestimmen (registry.same_plant/plant_id).
    # Achtung (Audit 4, A4-08): Eine Aenderung aendert die plant_id jeder Installation; beim Update gilt das als andere Anlage und setzt den anlagenbezogenen Zustand zurueck.
    IDENTITY_PARAMETERS = ("installation_id", "gateway_serial", "device_id", "heizkreis")
    quota = QUOTA

    def __init__(self, parameter: dict, paths: Paths, *, clock=time.monotonic, writer: bool = False,
                 wall=time.time, api: vicare.ViCareApi | None = None) -> None:
        self._p = dict(parameter or {})
        _check_parameter(self._p)
        self._paths, self._clock, self._wall, self._writer = paths, clock, wall, writer
        self.poll_seconds = float(self._p.get("poll_seconds", 300.0))
        self.description = with_poll_interval(BINDINGS[caps.LEVER_SET], self.poll_seconds)
        self.physical_writes = 0
        self._tokens = oauth.TokenStore(paths.driver_secrets_dir / "vicare.json", wall=wall)
        self._guard = QuotaGuard(paths.quota_dir / "vicare_cloud.json", QUOTA, now=wall)
        # Abfrage-Thread und Worker rufen parallel: je Aufruf eine eigene Verbindung (requests.request) statt einer
        # geteilten Session; bei einem Abruf alle 300 s kostet das nichts.
        self._api = api or vicare.ViCareApi(self._tokens, self._guard, wall=wall, session=requests)
        self._lock = threading.RLock()
        self._cache: dict[str, float | str] = {}
        self._cache_at: float | None = None
        self._cache_started: float | None = None  # Beginn des Abrufs, der den Cache gefuellt hat
        self._limits: dict[str, tuple[float, float] | None] = {}
        self._overlay: dict[str, float] = {}      # eigene Schreibwerte, bis die Cloud sie zeigt (Praezisierung 6)
        self._overlay_at: dict[str, float] = {}
        # Eigener Programmwechsel (prepare/restore_aux) mit Zeitpunkt: gilt ueber jedem neuen Cache, bis ein Abruf ihn
        # bestaetigt oder settle_seconds vergangen sind; sonst wuerde eine nachlaufende Cloud das Ursprungsprogramm
        # des Kunden verlieren oder ueberschreiben (Fix-Runde 1 Task 6).
        self._mode_overlay: tuple[str, float] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- Hilfen ---

    @property
    def _n(self) -> int:
        return int(self._p.get("heizkreis", 0))

    def _ids(self) -> tuple[int, str, str]:
        return int(self._p["installation_id"]), str(self._p["gateway_serial"]), str(self._p.get("device_id", "0"))

    @staticmethod
    def _fail(error: vicare.ViCareError) -> Exception:
        """ViCare-Fehler -> Vertragsgrund (agent/wire.py) bzw. QuotaExhausted (der Agent meldet kontingent_erschoepft,
        die Laufzeit behandelt es als Kontingentfehler). Ein abgelehntes Kommando ist ein gewoehnlicher Schreibfehler."""
        if isinstance(error, vicare.NotAuthenticated):
            return DriverError("nicht_angemeldet", NOT_LOGGED_IN_TEXT)
        if isinstance(error, vicare.RateLimited):
            return QuotaExhausted(QUOTA_TEXT)
        if isinstance(error, vicare.CommandRejected):
            return RuntimeError(error.text)
        if isinstance(error, vicare.ReadFailed):
            return DriverError("anlage_nicht_erreichbar", READ_REJECTED_TEXT)
        return DriverError("anlage_nicht_erreichbar", UNREACHABLE_TEXT)  # Unreachable und Unbekanntes

    def _execute(self, feature: str, command: str, params: dict) -> None:
        try:
            self._api.execute(*self._ids(), feature, command, params)
        except vicare.ViCareError as error:
            raise self._fail(error) from error

    def _program(self, name: str) -> str:
        return f"heating.circuits.{self._n}.operating.programs.{name}"

    # --- Abfrage ---

    def poll_once(self) -> None:
        started = self._clock()
        try:
            features = self._api.features(*self._ids())  # Netz ausserhalb der Sperre
        except (vicare.ViCareError, QuotaExhausted) as error:
            logger.warning("vicare_cloud: Abfrage fehlgeschlagen (%s), der Cache altert", type(error).__name__)
            return
        cache, limits = self._extract(features)
        with self._lock:
            if self._cache_started is not None and started < self._cache_started:
                return  # ein spaeter begonnener Abruf hat den Cache schon gefuellt
            self._cache, self._limits = cache, limits
            self._cache_at = self._cache_started = started
            now = self._clock()
            for lever, value in list(self._overlay.items()):
                shown = cache.get(LEVER_ROLES[lever])
                confirmed = isinstance(shown, float) and abs(shown - value) <= self.description.enforce_tolerance[lever]
                if confirmed or now - self._overlay_at[lever] >= self.description.settle_seconds:
                    del self._overlay[lever], self._overlay_at[lever]
            self._apply_mode_overlay(now)

    @staticmethod
    def _mode_confirmed(shown: float | str | None, wanted: str) -> bool:
        """Ein Abruf bestaetigt den eigenen Programmwechsel: dasselbe Programm, oder nach prepare() (wanted kein
        Fremdprogramm) irgendein Programm ausser comfort/eco (die Cloud kann z. B. reduced aus dem Zeitprogramm zeigen)."""
        if shown == wanted:
            return True
        return wanted not in FOREIGN_PROGRAMS and isinstance(shown, str) and shown not in FOREIGN_PROGRAMS

    def _apply_mode_overlay(self, now: float) -> None:
        """Unter der Sperre: Programm-Ueberlagerung auf den eben gebauten Cache legen oder beenden."""
        if self._mode_overlay is None:
            return
        wanted, since = self._mode_overlay
        if self._mode_confirmed(self._cache.get("mode_select"), wanted) or now - since >= self.description.settle_seconds:
            self._mode_overlay = None
        else:
            self._cache["mode_select"] = wanted

    def _set_mode(self, program: str) -> None:
        with self._lock:
            self.physical_writes += 1
            self._mode_overlay = (program, self._clock())
            self._cache["mode_select"] = program

    def _extract(self, features) -> tuple[dict[str, float | str], dict[str, tuple[float, float] | None]]:
        """Cache und Wertebereiche aus einer Feature-Liste, jedes Mal neu aufgebaut (nie mit dem alten zusammengefuehrt):
        fehlt ein Feature nach einem Firmware-Update, fehlt die Rolle, und read() wirft (Review Focus 5)."""
        index, n = caps.by_name(features), self._n
        curve, normal = index.get(caps.CURVE.format(n=n)), index.get(caps.NORMAL.format(n=n))
        cache: dict[str, float | str] = {}
        for role, number in (("curve_current", caps.number_value(curve, "slope")),
                             ("level_current", caps.number_value(curve, "shift")),
                             ("shift_current", caps.number_value(normal, "temperature"))):
            if number is not None:
                cache[role] = number
        active = caps.value(index.get(caps.ACTIVE.format(n=n)), "value")
        cache["mode_select"] = active if isinstance(active, str) and active else "normal"
        for role, (template, prop) in caps.SIGNAL_FEATURES.items():
            number = caps.number_value(index.get(template.format(n=n)), prop)
            if number is not None:
                cache[role] = number
        limits: dict[str, tuple[float, float] | None] = {}
        for lever, found in caps.lever_ranges(index, n).items():
            limits[lever] = (found[0], found[1]) if found is not None and found[0] < found[1] else None
        return cache, limits

    def _stale(self) -> bool:
        return self._cache_at is None or self._clock() - self._cache_at > CACHE_MAX_POLLS * self.poll_seconds

    def _fresh(self) -> dict[str, float | str]:
        """Kopie des Caches; veraltet -> QuotaExhausted (Kontingent erschoepft), DriverError nicht_angemeldet
        (Anmeldung abgelaufen) oder ValueError (Datenfehler der Laufzeit)."""
        with self._lock:
            if not self._stale():
                return dict(self._cache)
        if self._guard.exhausted():
            raise QuotaExhausted(QUOTA_TEXT)
        if self._tokens.expired() or not self._tokens.logged_in():
            raise DriverError("nicht_angemeldet", NOT_LOGGED_IN_TEXT)
        raise ValueError("Treiber-Cache veraltet")

    @staticmethod
    def _role(cache: Mapping[str, float | str], role: str) -> float | str:
        if role not in cache:
            raise ValueError(f"Feature fehlt: {role}")
        return cache[role]

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="vicare-poll", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self.poll_once()
            except Exception as error:
                logger.warning("vicare_cloud: Abfrage abgebrochen (%s)", type(error).__name__)

    # --- PlantBinding ---

    def has(self, lever: str) -> bool:
        return lever in self.description.lever_set.levers

    def ref(self, lever: str) -> str:
        return f"treiber:{LEVER_ROLES[lever]}"

    def read(self, lever: str) -> float | None:
        return float(self._role(self._fresh(), LEVER_ROLES[lever]))

    def read_or(self, lever: str, fallback: float | None) -> float | None:
        try:
            return self.read(lever)
        except Exception as error:
            logger.warning("%s nicht lesbar (%s), verwende %s", lever, type(error).__name__, fallback)
            return fallback

    def write(self, lever: str, value: float) -> None:
        cache = self._fresh()  # QuotaExhausted / nicht_angemeldet, bevor ein Aufruf entsteht
        if lever in CURVE_LEVERS:
            with self._lock:
                overlay = dict(self._overlay)
            current = {member: float(value) if member == lever else
                       overlay[member] if member in overlay else float(self._role(cache, LEVER_ROLES[member]))
                       for member in CURVE_LEVERS}
            sent = {"curve": round(current["curve"], 1), "level": int(round(current["level"]))}
            self._execute(caps.CURVE.format(n=self._n), "setCurve", {"shift": sent["level"], "slope": sent["curve"]})
            written = float(sent[lever])
        elif lever == "room_setpoint":
            written = float(value)
            self._execute(caps.NORMAL.format(n=self._n), "setTemperature", {"targetTemperature": written})
        else:
            raise ValueError(f"Hebel {lever} gibt es bei vicare_cloud nicht")
        with self._lock:
            self._overlay[lever], self._overlay_at[lever] = written, self._clock()
            self.physical_writes += 1

    def write_group(self, values: Mapping[str, float]) -> None:
        """Steigung und Niveau in EINEM setCurve (Audit 4, A4-31): keine Zwischenstellung mit dem alten Partnerwert,
        ein Aufruf aus dem Kontingent. Die Pipeline nutzt diese optionale Methode fuer die Schreibgruppe."""
        if set(values) != set(CURVE_LEVERS):
            raise ValueError(f"Schreibgruppe {sorted(values)} ist nicht {list(CURVE_LEVERS)}")
        self._fresh()  # QuotaExhausted / nicht_angemeldet, bevor ein Aufruf entsteht
        sent = {"curve": round(float(values["curve"]), 1), "level": int(round(float(values["level"])))}
        self._execute(caps.CURVE.format(n=self._n), "setCurve", {"shift": sent["level"], "slope": sent["curve"]})
        with self._lock:
            for lever in CURVE_LEVERS:
                self._overlay[lever], self._overlay_at[lever] = float(sent[lever]), self._clock()
            self.physical_writes += 1

    def needs_preparation(self) -> bool:
        return True

    def is_prepared(self) -> bool:
        return self._fresh()["mode_select"] not in FOREIGN_PROGRAMS

    def prepare(self) -> bool:
        """Ein aktives Komfort- oder Eco-Programm verlassen, damit das Normalprogramm (room_setpoint) wirkt."""
        program = self._fresh()["mode_select"]
        if program not in FOREIGN_PROGRAMS:
            return False
        self._execute(self._program(str(program)), "deactivate", {})
        self._set_mode("normal")
        return True

    def read_aux(self) -> dict[str, str | float]:
        return {"mode_select": str(self._fresh()["mode_select"])}

    def restore_aux(self, values: Mapping[str, str | float], levers: Mapping[str, float] | None = None) -> None:
        program = values.get("mode_select")
        if program not in FOREIGN_PROGRAMS or self._fresh()["mode_select"] == program:
            return
        self._execute(self._program(str(program)), "activate", {})
        self._set_mode(str(program))

    def limits(self, lever: str) -> tuple[float, float] | None:
        """Wertebereich der Anlage aus den Kommando-Constraints im Cache (Praezisierung 13); ohne Constraint, mit
        min >= max oder bei veraltetem Cache None (die Pipeline nimmt dann die lokalen Grenzen). Wirft nie."""
        with self._lock:
            if self._stale():
                return None
            return self._limits.get(lever)

    # --- Treiber ---

    def signals(self) -> Mapping[str, str]:
        with self._lock:
            present = set(self._cache)
        roles = list(ALWAYS_SIGNALS) + [role for role in caps.SIGNAL_FEATURES if role in present
                                        and role not in ALWAYS_SIGNALS]
        return {role: f"treiber:{role}" for role in roles}

    def read_signal(self, role: str) -> float | str:
        return self._role(self._fresh(), role)

    def login_begin(self, params: dict) -> dict:
        client_id, redirect_uri = params.get("client_id"), params.get("redirect_uri")
        if not (isinstance(client_id, str) and client_id and isinstance(redirect_uri, str) and redirect_uri):
            raise DriverError("login_fehlgeschlagen", LOGIN_INCOMPLETE_TEXT)
        challenge = self._tokens.begin(client_id, redirect_uri)
        return {"code_challenge": challenge, "code_challenge_method": "S256"}  # nie Verifier oder Token (done.json)

    def login_finish(self, params: dict) -> None:
        code, redirect_uri = params.get("code"), params.get("redirect_uri")
        if not (isinstance(code, str) and code and isinstance(redirect_uri, str) and redirect_uri):
            raise DriverError("login_fehlgeschlagen", LOGIN_INCOMPLETE_TEXT)
        try:
            self._tokens.finish(code, redirect_uri)
        except oauth.LoginRejected as error:
            raise DriverError("login_fehlgeschlagen", error.text) from error
        except oauth.NotLoggedIn as error:
            raise DriverError("login_fehlgeschlagen", LOGIN_TIMEOUT_TEXT) from error
        except oauth.TokenUnavailable as error:
            raise DriverError("anlage_nicht_erreichbar", LOGIN_UNREACHABLE_TEXT) from error

    def _heating_devices(self) -> list[dict]:
        try:
            return [d for d in self._api.installations() if d.get("device_type") == "heating"]
        except vicare.ViCareError as error:
            raise self._fail(error) from error

    def _features(self, installation_id, gateway_serial: str, device_id: str) -> list[dict]:
        try:
            return self._api.features(installation_id, gateway_serial, device_id)
        except vicare.ViCareError as error:
            raise self._fail(error) from error

    def probe(self) -> dict:
        found = []
        for device in self._heating_devices():
            features = self._features(device["installation_id"], device["gateway_serial"], device["device_id"])
            found += caps.candidates(features, installation_id=device["installation_id"],
                                     gateway_serial=device["gateway_serial"], device_id=device["device_id"],
                                     poll_seconds=self.poll_seconds)
        return {"driver_id": self.driver_id, "kandidaten": found}

    def inventory_sample(self) -> dict:
        """Eine Probe fuer die Inventur des Agenten (Praezisierung 7): nur Zahlenwerte der Rollen, keine Seriennummern,
        IDs oder Modellnamen."""
        if "installation_id" in self._p and "gateway_serial" in self._p:
            ids = self._ids()
        else:
            devices = self._heating_devices()
            if not devices:
                raise DriverError("anlage_nicht_erreichbar", "Viessmann meldet kein Heizgerät.")
            ids = (devices[0]["installation_id"], devices[0]["gateway_serial"], devices[0]["device_id"])
        cache, _ = self._extract(self._features(*ids))
        values = {role: value for role, value in cache.items() if isinstance(value, float)}
        return {"ts": self._wall(), "werte": values}

    def inventory(self, hours: int, samples: list[dict]) -> dict:
        return {"driver_id": self.driver_id, "stunden": hours, "proben": samples, "anlage": {}}

    def forget_credentials(self) -> None:
        self._tokens.forget()
