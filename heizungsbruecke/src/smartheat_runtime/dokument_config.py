"""Laufzeit aus dem Konfigurationsdokument: sobald ein Host smartheat_device nutzt (Gateway ab
0.6.0, HA-Add-on ab 5c), liest die Laufzeit Anlage, Hebelsatz, Verteilsystem, Raumfuehler, Tagestick und Abo aus dem
Konfigurationsdokument statt aus Optionen (je Kreis: die Funktionen nehmen die Nummer des Kreises, ohne Nummer gilt
der erste Kreis; jeder Kreis des Dokuments hat seine eigene Laufzeit); kurze Intervalle gibt es nur fuer die E2E aus der Umgebung
(SMARTHEAT_E2E_INTERVALLE). Transport und Zugang haelt der Geraetekern (eine
MQTT-Verbindung), der Abo-Status steht im Dokument (die 30-Tage-Frist bleibt lokal).
Die Anlage bindet den lokalen Zustand ueber tenant_id: eine andere Anlage beginnt mit leerem Zustand, eine
neue Konfiguration derselben Anlage nicht. Feldnamen = smartheat_device.wire.KONFIGURATION_FIELDS (die Laufzeit
importiert den Geraetekern nicht; ein Test haelt beide gleich). Die Bindung (Treiber, Thermostat) liest der Host.
Dazu ConfigError, Bindungsbeschreibung und Sicherheitswerte."""
import json
import logging
import os
from collections.abc import Callable, Mapping
from pathlib import Path

from smartheat_core import config_check, dateien, kreisansicht, rastung
from smartheat_core.binding import BindingDescription
from smartheat_core.bindungsparameter import beschreibung as _beschreibung
from smartheat_core.levers import LEVER_SETS
from smartheat_core.safety import LocalSafety, resolve_local_safety
from smartheat_runtime import entitlement
from smartheat_runtime.runtime_config import BootInfo, RuntimeConfig
from smartheat_runtime.windows import validate_daily_trigger_time

FELDER = ("anlage", "anlage_name", "weg", "kreise", "tagestick", "bindungsparameter")
#: Aendert sich eines davon, startet der Host die Laufzeit neu (software, mindest_software und Anlagenname nicht).
#: Die Kreise gehoeren dazu (Bindung, Raeume, Hebelsatz, Verteilsystem, Abo), aber ohne die gespiegelten
#: Ursprungswerte; die Bindungsparameter auch: Schritte, Toleranzen, Wartezeit und Schreibschutz kommen nur von dort.
LAUFZEIT_FELDER = ("anlage", "weg", "kreise", "tagestick", "bindungsparameter")
ABO = {"aktiv": entitlement.ACTIVE, "inaktiv": entitlement.INACTIVE}
# Lokale Checks laufen eventgetrieben; dieser Takt gilt nur noch fuer den Watchdog-Fallback
# bei getrennter WS-Verbindung (und fuer grace_check/health).
DEFAULT_LOCAL_CHECK_INTERVAL_SECONDS = 300
DEFAULT_TELEMETRY_INTERVAL_SECONDS = 300
RECONFIGURE_HINT = "bitte SmartHeat neu konfigurieren"
#: Nur fuer das Statusereignis ohne (gueltige) Konfiguration (boot_info); die Laufzeit kennt keinen Rueckfall.
_STANDARD_HEBELSATZ = "vaillant_vrc720"
E2E_INTERVALLE_ENV = "SMARTHEAT_E2E_INTERVALLE"
logger = logging.getLogger(__name__)


class ConfigError(ValueError):
    """Konfiguriert, aber ungueltig: Ruhezustand `konfigurationsfehler` statt Regelung (kein Exit, Neupruefung nach
    app.CONFIG_RECHECK_SECONDS). Die Meldung nennt das fehlende oder ungueltige Feld."""


def _hebelsatz(konfiguration, kreis: int | None = None) -> str:
    hebelsatz = kreisansicht.sicht(konfiguration, kreis).get("hebelsatz")
    if not isinstance(hebelsatz, str) or hebelsatz not in LEVER_SETS:
        raise ConfigError(f"Hebelsatz {hebelsatz!r} ist unbekannt – {RECONFIGURE_HINT}")
    return hebelsatz


