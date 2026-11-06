"""Hilfs-Entities der Bruecke. Der Raumtemperatur-Template-Sensor eines Kreises ist die Rolle room_actual (Mittel
aller gueltigen Fuehler der Referenzraeume). Dazu gibt es je Kreis und Raum mit Fuehlern einen Template-Sensor mit dem
Mittel der Fuehler dieses Raums (nur Fuehler aus dem Dokument). Die Aussentemperatur kommt aus einem Sensor und braucht
keine Hilfs-Entity. Der Server bildet alle Mittelwerte aus der Telemetrie.

Namen und Tracking-Schluessel: Kreis 1 behaelt `SmartHeat <anlage> Raumtemperatur` und `room_temperature` (die Entity-ID
einer bestehenden Anlage bleibt); jeder weitere Kreis n heisst `SmartHeat <anlage> K<n> Raumtemperatur` mit dem
Schluessel `room_temperature:<n>`; der Sensor des Raums m im Kreis n `SmartHeat <anlage> K<n> R<m> Raumtemperatur` mit
`room_temperature:<n>:<m>`. Kein Kundenname steht in einem Entity-Namen.

Jeder Helfer merkt sich die Quelle (Text der Vorlage) und die Fuehlerliste, auf der er angelegt wurde
(derived_sensors.json). Fehlt die Quelle oder weicht sie ab, wird er geloescht und neu angelegt; die Entity-ID bleibt
dabei gleich (slugify(name), gegen echtes HA geprueft). Den Hinweis auf einen Quellwechsel (`replaced`) gibt es nur,
wenn sich die Fuehlerliste aendert; ein anderer Vorlagentext bei gleichen Fuehlern (neuer Wertebereich in einem Update)
legt neu an, ohne den Kunden zu melden."""
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from heizungsbruecke.helper_templates import room_temperature_template
from smartheat_core.logtext import lesbar
from smartheat_runtime.backup_store import load_backup, save_backup

logger = logging.getLogger(__name__)

# Tracking-Schluessel der Helfer, die dieses Add-on anlegt oder frueher angelegt hat (outdoor_temperature: nur noch zum
# Entfernen der Anlage, ensure_all legt ihn nicht mehr an); jeder andere Eintrag ist ein Altrest.
_RAUM_KEY = re.compile(r"room_temperature(:[1-9][0-9]*(:[0-9]+)?)?")


def kreis_key(kreis: int) -> str:
    return "room_temperature" if kreis == 1 else f"room_temperature:{kreis}"


def raum_key(kreis: int, raum: int) -> str:
    return f"room_temperature:{kreis}:{raum}"


def _eigener_key(key: str) -> bool:
    return key == "outdoor_temperature" or _RAUM_KEY.fullmatch(key) is not None


@dataclass(frozen=True)
class DerivedSensors:
    entity_ids: dict[str, str]
    # Tracking-Schluessel der Helfer, die wegen einer geaenderten/unbekannten Quelle neu entstanden.
    replaced: tuple[str, ...]
    # Fingerabdruck aller Quellen, Zustand der Meldung "quellwechsel" (notifier).
    sources_fingerprint: str
    # Entity-ID des Mittelwert-Sensors je Raumnummer (nur Raeume mit Fuehlern)
    raeume: dict[int, str] = field(default_factory=dict)


def ensure_all(ha_api, tenant_id: str, room_sensors: list[str], state_path: Path, *, kreis: int = 1,
               raeume: tuple[tuple[int, tuple[str, ...]], ...] = ()) -> DerivedSensors:
    """Sensor des Kreises `kreis` (Mittel von `room_sensors`) und je Raum in `raeume` ((Nummer, Fuehler), nur Raeume mit
    Fuehlern) der Mittelwert-Sensor. Raum-Sensoren des Kreises, die das Dokument nicht mehr nennt, werden entfernt."""
    tracking = load_backup(state_path)
    replaced: list[str] = []
    sources: dict[str, str] = {}

    def ensure(key: str, source: str | None, create_fn, fuehler: list[str] | None = None) -> str:
        entity_id, was_replaced = _ensure_entity(
            ha_api, tracking, key, source, state_path, create_fn, tenant_id, fuehler,
        )
        if was_replaced:
            replaced.append(key)
        if source is not None:
            sources[key] = source
        return entity_id

    room_template = room_temperature_template(room_sensors)
    kreis_name = "" if kreis == 1 else f"K{kreis} "
    room_actual = ensure(
        kreis_key(kreis), room_template,
        lambda: ha_api.create_template_sensor(name=f"SmartHeat {tenant_id} {kreis_name}Raumtemperatur",
                                              template=room_template),
        room_sensors,
    )
    result = {"room_actual": room_actual}
    je_raum: dict[int, str] = {}
    for nummer, fuehler in raeume:
        vorlage = room_temperature_template(list(fuehler))
        je_raum[nummer] = ensure(
            raum_key(kreis, nummer), vorlage,
            lambda vorlage=vorlage, nummer=nummer: ha_api.create_template_sensor(
                name=f"SmartHeat {tenant_id} K{kreis} R{nummer} Raumtemperatur", template=vorlage),
            list(fuehler),
        )
    vorsatz = f"room_temperature:{kreis}:"
    for key in [k for k in tracking if k.startswith(vorsatz) and k not in {raum_key(kreis, n) for n, _ in raeume}]:
        try:
            _drop_unused(ha_api, tracking, key, state_path, tenant_id)
        except Exception as error:  # der Rest bleibt im Tracking, der naechste Start versucht es erneut
            logger.warning("Hilfssensor (%s) nicht geloescht (%s)", key, type(error).__name__)

    fingerprint = hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()[:12]
    return DerivedSensors(entity_ids=result, replaced=tuple(replaced), sources_fingerprint=fingerprint, raeume=je_raum)


