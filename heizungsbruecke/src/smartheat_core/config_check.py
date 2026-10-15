"""Lokale Pruefung einer Konfiguration vor dem Uebernehmen (Spec 5b 3.1, Weichenstellung 1), rein: nur
Standardbibliothek und smartheat_core. Regeln in dieser Reihenfolge: (1) Aufbau und Wertebereiche, (2) Hebelsatz und
Verteilsystem haben lokale Sicherheitswerte (safety.py), (3) eingerastet: Hebelsatz und Verteilsystem muessen zur Rastung
des Geraets passen (smartheat_core.rastung); eine leere Konfiguration loest sie nicht, (3a) Bindungsparameter gegen die
lokalen Grenzen (smartheat_core.bindungsparameter), (4) erlaubte Referenzen je Rolle, (5) keine Doppelbelegung, (6) Treiberparameter prueft
der Treiber (Rueckruf des Hosts, zuletzt; Gateway: die Bindung, HA: Aufbau der Bindung rein, Entities und Schreibziele ueber den
Rueckruf des Hosts mit der ganzen Konfiguration). Eine leere Konfiguration (anlage None) heisst "nicht eingerichtet" und ist
immer gueltig; sie aendert die Rastung nicht. Die Gruende sind Dokument-Fehlergruende des Vertrags (smartheat_device.wire.DOKUMENT_ERRORS), die Texte
gehen an Portal und Assistent (Kundensprache). Sicherheitsgrenzen, Boost und Notbetrieb liest diese Pruefung nur
(G-1, Regel 4)."""
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from smartheat_core.bindungsparameter import beschreibung
from smartheat_core.rastung import TEXT as RASTUNG_TEXT
from smartheat_core.rastung import Rastung, passt
from smartheat_core.safety import resolve_local_safety

UNGUELTIG = "ungueltig"
HEBELSATZ_EINGERASTET = "hebelsatz_eingerastet"
SICHERHEITSWERTE_FEHLEN = "sicherheitswerte_fehlen"
ROLLE_UNZULAESSIG = "rolle_unzulaessig"
DOPPELT_BELEGT = "doppelt_belegt"
TREIBER_PARAMETER = "treiber_parameter"
GRUENDE = (UNGUELTIG, HEBELSATZ_EINGERASTET, SICHERHEITSWERTE_FEHLEN, ROLLE_UNZULAESSIG, DOPPELT_BELEGT,
           TREIBER_PARAMETER)
HOST_GATEWAY = "gateway"
HOST_HA = "ha"
ABO_WERTE = ("aktiv", "inaktiv")
#: Gateway: Raumfuehler aus Zigbee (Temperatur bzw. Isttemperatur eines Thermostats) oder vom Treiber (SHG G2 3.2).
GATEWAY_RAUMFUEHLER = re.compile(r"zigbee:0x[0-9a-f]{16}:(temperature|local_temperature)|treiber:room_temperature")
IEEE = re.compile(r"0x[0-9a-f]{16}")
TAGESTICK = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
#: Intervalle (nur E2E) mit den Grenzen der Add-on-Optionen (smartheat_runtime.options, TP12e); ein Test haelt sie gleich.
PRUEFUNG_S = (1, 3600)
TELEMETRIE_S = (10, 600)
INTERVALLE = {"pruefung_s": PRUEFUNG_S, "telemetrie_s": TELEMETRIE_S}
RAUMFUEHLER_MAX = 8
#: Home Assistant: Kopie aus smartheat_device.wire (BINDUNG_HA_FIELDS, KREIS_FIELDS, HA_ROLLEN, HA_HINWEISE), ein Test
#: haelt sie gleich; die Pruefung importiert den Geraetekern nicht.
HA_BINDUNG_FELDER = ("kreis", "rollen", "batterien", "benachrichtigung", "hinweise_aus")
KREIS_FELDER = ("plattform", "config_entry", "anlage", "nummer")
HA_ROLLEN = (
    "room_target", "curve", "room_setpoint", "level", "heat_limit", "min_flow", "mode_select", "setpoint_comfort",
    "setpoint_setback", "outdoor_temp", "flow_temperature", "flow_setpoint", "return_temperature", "operating_mode",
    "system_water_pressure", "efficiency_ratio", "generator_hours", "generator_starts", "generator_state",
    "energy_electrical_heating", "energy_electrical_dhw", "energy_primary_heating", "energy_primary_dhw",
    "energy_thermal_heating", "energy_thermal_dhw", "energy_electrical_total",
)
HA_HINWEISE = ("raumfuehler", "batterie", "manueller_eingriff", "quellwechsel", "schreibbudget", "schreibzaehler")
#: Referenzen wie die bisherigen entity_*-Optionen des Add-ons (entity_id oder entity_id::attribut); die Form je Rolle
#: prueft der Host mit der Inventur, hier nur der Aufbau.
HA_REF = re.compile(r"[a-z_]+\.[a-z0-9_]+(::[a-z_]+)?")
HA_RAUMFUEHLER = re.compile(r"sensor\.[a-z0-9_]+|climate\.[a-z0-9_]+::current_temperature")
HA_BATTERIE = re.compile(r"(sensor|binary_sensor)\.[a-z0-9_]+")
HA_DIENST = re.compile(r"notify\.[a-z0-9_]+")
#: Abfrageintervall der Hersteller-Integration (Sekunden), Grenzen 10-3600 s (Teil von V1).
ABFRAGE_S = (10, 3600)
ANLAGE_MAX_CHARS = 64
TEXT_MAX_CHARS = 200