def beschreibung(konfiguration, kreis: int | None = None) -> BindingDescription:
    """Wirksame Binding-Beschreibung des Kreises aus Hebelsatz und Bindungsparametern; keine Ersatztabelle."""
    hebelsatz = _hebelsatz(konfiguration, kreis)
    try:
        sicht = kreisansicht.sicht(konfiguration, kreis)
        return _beschreibung(sicht.get("weg"), hebelsatz, sicht.get("bindungsparameter"))
    except ValueError as error:
        raise ConfigError(f"Konfiguration: {error}") from None


def sicherheit(konfiguration, kreis: int | None = None) -> LocalSafety:
    """Lokale Sicherheitswerte des Kreises fuer Hebelsatz und Verteilsystem (Regel 4)."""
    hebelsatz = _hebelsatz(konfiguration, kreis)
    try:
        return resolve_local_safety(hebelsatz, kreisansicht.sicht(konfiguration, kreis).get("verteilsystem"))
    except ValueError as error:
        raise ConfigError(f"Konfiguration: {error}") from None


def eingerichtet(konfiguration) -> bool:
    return isinstance(konfiguration, Mapping) and isinstance(konfiguration.get("anlage"), str) and bool(
        konfiguration["anlage"])


def ursprungswerte(konfiguration, kreis: int | None = None) -> dict:
    """Feld ursprungswerte des Kreises (vom Server gespiegelt); {} ohne gueltiges Feld."""
    werte = kreisansicht.sicht(konfiguration, kreis).get("ursprungswerte")
    return dict(werte) if isinstance(werte, Mapping) else {}


def abo_status(konfiguration, kreis: int | None = None) -> str:
    """Abo-Status des Kreises `kreis` (`kreise[n].abo`); ohne Nummer der des ersten Kreises. Ohne Einrichtung oder
    ohne diesen Kreis unbekannt."""
    if not eingerichtet(konfiguration):
        return entitlement.UNKNOWN
    sicht = kreisansicht.erste(konfiguration) if kreis is None else kreisansicht.fuer(konfiguration, kreis)
    abo = sicht.get("abo")
    return ABO.get(abo, entitlement.UNKNOWN) if isinstance(abo, str) else entitlement.UNKNOWN


def _e2e_intervalle() -> dict:
    """Nur fuer die E2E (kein Dokumentfeld): JSON-Objekt mit pruefung_s/telemetrie_s
    in den Grenzen von config_check.INTERVALLE; sonst {} (Standardwerte), Log ohne Wert."""
    roh = os.environ.get(E2E_INTERVALLE_ENV)
    if not roh:
        return {}
    try:
        werte = json.loads(roh)
    except (ValueError, RecursionError):  # zu tief verschachtelt: Standardwerte statt Startfehler
        werte = None
    if not isinstance(werte, dict) or not set(werte) <= set(config_check.INTERVALLE) or not all(
            isinstance(werte[k], int | float) and not isinstance(werte[k], bool)
            and config_check.INTERVALLE[k][0] <= werte[k] <= config_check.INTERVALLE[k][1] for k in werte):
        logger.warning("%s ungueltig, es gelten die Standardintervalle", E2E_INTERVALLE_ENV)
        return {}
    return werte


def kreisnummer(konfiguration, kreis: int | None = None) -> int:
    """Nummer des Heizkreises `kreis` (ohne Angabe des ersten) im Dokument; ohne brauchbaren Eintrag 1 (die BootInfo
    wirft nie)."""
    nummer = kreisansicht.sicht(konfiguration, kreis).get("kreis")
    return nummer if isinstance(nummer, int) and not isinstance(nummer, bool) and nummer >= 1 else 1


