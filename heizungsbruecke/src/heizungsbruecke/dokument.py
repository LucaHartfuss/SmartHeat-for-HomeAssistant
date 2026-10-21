"""Konfigurationsdokument des HA-Add-ons (Spec 5c 1.3, Stufe 3 S5 3.2): `bindung` liest die HA-Bindung direkt (Rollen
der Laufzeit, Raumfuehler, Batterien, Handys, Hinweise); kein Zwischenformat mehr. Der Raum-Soll der Regelung kommt aus
dem Soll-Speicher (SOLL_REF), die Soll-Entity ist nur Eingang und Anzeige. zuordnung_pruefen ist der HA-Teil der
lokalen Pruefung (config_check ruft ihn ueber DeviceHost.treiber_pruefen, Spec 5b 3.1): Pflicht-Entities und
Schreibbarkeit je Hebelsatz, Entities vorhanden, Schreibziele (jeder Hebel der Bindung, nicht nur die Schreibregeln des
Hebelsatzes) nur von Plattform und Config-Entry des Kreises (A4-11), Soll-Entity nicht am Geraet eines Schreibziels
(A4-12). Jede gebundene Entity (Soll-Entity, Rollen, Raumfuehler, Batterien) besteht ausserdem die lokale Positivliste
der Inventur (rollen_pruefen, A6-02). Ob outdoor_temp und die Kennzahl-Rollen fachlich passen, prueft der Server
(ha_pruefung).

Ist HA nicht erreichbar oder faehrt gerade hoch, ist das kein Urteil: der Server stellt eine abgelehnte Version nicht
erneut zu, ein Neustart des Hosts haette das Geraet sonst festgesetzt. zuordnung_pruefen wirft dann NichtPruefbar (das
Geraet haelt das Dokument zurueck und prueft spaeter erneut). Bleiben Bindung, Raumfuehler und Hebelsatz gegenueber der
uebernommenen Konfiguration gleich (z. B. nur das Abo oder der Tagestick aendert sich), entfaellt der HA-Teil.
Ablehnungen sind details (Rollen, nie Entity-IDs; Spec Stufe 3 S5 6.1), nur lokal_pruefen liefert den lokalen Text."""
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass

from heizungsbruecke.inventur import ERLAUBTE_DOMAENEN, einstellbare_plattformen, entity_erlaubt
from smartheat_core.config_check import NichtPruefbar
from smartheat_core.levers import LEVER_SETS
from smartheat_device import fehler
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


_V1_ROLLE = {laufzeit: v1 for v1, laufzeit in LEVER_ROLES.items()}


def v1_rolle(rolle: str) -> str:
    """Rolle des Manifests -> Rolle der HA-Bindung (V1 HA_ROLLEN), Umkehrung von laufzeit_rolle."""
    return _V1_ROLLE.get(rolle, rolle)


@dataclass(frozen=True)
class Befund:
    """HA-Pruefung: details fuer den Server (config_check, V1) und der lokale Text fuer den Konfigurationsfehler beim
    Start (lokal_pruefen, Spec Stufe 3 S5 6.3). details nennen Rollen, nie Entity-IDs (Regel 6)."""
    details: dict
    text: str


def _schreibziel_rollen(b: HaBindung) -> list[tuple[str, str]]:
    """Rollen der Bindung, die die Regelung schreibt: Schreibregeln des Hebelsatzes, alle Hebelrollen (auch die
    Heizkurve, die bei vaillant_vrc720 keine Schreibregel hat) und Betriebsart/Hilfs-Sollwerte."""
    rollen = dict.fromkeys([*SCHREIBREGELN[b.hebelsatz], *LEVER_ROLES.values(), *WEITERE_SCHREIBZIELE])
    return [(rolle, b.rollen[rolle]) for rolle in rollen if b.rollen.get(rolle)]


def _lokal(b: HaBindung) -> Befund | None:
    """Der Teil ohne HA: Pflichtrollen und Entity-Art der Schreibziele."""
    missing = [rolle for rolle in PFLICHT_ROLLEN[b.hebelsatz] if not b.rollen.get(rolle)]
    if missing:
        return Befund(fehler.details(v1_rolle(missing[0]), "fehlt"),
                      f"Für die Ansteuerung {b.hebelsatz} fehlt die Zuordnung {missing[0]}.")
    for rolle, (pattern, expected) in SCHREIBREGELN[b.hebelsatz].items():
        value = b.rollen.get(rolle)
        if value is not None and not pattern.fullmatch(value):
            return Befund(fehler.details(v1_rolle(rolle), "format"), f"{value} muss {expected} sein.")
    if b.soll_entity is None:
        return Befund(fehler.details("room_target", "fehlt"), "Die Wunschtemperatur-Entity fehlt.")
    return None


