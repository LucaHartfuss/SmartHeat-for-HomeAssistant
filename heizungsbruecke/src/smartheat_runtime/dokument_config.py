"""Laufzeit aus dem Konfigurationsdokument (Spec 5b 5.2, Plan D1): sobald ein Host smartheat_device nutzt (Gateway ab
0.6.0, HA-Add-on ab 5c), liest die Laufzeit Anlage, Hebelsatz, Verteilsystem, Raumfuehler, Tagestick, Abo und
Intervalle aus dem Konfigurationsdokument statt aus Optionen. Transport und Zugang haelt der Geraetekern (eine
MQTT-Verbindung), der Abo-Status steht im Dokument (Plan S1, Praezisierung abo; die 30-Tage-Frist bleibt lokal).
Die Anlage bindet den lokalen Zustand (A4-08) ueber tenant_id: eine andere Anlage beginnt mit leerem Zustand, eine
neue Konfiguration derselben Anlage nicht. Feldnamen = smartheat_device.wire.KONFIGURATION_FIELDS (die Laufzeit
importiert den Geraetekern nicht; ein Test haelt beide gleich). Die Bindung (Treiber, Thermostat) liest der Host.
Dazu ConfigError, Bindungsbeschreibung und Sicherheitswerte (bis Stufe 3 S5 in options.py)."""
from collections.abc import Callable, Mapping
from pathlib import Path

from smartheat_core import dateien, rastung
from smartheat_core.binding import BindingDescription
from smartheat_core.bindungsparameter import beschreibung as _beschreibung
from smartheat_core.levers import LEVER_SETS
from smartheat_core.safety import LocalSafety, resolve_local_safety
from smartheat_runtime import entitlement
from smartheat_runtime.runtime_config import BootInfo, RuntimeConfig
from smartheat_runtime.windows import validate_daily_trigger_time

FELDER = ("anlage", "profil", "hebelsatz", "verteilsystem", "bindung", "raumfuehler", "tagestick", "abo", "intervalle",
          "bindungsparameter")
#: Aendert sich eines davon, startet der Host die Laufzeit neu (software, mindest_software und profil nicht). Die
#: Bindungsparameter (Spec Stufe 3 S4b) gehoeren dazu: Schritte, Toleranzen, Wartezeit und Schreibschutz kommen nur von dort.
LAUFZEIT_FELDER = ("anlage", "hebelsatz", "verteilsystem", "bindung", "raumfuehler", "tagestick", "abo", "intervalle",
                   "bindungsparameter")
ABO = {"aktiv": entitlement.ACTIVE, "inaktiv": entitlement.INACTIVE}
# Lokale Checks laufen eventgetrieben; dieser Takt gilt nur noch fuer den Watchdog-Fallback
# bei getrennter WS-Verbindung (und fuer grace_check/health).
DEFAULT_LOCAL_CHECK_INTERVAL_SECONDS = 300
DEFAULT_TELEMETRY_INTERVAL_SECONDS = 300
RECONFIGURE_HINT = "bitte SmartHeat neu konfigurieren"
#: Nur fuer das Statusereignis ohne (gueltige) Konfiguration (boot_info); die Laufzeit kennt keinen Rueckfall.
_STANDARD_HEBELSATZ = "vaillant_vrc720"


class ConfigError(ValueError):
    """Konfiguriert, aber ungueltig: Ruhezustand `konfigurationsfehler` statt Regelung (kein Exit, Neupruefung nach
    app.CONFIG_RECHECK_SECONDS). Die Meldung nennt das fehlende oder ungueltige Feld."""


def _hebelsatz(konfiguration) -> str:
    hebelsatz = konfiguration.get("hebelsatz") if isinstance(konfiguration, Mapping) else None
    if not isinstance(hebelsatz, str) or hebelsatz not in LEVER_SETS:
        raise ConfigError(f"Hebelsatz {hebelsatz!r} ist unbekannt – {RECONFIGURE_HINT}")
    return hebelsatz


def beschreibung(konfiguration) -> BindingDescription:
    """Wirksame Binding-Beschreibung aus Hebelsatz und Bindungsparametern (Spec Stufe 3 S4b); keine Ersatztabelle."""
    hebelsatz = _hebelsatz(konfiguration)
    try:
        return _beschreibung(hebelsatz, konfiguration.get("bindungsparameter"))
    except ValueError as error:
        raise ConfigError(f"Konfiguration: {error}") from None


def sicherheit(konfiguration) -> LocalSafety:
    """Lokale Sicherheitswerte fuer Hebelsatz und Verteilsystem (Regel 4)."""
    hebelsatz = _hebelsatz(konfiguration)
    try:
        return resolve_local_safety(hebelsatz, konfiguration.get("verteilsystem"))
    except ValueError as error:
        raise ConfigError(f"Konfiguration: {error}") from None


def eingerichtet(konfiguration) -> bool:
    return isinstance(konfiguration, Mapping) and isinstance(konfiguration.get("anlage"), str) and bool(
        konfiguration["anlage"])


def ursprungswerte(konfiguration) -> dict:
    """Feld ursprungswerte der Konfiguration (vom Server gespiegelt, Spec Stufe 3 S2 6.1); {} ohne gueltiges Feld."""
    werte = konfiguration.get("ursprungswerte") if isinstance(konfiguration, Mapping) else None
    return dict(werte) if isinstance(werte, Mapping) else {}


