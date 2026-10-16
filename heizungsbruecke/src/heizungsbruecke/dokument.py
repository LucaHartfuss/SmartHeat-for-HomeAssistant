"""Konfigurationsdokument des HA-Add-ons (Spec 5c 1.3, Stufe 3 S5 3.2): `bindung` liest die HA-Bindung direkt (Rollen
der Laufzeit, Raumfuehler, Batterien, Handys, Hinweise); kein Zwischenformat mehr. Der Raum-Soll der Regelung kommt aus
dem Soll-Speicher (SOLL_REF), die Soll-Entity ist nur Eingang und Anzeige. zuordnung_pruefen ist der HA-Teil der
lokalen Pruefung (config_check ruft ihn ueber DeviceHost.treiber_pruefen, Spec 5b 3.1): Pflicht-Entities und
Schreibbarkeit je Hebelsatz, Entities vorhanden, Schreibziele (jeder Hebel der Bindung, nicht nur die Schreibregeln des
Hebelsatzes) nur von Plattform und Config-Entry des Kreises (A4-11), Soll-Entity nicht am Geraet eines Schreibziels
(A4-12). Ob outdoor_temp und die Kennzahl-Rollen passen, prueft der Server (ha_pruefung).

Ist HA nicht erreichbar oder faehrt gerade hoch, ist das kein Urteil: der Server stellt eine abgelehnte Version nicht
erneut zu, ein Neustart des Hosts haette das Geraet sonst festgesetzt. zuordnung_pruefen wirft dann NichtPruefbar (das
Geraet haelt das Dokument zurueck und prueft spaeter erneut). Bleiben Bindung, Raumfuehler und Hebelsatz gegenueber der
uebernommenen Konfiguration gleich (z. B. nur das Abo oder der Tagestick aendert sich), entfaellt der HA-Teil."""
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass

from smartheat_core.config_check import NichtPruefbar
from smartheat_core.levers import LEVER_SETS
from smartheat_runtime.dokument_config import RECONFIGURE_HINT, ConfigError
from smartheat_runtime.roles import LEVER_ROLES

logger = logging.getLogger(__name__)

_ROOM_TARGET = "room_target"
#: Was die Regelung ausser den Hebeln des Hebelsatzes schreibt (Weishaupt/Viessmann: Betriebsart, Hilfs-Sollwerte).
WEITERE_SCHREIBZIELE = ("mode_select", "setpoint_comfort", "setpoint_setback")
#: Felder, deren Aenderung eine erneute Pruefung gegen HA braucht.
_HA_FELDER = ("bindung", "raumfuehler", "hebelsatz")
#: Pflichtrollen je Hebelsatz (Laufzeitrollen, ohne room_target: das ist der Soll-Speicher, SOLL_REF).
PFLICHT_ROLLEN: dict[str, tuple[str, ...]] = {
    "vaillant_vrc720": ("curve_current", "shift_current", "min_flow", "heat_limit", "outdoor_temp"),
    "weishaupt_wwp": ("curve_current", "shift_current", "heat_limit", "outdoor_temp", "mode_select",
                      "setpoint_comfort", "setpoint_setback"),
    "weishaupt_wwp_basis": ("shift_current", "outdoor_temp", "mode_select", "setpoint_comfort", "setpoint_setback"),
    "viessmann_vicare": ("curve_current", "level_current", "shift_current", "outdoor_temp", "mode_select"),
}
_NUMBER = (re.compile(r"number\.[a-z0-9_]+"), "eine number.*-Entity")
#: Schreibbarkeit je Hebelsatz (Spec 5.6 "schreibbar"): HaPlantBinding.write kennt nur climate.set_temperature und
#: number.set_value; Weishaupt-Betriebsart ist ein select, das Viessmann-Heizprogramm eine climate-Entity.
SCHREIBREGELN: dict[str, dict[str, tuple[re.Pattern, str]]] = {
    "vaillant_vrc720": {
        "shift_current": (re.compile(r"climate\.[a-z0-9_]+(::temperature)?|number\.[a-z0-9_]+"),
                          "eine climate.*- oder number.*-Entity"),
        "min_flow": _NUMBER, "heat_limit": _NUMBER,
    },
    "weishaupt_wwp": {
        "curve_current": _NUMBER, "shift_current": _NUMBER, "heat_limit": _NUMBER, "setpoint_comfort": _NUMBER,
        "setpoint_setback": _NUMBER, "mode_select": (re.compile(r"select\.[a-z0-9_]+"), "eine select.*-Entity"),
    },
    "weishaupt_wwp_basis": {
        "shift_current": _NUMBER, "setpoint_comfort": _NUMBER, "setpoint_setback": _NUMBER,
        "mode_select": (re.compile(r"select\.[a-z0-9_]+"), "eine select.*-Entity"),
    },
    "viessmann_vicare": {
        "curve_current": _NUMBER, "level_current": _NUMBER, "shift_current": _NUMBER,
        "mode_select": (re.compile(r"climate\.[a-z0-9_]+"), "eine climate.*-Entity"),
    },
}


