"""Konfigurationsdokument des HA-Add-ons (Spec 5c 1.3, Stufe 3 S5 3.2): `bindung` liest die HA-Bindung direkt (Rollen
der Laufzeit, Raumfuehler, Batterien, Handys, Hinweise); kein Zwischenformat mehr. Der Raum-Soll der Regelung kommt aus
dem Soll-Speicher (SOLL_REF), die Soll-Entity ist nur Eingang und Anzeige. zuordnung_pruefen ist der HA-Teil der
lokalen Pruefung (config_check ruft ihn ueber DeviceHost.treiber_pruefen, Spec 5b 3.1): Pflicht-Entities und
Schreibbarkeit je Hebelsatz, Entities vorhanden, jede gebundene Entity (Soll-Entity, Rollen, Raumfuehler, Batterien)
besteht die lokale Positivliste der Inventur (rollen_pruefen, A6-02), dann die Schreibziele (schreibziele_befund).
Schreibziele (jeder Hebel der Bindung, nicht nur die Schreibregeln des Hebelsatzes, A4-11) kommen nur von der lokal je
Hebelsatz festgelegten Plattform (SCHREIB_PLATTFORM, A5-70) und alle aus einem Config-Entry (dem des Kreises, wenn er
eines nennt); die Soll-Entity ist kein Schreibziel und nicht an dessen Geraet (A4-12). Dieselben Regeln gelten beim Start
fuer die uebernommene Konfiguration (rollen_befund mit beim_start=True, L4-27). Ob outdoor_temp und die Kennzahl-Rollen fachlich passen,
prueft der Server (ha_pruefung).

Ist HA nicht erreichbar oder faehrt gerade hoch, ist das kein Urteil: der Server stellt eine abgelehnte Version nicht
erneut zu, ein Neustart des Hosts haette das Geraet sonst festgesetzt. zuordnung_pruefen wirft dann NichtPruefbar (das
Geraet haelt das Dokument zurueck und prueft spaeter erneut). Bleiben Bindung, Raumfuehler und Hebelsatz gegenueber der
uebernommenen Konfiguration gleich (z. B. nur das Abo oder der Tagestick aendert sich), entfaellt der HA-Teil.
Ablehnungen sind details (Rollen, nie Entity-IDs; Spec Stufe 3 S5 6.1), nur lokal_pruefen liefert den lokalen Text."""
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass

from heizungsbruecke.inventur import einstellbare_plattformen, entity_erlaubt, entity_klasse
from smartheat_core.bindungsparameter import ENERGIE_KANAELE
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
#: Plattform der Schreibziele je Hebelsatz (A5-70, Regel 4, Nutzer-Entscheidung E5 2026-10-22): lokal verankert, der
#: Kreis des Dokuments (kreis.plattform) muss sie nennen, sonst lehnt das Geraet ab (plattform/nicht_erlaubt); auch die
#: Lese-Rollen ohne Klasse haengen an ihr (_form_erlaubt). Schluessel wie PFLICHT_ROLLEN; ein Contract-Check haelt die
#: Tabelle gegen den Katalog des Servers.
SCHREIB_PLATTFORM: dict[str, str] = {
    "vaillant_vrc720": "mypyllant",
    "weishaupt_wwp": "weishaupt_modbus",
    "weishaupt_wwp_basis": "weishaupt_modbus",
    "viessmann_vicare": "vicare",
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


#: Was den Heizkreis bestimmt (A6-36): ohne config_entry, eine Neuanlage der Integration bleibt dieselbe Anlage.
# Achtung (A6-36): Felder und Format bestimmen die plant_id jeder HA-Installation. anlage und nummer leitet der
# Server ab (einrichtung/ha_erkennung.py, _normalize: Praefix der unique_id; Kreismuster des Katalogs); aendert er die
# Ableitung, gilt beim naechsten Speichern im Assistenten jede HA-Anlage als andere Anlage, bind_to_tenant verwirft
# Ursprungswerte, Wiederherstellungspunkt und Energiesummen (Restpunkt A6-36-Format).
KREIS_KENNUNG = ("plattform", "anlage", "nummer")


def anlagen_kennung(b: HaBindung) -> str:
    """Anlagen-Kennung des HA-Hosts (Loaded.plant_id, A6-36): der Heizkreis als JSON. Eine umbenannte oder neu
    zugeordnete Hebel-Entity desselben Kreises bleibt dieselbe Anlage (bis 1.1.0 waren es die Hebel-Referenzen,
    roles.lever_refs_identity; app.start uebernimmt einen solchen Bestand einmal)."""
    return json.dumps({key: b.kreis.get(key) for key in KREIS_KENNUNG}, sort_keys=True)


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
    darf nie fallen gelassen werden, auch eine optionale Hebelrolle nicht (ohne Zustand: nicht pruefbar, Wiederholung):
    bis zum Rueckbau der Uebernahme (A6-36-Uebernahme) gehoert sie zur frueheren Anlagen-Kennung
    (roles.lever_refs_identity, app.start), ohne sie verwuerfe bind_to_tenant einen Bestand von 1.1.0. Fallen duerfen
    nur optionale Lese-Rollen und Batterien."""
    return {*([b.soll_entity] if b.soll_entity is not None else []),
            *(b.rollen[rolle] for rolle in PFLICHT_ROLLEN[b.hebelsatz] if rolle in b.rollen), *b.raumfuehler,
            *(ref for _, ref in _schreibziel_rollen(b))}


def optionale_felder(b: HaBindung) -> list[str]:
    """Die Felder der gebundenen optionalen Lese-Rollen und Batterien (ohne Wiederholung, in der Reihenfolge der
    Pruefung): die einzigen, die ohne Zustand ungelesen bleiben duerfen (siehe _pflicht_refs)."""
    pflicht = _pflicht_refs(b)
    return list(dict.fromkeys(feld for feld, ref in _refs(b) if ref not in pflicht))


#: Klasse "beliebig, aber ohne Klasse nur von der Plattform des Hebelsatzes" (Lese-Rollen ohne feste Klasse).
FREIE_KLASSE = "*"
_TEMPERATUR = ("sensor", "temperature")
_FREI = ("sensor", FREIE_KLASSE)
#: Form je Feld der Bindung (A6-02 je Feld, Spec Rollen je Feld 2): erlaubte (Domaene, Geraeteklasse); Klasse None =
#: Domaene ohne Klassenpruefung (climate, weather). Zusaetzlich zu inventur.entity_erlaubt. Schreibziele stehen nicht
#: hier (sie halten SCHREIBREGELN und schreibziele_befund); jedes andere Feld ist nicht erlaubt (fail closed). Der Server
#: fuehrt dieselben Regeln in einrichtung/ha_rollen.py, der Contract-Check vergleicht beide.
ROLLEN_FORM: dict[str, tuple[tuple[str, str | None], ...]] = {
    "room_target": (("climate", None), _TEMPERATUR),
    "outdoor_temp": (("weather", None), _TEMPERATUR),
    "flow_setpoint": (_TEMPERATUR,), "flow_temperature": (_TEMPERATUR,), "return_temperature": (_TEMPERATUR,),
    "system_water_pressure": (("sensor", "pressure"),),
    **{f"energy_{kanal}": (("sensor", "energy"),) for kanal in ENERGIE_KANAELE},
    "operating_mode": (_FREI,), "efficiency_ratio": (_FREI,), "generator_hours": (_FREI,),
    "generator_starts": (_FREI,), "generator_state": (_FREI,),
    "raumfuehler": (_TEMPERATUR, ("climate", None)),
    "batterien": (("sensor", "battery"), ("binary_sensor", "battery")),
}
#: Ein climate-Raumfuehler liefert seine Ist-Temperatur, nie das Soll.
_KLIMA_IST = "::current_temperature"


def _form_domaene(feld: str, ref: str) -> bool:
    """Ohne Zustand: nur die Domaene aus der ID (und beim climate-Raumfuehler das Attribut) gegen die Form des Felds."""
    domain = _entity(ref).partition(".")[0]
    if feld == "raumfuehler" and domain == "climate" and not ref.endswith(_KLIMA_IST):
        return False
    return any(domain == erlaubt for erlaubt, _ in ROLLEN_FORM.get(feld, ()))


def _form_erlaubt(feld: str, ref: str, registry_entry: Mapping, state: Mapping, schreib_plattform: str) -> bool:
    """Mit Zustand: Domaene, Geraeteklasse und bei freier Klasse ohne Klasse die Plattform des Hebelsatzes
    (SCHREIB_PLATTFORM, nicht kreis.plattform des Servers)."""
    if not _form_domaene(feld, ref):
        return False
    domain = _entity(ref).partition(".")[0]
    klasse = entity_klasse(registry_entry, state)
    plattform = registry_entry.get("platform")
    for erlaubt, verlangt in ROLLEN_FORM[feld]:
        if erlaubt != domain:
            continue
        if verlangt is None or klasse == verlangt:
            return True
        if verlangt == FREIE_KLASSE and (klasse is not None or (plattform is not None and plattform == schreib_plattform)):
            return True
    return False


#: Zweites Argument von NichtPruefbar, wenn einem Schreibziel beim Start der Registry-Eintrag fehlt (eigener Starttext).
OHNE_REGISTRY = "registry"


def schreibziele_befund(b: HaBindung, registry: Mapping[str, Mapping], *, beim_start: bool) -> tuple[str, str] | None:
    """Die Regeln der Schreibziele (A4-11, A4-12, A5-70, L4-27; Regel 4, E5): (feld, "nicht_erlaubt") des ersten
    Verstosses oder None. Der Kreis nennt die Plattform des Hebelsatzes (SCHREIB_PLATTFORM, sonst Feld plattform); jedes
    Schreibziel (_schreibziel_rollen) kommt von dieser Plattform, alle aus einem Config-Entry (dem des Kreises, wenn er
    eines nennt, sonst dem des ersten Schreibziels; ohne Config-Entry nicht erlaubt); die Soll-Entity ist kein
    Schreibziel und nicht an dessen Geraet. `registry` ist die Entity-Registry je entity_id. Ein Schreibziel ohne
    Registry-Eintrag: beim Uebernehmen (beim_start=False) nicht_erlaubt wie bisher, beim Start kein Urteil, sondern
    NichtPruefbar(feld, OHNE_REGISTRY) (Wiederholung, fail closed). Keyword ohne Standard: jeder Aufrufer entscheidet."""
    plattform = SCHREIB_PLATTFORM[b.hebelsatz]
    if b.kreis.get("plattform") != plattform:
        return "plattform", "nicht_erlaubt"
    config_entry = b.kreis.get("config_entry")
    ziele = _schreibziel_rollen(b)
    geraete: set[str | None] = set()
    for rolle, ref in ziele:
        eintrag = registry.get(_entity(ref))
        if eintrag is None:
            if beim_start:
                raise NichtPruefbar(v1_rolle(rolle), OHNE_REGISTRY)
            return v1_rolle(rolle), "nicht_erlaubt"
        eigenes = eintrag.get("config_entry_id")
        if eintrag.get("platform") != plattform or not isinstance(eigenes, str) or not eigenes or (
                config_entry is not None and eigenes != config_entry):
            return v1_rolle(rolle), "nicht_erlaubt"
        config_entry = eigenes  # ohne Config-Entry im Kreis bindet das erste Schreibziel alle weiteren
        geraete.add(eintrag.get("device_id"))
    if b.soll_entity is not None:
        soll_geraet = (registry.get(_entity(b.soll_entity)) or {}).get("device_id")
        if _entity(b.soll_entity) in {_entity(ref) for _, ref in ziele} or (
                soll_geraet is not None and soll_geraet in geraete):
            return "room_target", "nicht_erlaubt"
    return None


def rollen_befund(b: HaBindung, ha_api, *, beim_start: bool) -> tuple[tuple[str, str] | None, list[tuple[str, str]]]:
    """Lokale Positivliste fuer jede Rolle (A6-02, G-9) und die Regeln der Schreibziele (schreibziele_befund, L4-27):
    ((feld, "nicht_erlaubt") des ersten Verstosses oder None, [(feld, ref) der erlaubten optionalen Lese-Rollen und
    Batterien ohne Zustand]). Soll-Entity, Rollen, Raumfuehler und Batterien muessen die Positivliste der Inventur
    bestehen (inventur.entity_erlaubt); `::attribut` zaehlt die Entity davor. Registry und Zustaende werden einmal
    geladen; die Plattformen mit Einstellbarem zaehlen aus allen aktiven Registry-Eintraegen mit Zustand. Hat
    Soll-Entity, Pflichtrolle, Schreibziel (auch eine optionale Hebelrolle) oder Raumfuehler keinen Zustand, ist das
    kein Urteil: NichtPruefbar(feld) (kommt von selbst wieder); einem Schreibziel ohne Registry-Eintrag geht es nur beim
    Start so (beim_start, schreibziele_befund), beim Uebernehmen ist es nicht_erlaubt. Eine
    optionale Lese-Rolle oder Batterie ohne Zustand (geloescht, umbenannt, deaktiviert) heilt nicht von selbst: ihre
    Domaene wird aus der ID geprueft, sie steht in der zweiten Liste und wird nicht gelesen. Ohne Soll-Entity entfaellt
    room_target (lokal_pruefen meldet das). Die Schreibziel-Regeln folgen nach der Positivliste aller Felder."""
    registry = {e["entity_id"]: e for e in ha_api.entity_registry() if isinstance(e, Mapping) and "entity_id" in e}
    states = {s["entity_id"]: s for s in ha_api.get_states() if isinstance(s, Mapping) and "entity_id" in s}
    einstellbar = einstellbare_plattformen({entity_id: entry for entity_id, entry in registry.items()
                                            if entity_id in states})
    pflicht = _pflicht_refs(b)
    ziele = {v1_rolle(rolle) for rolle, _ in _schreibziel_rollen(b)}
    ohne_zustand: list[tuple[str, str]] = []
    for feld, ref in _refs(b):
        entity_id = _entity(ref)
        schreibziel = feld in ziele
        if (state := states.get(entity_id)) is None:
            if ref in pflicht:
                raise NichtPruefbar(feld)
            if not _form_domaene(feld, ref):
                return (feld, "nicht_erlaubt"), ohne_zustand
            ohne_zustand.append((feld, ref))
            continue
        eintrag = registry.get(entity_id) or {}
        if not entity_erlaubt(entity_id, eintrag, state, einstellbar) or not (
                schreibziel or _form_erlaubt(feld, ref, eintrag, state, SCHREIB_PLATTFORM[b.hebelsatz])):
            return (feld, "nicht_erlaubt"), ohne_zustand
    return schreibziele_befund(b, registry, beim_start=beim_start), ohne_zustand


def rollen_pruefen(b: HaBindung, ha_api) -> tuple[str, str] | None:
    """Das Urteil aus rollen_befund beim Uebernehmen (beim_start=False): (feld, "nicht_erlaubt") des ersten Verstosses
    oder None; wirft NichtPruefbar wie dort."""
    return rollen_befund(b, ha_api, beim_start=False)[0]


def _mit_ha(b: HaBindung, ha_api) -> tuple[str, str] | None:
    """Der Teil mit HA: (feld, regel) des ersten Verstosses oder None. Bildet keine details (fehler.details kann bei
    einem Programmierfehler ValueError werfen, der in zuordnung_pruefen nicht als „HA nicht erreichbar“ gelten darf)."""
    if not ha_api.is_reachable():  # faehrt HA hoch, fehlen die Entities der Integration noch (404): kein Urteil
        raise NichtPruefbar
    for feld, ref in _refs(b):
        if not ha_api.entity_exists(_entity(ref)):
            return feld, "unbekannt"
    return rollen_pruefen(b, ha_api)  # Positivliste, dann die Schreibziele (wie beim Start)


def _unveraendert(konfiguration, akzeptiert) -> bool:
    return isinstance(akzeptiert, Mapping) and all(
        key in akzeptiert and akzeptiert[key] == konfiguration.get(key) for key in _HA_FELDER)


def zuordnung_pruefen(konfiguration, ha_api, akzeptiert=None) -> dict | None:
    """None = in Ordnung; sonst details (Grund rolle_unzulaessig, Spec Stufe 3 S5 6.1). `akzeptiert` ist die
    uebernommene Konfiguration (None, wenn es keine gibt). Wirft NichtPruefbar, wenn HA gerade nicht antwortet (siehe
    oben). Ein anderer Fehler im Teil ohne HA ausser dem ConfigError der Bindung wird nicht gefangen (Programmfehler,
    bleibt sichtbar); das Dokument kommt dann bei der naechsten Verbindung erneut."""
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