def boot_info(konfiguration, *, abgemeldet: bool, client_version: str, backup_path: Path,
              failsafe_path: Path, aufraeumen_path: Path | None = None,
              alter_zustand: Path | None = None, anlage_budget_path: Path | None = None,
              kreis: int | None = None) -> BootInfo:
    """Wirft nie (wie die BootInfo der Hosts): ein unbekannter Hebelsatz faellt fuer das Statusereignis auf den
    Standard zurueck; runtime_config meldet ihn danach als Konfigurationsfehler."""
    intervalle = _e2e_intervalle()  # einmal je Aufruf lesen (und loggen)
    anlage = konfiguration["anlage"] if eingerichtet(konfiguration) else None
    hebelsatz = kreisansicht.sicht(konfiguration, kreis).get("hebelsatz")
    lever_set = LEVER_SETS.get(hebelsatz) if isinstance(hebelsatz, str) else None
    lever_set = lever_set or LEVER_SETS[_STANDARD_HEBELSATZ]
    return BootInfo(
        configured=anlage is not None and not abgemeldet, signed_off=anlage is not None and abgemeldet,
        tenant_id=anlage, lever_set=lever_set, client_version=client_version, notify_hints_off=(),
        local_check_interval=intervalle.get("pruefung_s", DEFAULT_LOCAL_CHECK_INTERVAL_SECONDS),
        backup_path=backup_path, failsafe_path=failsafe_path, kreis=kreisnummer(konfiguration, kreis),
        aufraeumen_path=aufraeumen_path, alter_zustand=alter_zustand, anlage_budget_path=anlage_budget_path,
        kreise_der_anlage=max(1, len(kreisansicht.kreise(konfiguration))),
    )


def rastung_pruefen(konfiguration, rastung_pfad: Path) -> None:
    """Zweite Linie: die Laufzeit startet bzw. setzt nur zurueck, wenn die Konfiguration zur
    Rastung passt; der Dokumentspeicher hat das schon geprueft, das hier deckt einen Fehler dort ab. Wirft ConfigError."""
    gelesen = rastung.lesen(rastung_pfad)
    if not rastung.passt(gelesen, konfiguration):
        raise ConfigError(f"Konfiguration: {rastung.text_zu(gelesen)}")


def runtime_config(konfiguration, *, rastung_pfad: Path, entitlement_path: Path,
                   abo_source: Callable[[], str], kreis: int | None = None) -> RuntimeConfig:
    """Die Laufzeitkonfiguration des Kreises `kreis` (ohne Angabe des ersten). Wirft ConfigError (der Host macht daraus
    StartFailure); config_check hat das Dokument schon geprueft."""
    if not eingerichtet(konfiguration):
        raise ConfigError("Das Gerät ist nicht eingerichtet")
    nummern = [eintrag.get("kreis") for eintrag in kreisansicht.kreise(konfiguration)]
    if not nummern:
        raise ConfigError("Konfiguration: kein Heizkreis")
    if kreis is not None and kreis not in nummern:
        raise ConfigError(f"Konfiguration: Heizkreis {kreis} fehlt")
    rastung_pruefen(konfiguration, rastung_pfad)
    hebelsatz = _hebelsatz(konfiguration, kreis)
    safety = sicherheit(konfiguration, kreis)
    try:
        daily = validate_daily_trigger_time(konfiguration.get("tagestick"))
    except ValueError as error:
        raise ConfigError(f"Konfiguration: {error}") from None
    sensors = kreisansicht.sicht(konfiguration, kreis)["raumfuehler"]  # nur die Fuehler der Referenzraeume
    if not sensors:
        raise ConfigError("Konfiguration: keine Raumfühler")
    intervalle = _e2e_intervalle()  # einmal je Aufruf lesen (und loggen)
    return RuntimeConfig(
        tenant_id=konfiguration["anlage"], kreis=kreisnummer(konfiguration, kreis), lever_set_id=hebelsatz,
        local_safety=safety, daily_trigger_time=daily,
        local_check_interval=intervalle.get("pruefung_s", DEFAULT_LOCAL_CHECK_INTERVAL_SECONDS),
        telemetry_interval=intervalle.get("telemetrie_s", DEFAULT_TELEMETRY_INTERVAL_SECONDS),
        notify_hints_off=(), room_sensor_refs=tuple(sensors), battery_refs=(), entitlement_path=entitlement_path,
        abo_source=abo_source,
    )