def laufzeit_rolle(rolle: str) -> str:
    """Rolle der HA-Bindung (V1 HA_ROLLEN) -> Rolle des Manifests (Hebel ueber LEVER_ROLES, sonst gleich)."""
    return LEVER_ROLES.get(rolle, rolle)


@dataclass(frozen=True)
class HaBindung:
    hebelsatz: str
    rollen: dict[str, str]
    soll_entity: str | None
    raumfuehler: tuple[str, ...]
    batterien: tuple[str, ...]
    benachrichtigung: tuple[str, ...]
    hinweise_aus: tuple[str, ...]
    kreis: dict


def _mapping(value) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _texte(value) -> tuple[str, ...]:
    return tuple(item for item in value if isinstance(item, str)) if isinstance(value, list) else ()


def bindung(konfiguration) -> HaBindung:
    """HA-Bindung aus dem Dokument; config_check hat die Form geprueft. Wirft ConfigError bei einem Hebelsatz, den der
    HA-Host nicht kennt."""
    konfiguration = _mapping(konfiguration)
    hebelsatz = konfiguration.get("hebelsatz")
    if not isinstance(hebelsatz, str) or hebelsatz not in LEVER_SETS or hebelsatz not in PFLICHT_ROLLEN:
        raise ConfigError(f"Hebelsatz {hebelsatz!r} ist für Home Assistant unbekannt – {RECONFIGURE_HINT}")
    raw = _mapping(konfiguration.get("bindung"))
    rollen = {laufzeit_rolle(rolle): ref for rolle, ref in _mapping(raw.get("rollen")).items()
              if rolle != _ROOM_TARGET and isinstance(ref, str) and ref}
    soll = _mapping(raw.get("rollen")).get(_ROOM_TARGET)
    return HaBindung(
        hebelsatz=hebelsatz, rollen=rollen, soll_entity=soll if isinstance(soll, str) and soll else None,
        raumfuehler=_texte(konfiguration.get("raumfuehler")), batterien=_texte(raw.get("batterien")),
        benachrichtigung=_texte(raw.get("benachrichtigung")), hinweise_aus=_texte(raw.get("hinweise_aus")),
        kreis=dict(_mapping(raw.get("kreis"))))


def _entity(ref: str) -> str:
    return ref.partition("::")[0]


def schreibziele(b: HaBindung) -> list[str]:
    """Jede Rolle der Bindung, die die Regelung schreibt: Schreibregeln des Hebelsatzes, alle Hebelrollen (auch die
    Heizkurve, die bei vaillant_vrc720 keine Schreibregel hat) und Betriebsart/Hilfs-Sollwerte."""
    rollen = dict.fromkeys([*SCHREIBREGELN[b.hebelsatz], *LEVER_ROLES.values(), *WEITERE_SCHREIBZIELE])
    return [b.rollen[rolle] for rolle in rollen if b.rollen.get(rolle)]