def abo_status(konfiguration) -> str:
    if not eingerichtet(konfiguration):
        return entitlement.UNKNOWN
    return ABO.get(konfiguration.get("abo"), entitlement.UNKNOWN)


def _intervall(konfiguration, key: str, default: float) -> float:
    werte = konfiguration.get("intervalle") if isinstance(konfiguration, Mapping) else None
    value = werte.get(key) if isinstance(werte, Mapping) else None
    return value if isinstance(value, int | float) and not isinstance(value, bool) else default


def boot_info(konfiguration, *, abgemeldet: bool, client_version: str, backup_path: Path,
              failsafe_path: Path, aufraeumen_path: Path | None = None) -> BootInfo:
    """Wirft nie (wie die BootInfo der Hosts): ein unbekannter Hebelsatz faellt fuer das Statusereignis auf den
    Standard zurueck; runtime_config meldet ihn danach als Konfigurationsfehler."""
    anlage = konfiguration["anlage"] if eingerichtet(konfiguration) else None
    hebelsatz = konfiguration.get("hebelsatz") if isinstance(konfiguration, Mapping) else None
    lever_set = LEVER_SETS.get(hebelsatz) if isinstance(hebelsatz, str) else None
    lever_set = lever_set or LEVER_SETS[_STANDARD_HEBELSATZ]
    return BootInfo(
        configured=anlage is not None and not abgemeldet, signed_off=anlage is not None and abgemeldet,
        tenant_id=anlage, lever_set=lever_set, client_version=client_version, notify_hints_off=(),
        local_check_interval=_intervall(konfiguration, "pruefung_s", DEFAULT_LOCAL_CHECK_INTERVAL_SECONDS),
        backup_path=backup_path, failsafe_path=failsafe_path, aufraeumen_path=aufraeumen_path,
    )


def rastung_pruefen(konfiguration, rastung_pfad: Path) -> None:
    """Zweite Linie (Spec Stufe 3 S3 3.3): die Laufzeit startet bzw. setzt nur zurueck, wenn die Konfiguration zur
    Rastung passt; der Dokumentspeicher hat das schon geprueft, das hier deckt einen Fehler dort ab. Wirft ConfigError."""
    if not rastung.passt(rastung.lesen(rastung_pfad), konfiguration if isinstance(konfiguration, Mapping) else {}):
        raise ConfigError(f"Konfiguration: {rastung.TEXT}")


def runtime_config(konfiguration, *, rastung_pfad: Path, entitlement_path: Path,
                   abo_source: Callable[[], str]) -> RuntimeConfig:
    """Wirft ConfigError (der Host macht daraus StartFailure); config_check hat das Dokument schon geprueft."""
    if not eingerichtet(konfiguration):
        raise ConfigError("Das Gerät ist nicht eingerichtet")
    rastung_pruefen(konfiguration, rastung_pfad)
    hebelsatz = _hebelsatz(konfiguration)
    safety = sicherheit(konfiguration)
    try:
        daily = validate_daily_trigger_time(konfiguration.get("tagestick"))
    except ValueError as error:
        raise ConfigError(f"Konfiguration: {error}") from None
    sensors = konfiguration.get("raumfuehler")
    if not isinstance(sensors, list) or not sensors or not all(isinstance(ref, str) for ref in sensors):
        raise ConfigError("Konfiguration: keine Raumfühler")
    return RuntimeConfig(
        tenant_id=konfiguration["anlage"], lever_set_id=hebelsatz,
        local_safety=safety, daily_trigger_time=daily,
        local_check_interval=_intervall(konfiguration, "pruefung_s", DEFAULT_LOCAL_CHECK_INTERVAL_SECONDS),
        telemetry_interval=_intervall(konfiguration, "telemetrie_s", DEFAULT_TELEMETRY_INTERVAL_SECONDS),
        notify_hints_off=(), room_sensor_refs=tuple(sensors), battery_refs=(), entitlement_path=entitlement_path,
        abo_source=abo_source,
    )


def laufzeit_relevant(alt, neu) -> bool:
    alt = alt if isinstance(alt, Mapping) else {}
    neu = neu if isinstance(neu, Mapping) else {}
    return any(alt.get(key) != neu.get(key) for key in LAUFZEIT_FELDER)


def abgemeldet(pfad: Path, konfiguration) -> bool:
    """Abmelde-Markierung (Befehl sign_off) fuer die Anlage der aktuellen Konfiguration? (Gateway und HA-Add-on)"""
    marker = dateien.lesen_json(pfad)
    anlage = konfiguration.get("anlage") if isinstance(konfiguration, Mapping) else None
    return isinstance(marker, dict) and anlage is not None and marker.get("anlage") == anlage


def abmelden(pfad: Path, anlage: str) -> bool:
    """Markierung setzen (Modus 0600); True, wenn sie neu ist (dann startet die Laufzeit neu). Die Laufzeit
    importiert den Geraetekern nicht, die atomaren Datei-Helfer stehen in smartheat_core.dateien."""
    marker = dateien.lesen_json(pfad)
    if isinstance(marker, dict) and marker.get("anlage") == anlage:
        return False
    dateien.schreiben_json(pfad, {"anlage": anlage}, modus=dateien.PRIVAT)
    return True