def _ensure_entity(
    ha_api, tracking: dict, key: str, source: str | None, state_path: Path, create_fn, tenant_id: str,
    fuehler: list[str] | None = None,
) -> tuple[str, bool]:
    """(Entity-ID, Quellwechsel gemeldet?). Erst loeschen, dann anlegen: nur so
    bekommt der neue Helfer wieder dieselbe Entity-ID.

    Neu angelegt wird, wenn eine Quelle verlangt ist und von der gemerkten abweicht, auch wenn der alte Helfer schon
    fehlt (Loeschen gelang, Anlegen scheiterte im vorigen Versuch -- das Tracking wird erst nach dem Anlegen
    geschrieben). Gemeldet (zweiter Wert) wird nur, wenn sich dabei die Fuehlerliste aendert. Ein Eintrag ohne gemerkte
    Fuehlerliste (frueherer Release) gilt als fuehlergleich: der Vorlagentext kann sich ohne neue Fuehler geaendert
    haben, der Helfer wird dann ersetzt, der Kunde aber nicht gewarnt."""
    existing = tracking.get(key, {})
    existing_id = existing.get("entity_id")
    neu_anlegen = bool(existing_id) and source is not None and existing.get("source") != source
    gemerkt = existing.get("fuehler")
    meldung = neu_anlegen and fuehler is not None and gemerkt is not None and gemerkt != sorted(fuehler)
    if existing_id and ha_api.entity_exists(existing_id):
        if not neu_anlegen:
            return existing_id, False
        logger.info("Hilfs-Entity %s (%s) wird wegen geaenderter Quelle neu angelegt", lesbar(str(existing_id)), key)
        ha_api.delete_helper(existing_id, tenant_id=tenant_id)
    entity_id = create_fn()
    eintrag = {"entity_id": entity_id} if source is None else {"entity_id": entity_id, "source": source}
    if source is not None and fuehler is not None:
        eintrag["fuehler"] = sorted(fuehler)
    tracking[key] = eintrag
    save_backup(state_path, tracking)
    return entity_id, meldung


def _drop_unused(ha_api, tracking: dict, key: str, state_path: Path, tenant_id: str) -> str | None:
    """Ein Helfer der Anlage wird entfernt, erst in HA
    (ha_api.delete_helper mit dem Tenant fuer die Titelpruefung), dann aus dem Tracking. Gibt die entfernte
    Entity-ID zurueck, None ohne Tracking-Eintrag. Wirft, wenn das Loeschen scheitert; der Eintrag bleibt dann
    stehen."""
    existing_id = tracking.get(key, {}).get("entity_id")
    if existing_id is None:
        return None
    if ha_api.entity_exists(existing_id):
        ha_api.delete_helper(existing_id, tenant_id=tenant_id)
    del tracking[key]
    save_backup(state_path, tracking)
    return existing_id


def entfernen(ha_api, tenant_id: str, state_path: Path) -> bool:
    """Alle vom Add-on angelegten Hilfssensoren loeschen (Entfernen der Anlage): je Helfer erst in HA,
    dann aus dem Tracking (wie _drop_unused). Wirft nie. Ein nicht loeschbarer Helfer (HA antwortet nicht, Fehler)
    bleibt im Tracking fuer den naechsten Versuch, ins Log kommt nur die Fehlerart. Kaputte Eintraege ohne Entity-ID
    und Eintraege fremder Schluessel (Altreste frueherer Versionen) fallen ohne Loeschversuch weg. True, wenn kein
    Helfer mehr uebrig ist; dann ist auch die Datei weg."""
    try:
        tracking = load_backup(state_path)
    except (OSError, ValueError) as error:
        logger.warning("Liste der Hilfssensoren nicht lesbar (%s), verworfen", type(error).__name__)
        tracking = {}
    if not isinstance(tracking, dict):
        tracking = {}
    for key in list(tracking):
        entry = tracking[key]
        if not _eigener_key(key) or not isinstance(entry, dict) or not isinstance(entry.get("entity_id"), str):
            del tracking[key]
            continue
        try:
            removed = _drop_unused(ha_api, tracking, key, state_path, tenant_id)
        except Exception as error:
            logger.warning("Hilfssensor %s (%s) nicht geloescht (%s), neuer Versuch beim naechsten Start",
                           lesbar(str(entry["entity_id"])), key, type(error).__name__)
            continue
        logger.info("Hilfssensor %s (%s) geloescht", lesbar(str(removed)), key)
    try:
        if tracking:
            save_backup(state_path, tracking)
            return False
        state_path.unlink(missing_ok=True)
    except OSError as error:
        logger.warning("Liste der Hilfssensoren nicht gespeichert (%s)", type(error).__name__)
        return False
    return True