def _lokal(b: HaBindung) -> str | None:
    """Der Teil ohne HA: Pflichtrollen und Entity-Art der Schreibziele."""
    missing = [rolle for rolle in PFLICHT_ROLLEN[b.hebelsatz] if not b.rollen.get(rolle)]
    if missing:
        return f"Für die Ansteuerung {b.hebelsatz} fehlt die Zuordnung {missing[0]}."
    for rolle, (pattern, expected) in SCHREIBREGELN[b.hebelsatz].items():
        value = b.rollen.get(rolle)
        if value is not None and not pattern.fullmatch(value):
            return f"{value} muss {expected} sein."
    if b.soll_entity is None:
        return "Die Wunschtemperatur-Entity fehlt."
    return None


def lokal_pruefen(konfiguration) -> str | None:
    """Der Teil ohne HA fuer das ganze Dokument: Kundentext oder None. Wirft ConfigError bei unbekanntem Hebelsatz
    (HaHost.load macht daraus einen Konfigurationsfehler, damit der Start nie an einem fehlenden Schluessel stirbt)."""
    return _lokal(bindung(konfiguration))


def _mit_ha(b: HaBindung, ha_api) -> str | None:
    if not ha_api.is_reachable():  # faehrt HA hoch, fehlen die Entities der Integration noch (404): kein Urteil
        raise NichtPruefbar
    registry = {e.get("entity_id"): e for e in ha_api.entity_registry() if isinstance(e, Mapping)}
    assert b.soll_entity is not None
    for ref in (b.soll_entity, *b.rollen.values(), *b.raumfuehler, *b.batterien):
        if not ha_api.entity_exists(_entity(ref)):
            return f"Die Entity {_entity(ref)} gibt es in Home Assistant nicht."
    ziele = schreibziele(b)
    for ref in ziele:
        entry = registry.get(_entity(ref)) or {}
        # Kreis ohne Config-Entry (config_entry None): nur die Plattform bindet
        if entry.get("platform") != b.kreis.get("plattform") or (
                b.kreis.get("config_entry") is not None and entry.get("config_entry_id") != b.kreis["config_entry"]):
            return f"{_entity(ref)} gehört nicht zur gewählten Heizungs-Integration."
    soll_entry = registry.get(_entity(b.soll_entity)) or {}
    geraete = {(registry.get(_entity(ref)) or {}).get("device_id") for ref in ziele} - {None}
    if _entity(b.soll_entity) in {_entity(ref) for ref in ziele} or (
            soll_entry.get("device_id") is not None and soll_entry.get("device_id") in geraete):
        return "Die Wunschtemperatur darf kein Wert der Heizungsanlage selbst sein."
    return None


def _unveraendert(konfiguration, akzeptiert) -> bool:
    return isinstance(akzeptiert, Mapping) and all(
        key in akzeptiert and akzeptiert[key] == konfiguration.get(key) for key in _HA_FELDER)


def zuordnung_pruefen(konfiguration, ha_api, akzeptiert=None) -> str | None:
    """None = in Ordnung; sonst ein Kundentext (Grund rolle_unzulaessig). `akzeptiert` ist die uebernommene
    Konfiguration (None, wenn es keine gibt). Wirft NichtPruefbar, wenn HA gerade nicht antwortet (siehe oben), sonst
    nie."""
    try:
        b = bindung(konfiguration)
        text = _lokal(b)
    except ConfigError as error:
        return str(error)
    if text is not None or b.soll_entity is None or _unveraendert(konfiguration, akzeptiert):
        return text
    try:
        return _mit_ha(b, ha_api)
    except NichtPruefbar:
        raise
    except Exception as error:  # HA nicht erreichbar, unerwartete Antwort: spaeter erneut, nie eine Ablehnung
        logger.warning("Zuordnung vorerst nicht pruefbar (%s)", type(error).__name__)
        raise NichtPruefbar from None