def laufzeit_relevant(alt, neu) -> bool:
    alt = alt if isinstance(alt, Mapping) else {}
    neu = neu if isinstance(neu, Mapping) else {}
    return any(_laufzeit_wert(alt, key) != _laufzeit_wert(neu, key) for key in LAUFZEIT_FELDER)


def laufzeit_relevant_anlage(alt, neu) -> bool:
    """Aendert sich ein anlagenweites Laufzeitfeld (alles ausser `kreise`)? Das betrifft jeden Kreis zugleich."""
    alt = alt if isinstance(alt, Mapping) else {}
    neu = neu if isinstance(neu, Mapping) else {}
    return any(alt.get(feld) != neu.get(feld) for feld in LAUFZEIT_FELDER if feld != "kreise")


def laufzeit_relevant_kreis(alt, neu, kreis: int) -> bool:
    """Wie `laufzeit_relevant`, aber nur fuer die Laufzeit des Kreises `kreis`: die anlagenweiten Felder und der Eintrag
    dieses Kreises zaehlen, die Eintraege der anderen nicht. Ein Abo, das wieder `aktiv` ist, zaehlt nicht: der Kreis
    nimmt es selbst auf (`abo_wiederaufnahme`), ohne dass der Host neu startet."""
    alt = alt if isinstance(alt, Mapping) else {}
    neu = neu if isinstance(neu, Mapping) else {}
    if laufzeit_relevant_anlage(alt, neu):
        return True
    ohne_abo = abo_status(neu, kreis) == entitlement.ACTIVE
    return _kreis_wert(alt, kreis, ohne_abo=ohne_abo) != _kreis_wert(neu, kreis, ohne_abo=ohne_abo)


def _kreis_wert(konfiguration: Mapping, kreis: int, *, ohne_abo: bool):
    """Eintrag des Kreises ohne Ursprungswerte (und ohne Abo, wenn gewuenscht); None, wenn es ihn nicht gibt."""
    weg = ("ursprungswerte", "abo") if ohne_abo else ("ursprungswerte",)
    for eintrag in kreisansicht.kreise(konfiguration):
        if eintrag.get("kreis") == kreis:
            return {k: v for k, v in eintrag.items() if k not in weg}
    return None


def abo_wiederaufnahme(alt, neu, kreis: int) -> bool:
    """Das Abo des Kreises war nicht aktiv und ist es jetzt: der Kreis verlaesst seinen Ruhezustand."""
    return (eingerichtet(neu) and abo_status(neu, kreis) == entitlement.ACTIVE
            and abo_status(alt, kreis) != entitlement.ACTIVE)


def _laufzeit_wert(konfiguration: Mapping, feld: str):
    """Wert eines Laufzeitfeldes; die Kreise ohne die gespiegelten Ursprungswerte (ihr Eintreffen startet nichts neu)."""
    wert = konfiguration.get(feld)
    if feld == "kreise" and isinstance(wert, list):
        return [{k: v for k, v in kreis.items() if k != "ursprungswerte"} if isinstance(kreis, Mapping) else kreis
                for kreis in wert]
    return wert


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


def abmeldung_aufheben(pfad: Path, anlage: str) -> bool:
    """Neue, nicht leere Konfiguration derselben Anlage: das Geraet folgt dem
    Server, die Abmelde-Markierung verfaellt. True, wenn sie entfernt wurde; ein OSError laesst sie stehen (Warnung mit
    Fehlerart, ohne Pfad)."""
    marker = dateien.lesen_json(pfad)
    if not (isinstance(marker, dict) and marker.get("anlage") == anlage):
        return False
    try:
        pfad.unlink()
    except FileNotFoundError:
        return False
    except OSError as error:
        logger.warning("Abmelde-Markierung nicht entfernt (%s)", type(error).__name__)
        return False
    return True
