"""Konfigurationsdokument des HA-Add-ons (Spec 5c 1.3): die HA-Bindung ergibt genau die bisherigen Optionen
(entity_* je Rolle, Raumfuehler, Batterien, Handys, Hinweis-Schalter, Abfrageintervall), damit Binding, Manifest,
Hilfssensoren und Pruefungen des Add-ons unveraendert bleiben (Golden Master). Der Raum-Soll der Regelung kommt aus
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
from collections.abc import Mapping

from heizungsbruecke import config
from heizungsbruecke.ha_signals import SOLL_REF
from smartheat_core.config_check import NichtPruefbar
from smartheat_runtime.options import POLL_INTERVAL_OPTION
from smartheat_runtime.roles import LEVER_ROLES

logger = logging.getLogger(__name__)

_ROOM_TARGET = "room_target"
#: Was die Regelung ausser den Hebeln des Hebelsatzes schreibt (Weishaupt/Viessmann: Betriebsart, Hilfs-Sollwerte).
_WEITERE_SCHREIBZIELE = ("entity_mode_select", "entity_setpoint_comfort", "entity_setpoint_setback")
#: Felder, deren Aenderung eine erneute Pruefung gegen HA braucht.
_HA_FELDER = ("bindung", "raumfuehler", "hebelsatz")


def option_fuer(rolle: str) -> str:
    return f"entity_{LEVER_ROLES.get(rolle, rolle)}"


def _bindung(konfiguration) -> Mapping:
    bindung = konfiguration.get("bindung") if isinstance(konfiguration, Mapping) else None
    return bindung if isinstance(bindung, Mapping) else {}


def soll_entity(konfiguration) -> str | None:
    rollen = _bindung(konfiguration).get("rollen")
    value = rollen.get(_ROOM_TARGET) if isinstance(rollen, Mapping) else None
    return value if isinstance(value, str) and value else None


def optionen(konfiguration) -> dict:
    bindung = _bindung(konfiguration)
    rollen = bindung.get("rollen")
    rollen = rollen if isinstance(rollen, Mapping) else {}
    options: dict = {
        "tenant_id": konfiguration.get("anlage"), "setup_id": konfiguration.get("anlage"),
        "lever_set": konfiguration.get("hebelsatz"), "verteilsystem": konfiguration.get("verteilsystem"),
        "daily_trigger_time": konfiguration.get("tagestick"),
        "room_sensors": list(konfiguration.get("raumfuehler") or []),
        "battery_entities": list(bindung.get("batterien") or []),
        "notify_services": list(bindung.get("benachrichtigung") or []),
        "notify_hints_off": list(bindung.get("hinweise_aus") or []),
    }
    for rolle, ref in rollen.items():
        if rolle != _ROOM_TARGET:
            options[option_fuer(rolle)] = ref
    options["entity_room_target"] = SOLL_REF
    if isinstance(bindung.get("abfrage_s"), int):
        options[POLL_INTERVAL_OPTION] = bindung["abfrage_s"]
    intervalle = konfiguration.get("intervalle")
    if isinstance(intervalle, Mapping):
        if "pruefung_s" in intervalle:
            options["local_check_interval_seconds"] = intervalle["pruefung_s"]
        if "telemetrie_s" in intervalle:
            options["telemetry_interval_seconds"] = intervalle["telemetrie_s"]
    return options


def _entity(ref: str) -> str:
    return ref.partition("::")[0]


def _schreibziele(options: dict, lever_set: str) -> list[str]:
    """Jede Rolle der Bindung, die die Regelung schreibt: die Schreibregeln des Hebelsatzes, alle Hebelrollen (auch die
    Heizkurve, die bei vaillant_vrc720 keine Schreibregel hat) und die Betriebsart/Hilfs-Sollwerte."""
    keys = dict.fromkeys([*config.writable_entity_rules(lever_set), *(option_fuer(r) for r in LEVER_ROLES),
                          *_WEITERE_SCHREIBZIELE])
    return [options[key] for key in keys if options.get(key)]


def _lokal(options: dict, lever_set: str, soll: str | None) -> str | None:
    """Der Teil ohne HA: Pflichtrollen und Entity-Art der Schreibziele."""
    missing = [key for key in config.required_entity_options(lever_set)
               if key != "entity_room_target" and not options.get(key)]
    if missing:
        return f"Für die Ansteuerung {lever_set} fehlt die Zuordnung {missing[0].removeprefix('entity_')}."
    for key, (pattern, expected) in config.writable_entity_rules(lever_set).items():
        value = options.get(key)
        if value is not None and not pattern.fullmatch(value):
            return f"{value} muss {expected} sein."
    if soll is None:
        return "Die Wunschtemperatur-Entity fehlt."
    return None


def _mit_ha(konfiguration, options: dict, lever_set: str, soll: str, ha_api) -> str | None:
    if not ha_api.is_reachable():  # faehrt HA hoch, fehlen die Entities der Integration noch (404): kein Urteil
        raise NichtPruefbar
    registry = {e.get("entity_id"): e for e in ha_api.entity_registry() if isinstance(e, Mapping)}
    refs = [soll, *(v for k, v in options.items() if k.startswith("entity_") and v != SOLL_REF),
            *options["room_sensors"], *options["battery_entities"]]
    for ref in refs:
        if not ha_api.entity_exists(_entity(ref)):
            return f"Die Entity {_entity(ref)} gibt es in Home Assistant nicht."
    kreis = _bindung(konfiguration).get("kreis") or {}
    schreibziele = _schreibziele(options, lever_set)
    for ref in schreibziele:
        entry = registry.get(_entity(ref)) or {}
        # Kreis ohne Config-Entry (config_entry None): nur die Plattform bindet
        if entry.get("platform") != kreis.get("plattform") or (
                kreis.get("config_entry") is not None and entry.get("config_entry_id") != kreis["config_entry"]):
            return f"{_entity(ref)} gehört nicht zur gewählten Heizungs-Integration."
    soll_entry = registry.get(_entity(soll)) or {}
    geraete = {(registry.get(_entity(ref)) or {}).get("device_id") for ref in schreibziele} - {None}
    if _entity(soll) in {_entity(ref) for ref in schreibziele} or (
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
        options = optionen(konfiguration)
        lever_set = config.lever_set_id(options)
        soll = soll_entity(konfiguration)
        text = _lokal(options, lever_set, soll)
    except config.ConfigError as error:
        return str(error)
    if text is not None or soll is None or _unveraendert(konfiguration, akzeptiert):
        return text
    try:
        return _mit_ha(konfiguration, options, lever_set, soll, ha_api)
    except NichtPruefbar:
        raise
    except Exception as error:  # HA nicht erreichbar, unerwartete Antwort: spaeter erneut, nie eine Ablehnung
        logger.warning("Zuordnung vorerst nicht pruefbar (%s)", type(error).__name__)
        raise NichtPruefbar from None