def lokal_pruefen(konfiguration) -> str | None:
    """Der Teil ohne HA fuer das ganze Dokument: lokaler Text oder None (Konfigurationsfehler beim Start). Wirft
    ConfigError bei unbekanntem Hebelsatz (HaHost.load macht daraus einen Konfigurationsfehler, damit der Start nie an
    einem fehlenden Schluessel stirbt)."""
    befund = _lokal(bindung(konfiguration))
    return befund.text if befund is not None else None


def _refs(b: HaBindung) -> list[tuple[str, str]]:
    """Alle gebundenen Referenzen als (Feld, Referenz) in der Reihenfolge der Pruefung; Feld ist die V1-Rolle bzw.
    raumfuehler/batterien, nie die Entity. Ohne Soll-Entity entfaellt room_target (lokal_pruefen meldet sie als fehlend)."""
    soll = [("room_target", b.soll_entity)] if b.soll_entity is not None else []
    return [*soll, *((v1_rolle(rolle), ref) for rolle, ref in b.rollen.items()),
            *(("raumfuehler", ref) for ref in b.raumfuehler), *(("batterien", ref) for ref in b.batterien)]


def _pflicht_refs(b: HaBindung) -> set[str]:
    """Die Referenzen, ohne die nichts laeuft und die von selbst wiederkommen: Soll-Entity, Pflichtrollen des Hebelsatzes
    (dieselben wie host._wait_for_required_entities, plus Soll-Entity), Raumfuehler und jedes Schreibziel. Ein Schreibziel
    darf nie fallen gelassen werden, auch eine optionale Hebelrolle nicht: sie gehoert zur Anlagenidentitaet
    (roles.lever_refs_identity), ohne sie wuerde bind_to_tenant eine andere Anlage melden und alles Gebundene
    zuruecksetzen. Fallen duerfen nur optionale Lese-Rollen und Batterien."""
    return {*([b.soll_entity] if b.soll_entity is not None else []),
            *(b.rollen[rolle] for rolle in PFLICHT_ROLLEN[b.hebelsatz] if rolle in b.rollen), *b.raumfuehler,
            *(ref for _, ref in _schreibziel_rollen(b))}


#: Domaenen der Positivliste, die eine Entity ohne Zustand schon an ihrer ID bestehen laesst (Sensoren nur mit Zustand
#: nach Klasse pruefbar, hier nur die Domaene); binary_sensor nur als Batterie.
_DOMAENEN_ROLLE = ERLAUBTE_DOMAENEN | {"sensor"}
_DOMAENEN_BATTERIE = frozenset({"sensor", "binary_sensor"})


def rollen_befund(b: HaBindung, ha_api) -> tuple[tuple[str, str] | None, list[tuple[str, str]]]:
    """Lokale Positivliste fuer jede Rolle (A6-02, G-9): ((feld, "nicht_erlaubt") des ersten Verstosses oder None,
    [(feld, ref) der erlaubten optionalen Lese-Rollen und Batterien ohne Zustand]). Soll-Entity, Rollen, Raumfuehler und
    Batterien muessen die Positivliste der Inventur bestehen (inventur.entity_erlaubt); `::attribut` zaehlt die Entity
    davor. Registry und Zustaende werden einmal geladen; die Plattformen mit Einstellbarem zaehlen aus allen aktiven
    Registry-Eintraegen mit Zustand. Hat Soll-Entity, Pflichtrolle, Schreibziel (auch eine optionale Hebelrolle) oder Raumfuehler keinen Zustand, ist das kein
    Urteil: NichtPruefbar(feld) (kommt von selbst wieder). Eine optionale Lese-Rolle oder Batterie ohne Zustand (geloescht,
    umbenannt, deaktiviert) heilt nicht von selbst: ihre Domaene wird aus der ID geprueft, sie steht in der zweiten
    Liste und wird nicht gelesen. Ohne Soll-Entity entfaellt room_target (lokal_pruefen meldet das)."""
    registry = {e["entity_id"]: e for e in ha_api.entity_registry() if isinstance(e, Mapping) and "entity_id" in e}
    states = {s["entity_id"]: s for s in ha_api.get_states() if isinstance(s, Mapping) and "entity_id" in s}
    einstellbar = einstellbare_plattformen({entity_id: entry for entity_id, entry in registry.items()
                                            if entity_id in states})
    pflicht = _pflicht_refs(b)
    ohne_zustand: list[tuple[str, str]] = []
    for feld, ref in _refs(b):
        entity_id = _entity(ref)
        if (state := states.get(entity_id)) is None:
            if ref in pflicht:
                raise NichtPruefbar(feld)
            if entity_id.partition(".")[0] not in (_DOMAENEN_BATTERIE if feld == "batterien" else _DOMAENEN_ROLLE):
                return (feld, "nicht_erlaubt"), ohne_zustand
            ohne_zustand.append((feld, ref))
        elif not entity_erlaubt(entity_id, registry.get(entity_id) or {}, state, einstellbar):
            return (feld, "nicht_erlaubt"), ohne_zustand
    return None, ohne_zustand