TreiberPruefung = Callable[[dict], str | None]


class NichtPruefbar(Exception):
    """Der Treiber-Rueckruf kann jetzt nicht entscheiden (HA nicht erreichbar, faehrt hoch): weder uebernehmen noch
    ablehnen. Der Server stellt eine abgelehnte Version nie erneut zu; das Geraet haelt das Dokument deshalb zurueck
    und prueft spaeter erneut (smartheat_device.device). Der Text gehoert nicht in Logs (Regel 6)."""


@dataclass(frozen=True)
class Ablehnung:
    grund: str
    text: str


def ist_leer(inhalt) -> bool:
    """Leere Konfiguration (Spec 3.1): `anlage` fehlt oder ist None."""
    return isinstance(inhalt, Mapping) and inhalt.get("anlage") is None


def _text(value, limit: int = TEXT_MAX_CHARS) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit


def _zahl(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _aufbau(neu: Mapping) -> str | None:
    if not _text(neu.get("anlage"), ANLAGE_MAX_CHARS):
        return "Die Anlage fehlt oder ist ungültig."
    for key in ("profil", "hebelsatz", "verteilsystem"):
        if not _text(neu.get(key)):
            return f"Das Feld {key} fehlt oder ist ungültig."
    if not isinstance(neu.get("bindung"), Mapping):
        return "Die Bindung fehlt."
    sensors = neu.get("raumfuehler")
    if not isinstance(sensors, list) or not 0 < len(sensors) <= RAUMFUEHLER_MAX or not all(map(_text, sensors)):
        return f"Es braucht einen bis {RAUMFUEHLER_MAX} Raumfühler."
    tick = neu.get("tagestick")
    if not isinstance(tick, str) or not TAGESTICK.fullmatch(tick):
        return "Die Zeit des Tagesticks fehlt oder ist keine Uhrzeit (HH:MM)."
    if neu.get("abo") not in ABO_WERTE:
        return "Der Abo-Status fehlt."
    abfrage = neu["bindung"].get("abfrage_s")
    if abfrage is not None and not (isinstance(abfrage, int) and not isinstance(abfrage, bool)
                                    and ABFRAGE_S[0] <= abfrage <= ABFRAGE_S[1]):
        return f"Das Abfrageintervall muss zwischen {ABFRAGE_S[0]} und {ABFRAGE_S[1]} Sekunden liegen."
    intervalle = neu.get("intervalle")
    if intervalle is not None:
        if not isinstance(intervalle, Mapping) or not set(intervalle) <= set(INTERVALLE):
            return "Die Intervalle sind ungültig."
        for key, (low, high) in INTERVALLE.items():
            value = intervalle.get(key)
            if value is not None and not (_zahl(value) and low <= value <= high):
                return f"Das Intervall {key} muss zwischen {low} und {high} Sekunden liegen."
    return None


def _gateway_bindung(neu: Mapping) -> Ablehnung | None:
    bindung = neu["bindung"]
    treiber = bindung.get("treiber")
    if not (isinstance(treiber, Mapping) and _text(treiber.get("id")) and isinstance(treiber.get("parameter"), Mapping)):
        return Ablehnung(ROLLE_UNZULAESSIG, "Die Bindung braucht einen Treiber mit Parametern.")
    thermostat = bindung.get("thermostat")
    if thermostat is not None and not (isinstance(thermostat, str) and IEEE.fullmatch(thermostat)):
        return Ablehnung(ROLLE_UNZULAESSIG, "Das Thermostat ist kein Zigbee-Gerät.")
    seen: set[str] = set()
    for ref in neu["raumfuehler"]:
        if not GATEWAY_RAUMFUEHLER.fullmatch(ref):
            return Ablehnung(ROLLE_UNZULAESSIG, f"Raumfühler {ref} ist für das Gateway nicht zulässig.")
        if ref in seen:
            return Ablehnung(DOPPELT_BELEGT, f"Raumfühler {ref} ist doppelt zugeordnet.")
        seen.add(ref)
    return None


def _ha_bindung(neu: Mapping) -> Ablehnung | None:
    bindung = neu["bindung"]
    if not all(key in bindung for key in HA_BINDUNG_FELDER):
        return Ablehnung(ROLLE_UNZULAESSIG, "Die Zuordnung für Home Assistant ist unvollständig.")
    kreis = bindung["kreis"]
    # config_entry darf fehlen oder None sein (Server: Kreis ohne Config-Entry), die uebrigen Felder nicht.
    if not isinstance(kreis, Mapping) or not all(
            isinstance(kreis.get(key), str) and kreis[key] for key in KREIS_FELDER if key != "config_entry"):
        return Ablehnung(ROLLE_UNZULAESSIG, "Der Heizkreis der Zuordnung ist unvollständig.")
    rollen = bindung["rollen"]
    if not isinstance(rollen, Mapping) or "room_target" not in rollen:
        return Ablehnung(ROLLE_UNZULAESSIG, "Die Wunschtemperatur-Entity fehlt.")
    for rolle, ref in rollen.items():
        if rolle not in HA_ROLLEN or not isinstance(ref, str) or not HA_REF.fullmatch(ref):
            return Ablehnung(ROLLE_UNZULAESSIG, f"Die Rolle {rolle} ist nicht zulässig zugeordnet.")
    seen: set[str] = set()
    for ref in neu["raumfuehler"]:
        if not HA_RAUMFUEHLER.fullmatch(ref):
            return Ablehnung(ROLLE_UNZULAESSIG, f"Raumfühler {ref} ist für Home Assistant nicht zulässig.")
        if ref in seen:
            return Ablehnung(DOPPELT_BELEGT, f"Raumfühler {ref} ist doppelt zugeordnet.")
        seen.add(ref)
    werte = list(rollen.values())
    if len(set(werte)) != len(werte):
        return Ablehnung(DOPPELT_BELEGT, "Eine Entity ist mehreren Rollen zugeordnet.")
    for key, pattern in (("batterien", HA_BATTERIE), ("benachrichtigung", HA_DIENST)):
        values = bindung[key]
        if not isinstance(values, list) or not all(isinstance(v, str) and pattern.fullmatch(v) for v in values):
            return Ablehnung(ROLLE_UNZULAESSIG, f"Das Feld {key} ist ungültig.")
    hinweise = bindung["hinweise_aus"]
    if not isinstance(hinweise, list) or not all(isinstance(h, str) for h in hinweise) or not set(hinweise) <= set(
            HA_HINWEISE):
        return Ablehnung(ROLLE_UNZULAESSIG, "Unbekannter Hinweis-Schalter.")
    return None


def pruefen(neu: Mapping, rastung: Rastung | None, *, host: str,
            treiber_pruefen: TreiberPruefung | None = None) -> Ablehnung | None:
    """None = uebernehmen; sonst der erste verletzte Grund. `rastung` ist die Rastung des Geraets (None: noch keine)."""
    if not isinstance(neu, Mapping):
        return Ablehnung(UNGUELTIG, "Die Konfiguration ist kein Objekt.")
    if ist_leer(neu):
        return None
    problem = _aufbau(neu)
    if problem is not None:
        return Ablehnung(UNGUELTIG, problem)
    try:
        resolve_local_safety(neu["hebelsatz"], neu["verteilsystem"])
    except ValueError as error:
        return Ablehnung(SICHERHEITSWERTE_FEHLEN, f"Für diese Anlage fehlen lokale Sicherheitswerte ({error}).")
    if not passt(rastung, neu):
        return Ablehnung(HEBELSATZ_EINGERASTET, RASTUNG_TEXT)
    try:
        beschreibung(neu["hebelsatz"], neu.get("bindungsparameter"))
    except ValueError as error:
        return Ablehnung(UNGUELTIG, f"Bindungsparameter: {error}")
    if host == HOST_HA:
        ablehnung = _ha_bindung(neu)
        if ablehnung is None and treiber_pruefen is not None and (text := treiber_pruefen(dict(neu))):
            ablehnung = Ablehnung(ROLLE_UNZULAESSIG, text)
        return ablehnung
    if host != HOST_GATEWAY:
        return Ablehnung(ROLLE_UNZULAESSIG, "Unbekannter Host.")
    ablehnung = _gateway_bindung(neu)
    if ablehnung is not None:
        return ablehnung
    if treiber_pruefen is not None:
        text = treiber_pruefen(dict(neu["bindung"]))
        if text:
            return Ablehnung(TREIBER_PARAMETER, text)
    return None