def rollen_pruefen(b: HaBindung, ha_api) -> tuple[str, str] | None:
    """Das Urteil aus rollen_befund: (feld, "nicht_erlaubt") des ersten Verstosses oder None; wirft NichtPruefbar wie
    dort."""
    return rollen_befund(b, ha_api)[0]


def _mit_ha(b: HaBindung, ha_api) -> tuple[str, str] | None:
    """Der Teil mit HA: (feld, regel) des ersten Verstosses oder None. Bildet keine details (fehler.details kann bei
    einem Programmierfehler ValueError werfen, der in zuordnung_pruefen nicht als „HA nicht erreichbar“ gelten darf)."""
    if not ha_api.is_reachable():  # faehrt HA hoch, fehlen die Entities der Integration noch (404): kein Urteil
        raise NichtPruefbar
    registry = {e.get("entity_id"): e for e in ha_api.entity_registry() if isinstance(e, Mapping)}
    for feld, ref in _refs(b):
        if not ha_api.entity_exists(_entity(ref)):
            return feld, "unbekannt"
    if verstoss := rollen_pruefen(b, ha_api):
        return verstoss
    assert b.soll_entity is not None
    ziele = _schreibziel_rollen(b)
    for rolle, ref in ziele:
        entry = registry.get(_entity(ref)) or {}
        # Kreis ohne Config-Entry (Feld fehlt): nur die Plattform bindet
        if entry.get("platform") != b.kreis.get("plattform") or (
                b.kreis.get("config_entry") is not None and entry.get("config_entry_id") != b.kreis["config_entry"]):
            return v1_rolle(rolle), "nicht_erlaubt"
    soll_entry = registry.get(_entity(b.soll_entity)) or {}
    geraete = {(registry.get(_entity(ref)) or {}).get("device_id") for _, ref in ziele} - {None}
    if _entity(b.soll_entity) in {_entity(ref) for _, ref in ziele} or (
            soll_entry.get("device_id") is not None and soll_entry.get("device_id") in geraete):
        return "room_target", "nicht_erlaubt"
    return None


def _unveraendert(konfiguration, akzeptiert) -> bool:
    return isinstance(akzeptiert, Mapping) and all(
        key in akzeptiert and akzeptiert[key] == konfiguration.get(key) for key in _HA_FELDER)


def zuordnung_pruefen(konfiguration, ha_api, akzeptiert=None) -> dict | None:
    """None = in Ordnung; sonst details (Grund rolle_unzulaessig, Spec Stufe 3 S5 6.1). `akzeptiert` ist die
    uebernommene Konfiguration (None, wenn es keine gibt). Wirft NichtPruefbar, wenn HA gerade nicht antwortet (siehe
    oben), sonst nie."""
    try:
        b = bindung(konfiguration)
    except ConfigError:
        return fehler.details("hebelsatz", "unbekannt")
    befund = _lokal(b)
    if befund is not None:
        return befund.details
    if _unveraendert(konfiguration, akzeptiert):
        return None
    try:
        fund = _mit_ha(b, ha_api)
    except NichtPruefbar:
        raise
    except Exception as error:  # HA nicht erreichbar, unerwartete Antwort: spaeter erneut, nie eine Ablehnung
        logger.warning("Zuordnung vorerst nicht pruefbar (%s)", type(error).__name__)
        raise NichtPruefbar from None
    # ausserhalb des try: ein Verstoss gegen den Vertrag (ValueError) ist ein Programmierfehler und bleibt sichtbar
    return fehler.details(*fund) if fund is not None else None
