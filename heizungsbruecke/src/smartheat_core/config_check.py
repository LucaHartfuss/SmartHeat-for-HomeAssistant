"""Lokale Pruefung einer Konfiguration vor dem Uebernehmen (Spec 5b 3.1, Weichenstellung 1), rein: nur
Standardbibliothek und smartheat_core. Regeln in dieser Reihenfolge: (1) Aufbau und Wertebereiche, (2) Hebelsatz und
Verteilsystem haben lokale Sicherheitswerte (safety.py), (3) eingerastet: Hebelsatz und Verteilsystem muessen zur Rastung
des Geraets passen (smartheat_core.rastung); eine leere Konfiguration loest sie nicht, (3a) Bindungsparameter gegen die
lokalen Grenzen (smartheat_core.bindungsparameter), (4) erlaubte Referenzen je Rolle, (5) keine Doppelbelegung, (6) Treiberparameter prueft
der Treiber (Rueckruf des Hosts, zuletzt; Gateway: die Bindung, HA: Aufbau der Bindung rein, Entities und Schreibziele ueber den
Rueckruf des Hosts mit der ganzen Konfiguration). Eine leere Konfiguration (anlage None) heisst "nicht eingerichtet" und ist
immer gueltig; sie aendert die Rastung nicht. Die Gruende sind Dokument-Fehlergruende des Vertrags (smartheat_device.wire.DOKUMENT_ERRORS). Ablehnungen
tragen Grund und details (Feld, Regel, Zahlen; Spec Stufe 3 S5 6.1), den Text bildet der Server. Sicherheitsgrenzen, Boost und Notbetrieb liest diese Pruefung nur
(G-1, Regel 4)."""
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from smartheat_core.bindungsparameter import Fehler as BindungsparameterFehler
from smartheat_core.bindungsparameter import beschreibung
from smartheat_core.levers import LEVER_SETS
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
#: Intervalle (nur E2E); die Grenzen stehen nur hier, die Laufzeit uebernimmt jeden angenommenen Wert (ein Test
#: startet sie an beiden Grenzen). Obergrenze Telemetrie (Regel 4, TP12e, AU-022): der Server wertet Luecken ab
#: samples.MAX_GAP (15 min) als Pause; 600 s lassen Reserve (Contract-Check 33: <= 0,8 x MAX_GAP).
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

#: Kopie aus smartheat_device.wire (PRUEF_REGELN), ein Test haelt sie gleich; Feldnamen sind Namen aus
#: wire.PRUEF_FELDER (ein Test prueft jede Ablehnung).
PRUEF_REGELN = ("fehlt", "ungueltig", "anzahl", "bereich", "format", "doppelt", "unbekannt", "nicht_erlaubt")

TreiberPruefung = Callable[[dict], dict | None]


class NichtPruefbar(Exception):
    """Der Treiber-Rueckruf kann jetzt nicht entscheiden (HA nicht erreichbar, faehrt hoch): weder uebernehmen noch
    ablehnen. Der Server stellt eine abgelehnte Version nie erneut zu; das Geraet haelt das Dokument deshalb zurueck
    und prueft spaeter erneut (smartheat_device.device). Der Text gehoert nicht in Logs (Regel 6)."""


@dataclass(frozen=True)
class Ablehnung:
    """Grund aus wire.DOKUMENT_ERRORS und details (wire.DETAIL_FIELDS); keine Texte (Spec Stufe 3 S5 6.2)."""
    grund: str
    details: dict | None = None


def befund(feld: str | None = None, regel: str | None = None, **zahlen: float) -> dict:
    assert regel is None or regel in PRUEF_REGELN, regel
    return {**({"feld": feld} if feld else {}), **({"regel": regel} if regel else {}), **zahlen}


def ist_leer(inhalt) -> bool:
    """Leere Konfiguration (Spec 3.1): `anlage` fehlt oder ist None."""
    return isinstance(inhalt, Mapping) and inhalt.get("anlage") is None


def _text(value, limit: int = TEXT_MAX_CHARS) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit


def _zahl(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _aufbau(neu: Mapping) -> dict | None:
    """details des ersten Verstosses gegen Aufbau und Wertebereiche (Grund ungueltig), sonst None."""
    if not _text(neu.get("anlage"), ANLAGE_MAX_CHARS):
        return befund("anlage", "ungueltig")
    for key in ("profil", "hebelsatz", "verteilsystem"):
        if not _text(neu.get(key)):
            return befund(key, "ungueltig")
    if not isinstance(neu.get("bindung"), Mapping):
        return befund("bindung", "fehlt")
    sensors = neu.get("raumfuehler")
    if not isinstance(sensors, list) or not all(map(_text, sensors)):
        return befund("raumfuehler", "ungueltig")
    if not 0 < len(sensors) <= RAUMFUEHLER_MAX:
        return befund("raumfuehler", "anzahl", min=1, max=RAUMFUEHLER_MAX, anzahl=len(sensors))
    tick = neu.get("tagestick")
    if not isinstance(tick, str) or not TAGESTICK.fullmatch(tick):
        return befund("tagestick", "format")
    if neu.get("abo") not in ABO_WERTE:
        return befund("abo", "unbekannt")
    abfrage = neu["bindung"].get("abfrage_s")
    if abfrage is not None and not (isinstance(abfrage, int) and not isinstance(abfrage, bool)
                                    and ABFRAGE_S[0] <= abfrage <= ABFRAGE_S[1]):
        return befund("abfrage_s", "bereich", min=ABFRAGE_S[0], max=ABFRAGE_S[1])
    intervalle = neu.get("intervalle")
    if intervalle is not None:
        if not isinstance(intervalle, Mapping) or not set(intervalle) <= set(INTERVALLE):
            return befund("intervalle", "ungueltig")
        for key, (low, high) in INTERVALLE.items():
            value = intervalle.get(key)
            if value is not None and not (_zahl(value) and low <= value <= high):
                return befund(key, "bereich", min=low, max=high)
    return None


def _gateway_bindung(neu: Mapping) -> Ablehnung | None:
    bindung = neu["bindung"]
    treiber = bindung.get("treiber")
    if not (isinstance(treiber, Mapping) and _text(treiber.get("id")) and isinstance(treiber.get("parameter"), Mapping)):
        return Ablehnung(ROLLE_UNZULAESSIG, befund("treiber", "ungueltig"))
    thermostat = bindung.get("thermostat")
    if thermostat is not None and not (isinstance(thermostat, str) and IEEE.fullmatch(thermostat)):
        return Ablehnung(ROLLE_UNZULAESSIG, befund("thermostat", "format"))
    seen: set[str] = set()
    for ref in neu["raumfuehler"]:
        if not GATEWAY_RAUMFUEHLER.fullmatch(ref):
            return Ablehnung(ROLLE_UNZULAESSIG, befund("raumfuehler", "nicht_erlaubt"))
        if ref in seen:
            return Ablehnung(DOPPELT_BELEGT, befund("raumfuehler", "doppelt"))
        seen.add(ref)
    return None


def _ha_bindung(neu: Mapping) -> Ablehnung | None:
    bindung = neu["bindung"]
    fehlend = next((key for key in HA_BINDUNG_FELDER if key not in bindung), None)
    if fehlend is not None:
        return Ablehnung(ROLLE_UNZULAESSIG, befund(fehlend, "fehlt"))
    kreis = bindung["kreis"]
    # config_entry darf fehlen oder None sein (Server: Kreis ohne Config-Entry), die uebrigen Felder nicht.
    if not isinstance(kreis, Mapping) or not all(
            isinstance(kreis.get(key), str) and kreis[key] for key in KREIS_FELDER if key != "config_entry"):
        return Ablehnung(ROLLE_UNZULAESSIG, befund("kreis", "ungueltig"))
    rollen = bindung["rollen"]
    if not isinstance(rollen, Mapping):
        return Ablehnung(ROLLE_UNZULAESSIG, befund("rollen", "ungueltig"))
    if "room_target" not in rollen:
        return Ablehnung(ROLLE_UNZULAESSIG, befund("room_target", "fehlt"))
    for rolle, ref in rollen.items():
        if rolle not in HA_ROLLEN:
            return Ablehnung(ROLLE_UNZULAESSIG, befund("rollen", "unbekannt"))  # der fremde Name geht nicht hoch
        if not isinstance(ref, str) or not HA_REF.fullmatch(ref):
            return Ablehnung(ROLLE_UNZULAESSIG, befund(rolle, "format"))
    seen: set[str] = set()
    for ref in neu["raumfuehler"]:
        if not HA_RAUMFUEHLER.fullmatch(ref):
            return Ablehnung(ROLLE_UNZULAESSIG, befund("raumfuehler", "format"))
        if ref in seen:
            return Ablehnung(DOPPELT_BELEGT, befund("raumfuehler", "doppelt"))
        seen.add(ref)
    werte = list(rollen.values())
    if len(set(werte)) != len(werte):
        return Ablehnung(DOPPELT_BELEGT, befund("rollen", "doppelt"))
    for key, pattern in (("batterien", HA_BATTERIE), ("benachrichtigung", HA_DIENST)):
        values = bindung[key]
        if not isinstance(values, list) or not all(isinstance(v, str) and pattern.fullmatch(v) for v in values):
            return Ablehnung(ROLLE_UNZULAESSIG, befund(key, "format"))
    hinweise = bindung["hinweise_aus"]
    if not isinstance(hinweise, list) or not all(isinstance(h, str) for h in hinweise) or not set(hinweise) <= set(
            HA_HINWEISE):
        return Ablehnung(ROLLE_UNZULAESSIG, befund("hinweise_aus", "unbekannt"))
    return None


def pruefen(neu: Mapping, rastung: Rastung | None, *, host: str,
            treiber_pruefen: TreiberPruefung | None = None) -> Ablehnung | None:
    """None = uebernehmen; sonst der erste verletzte Grund. `rastung` ist die Rastung des Geraets (None: noch keine)."""
    if not isinstance(neu, Mapping):
        return Ablehnung(UNGUELTIG)
    if ist_leer(neu):
        return None
    problem = _aufbau(neu)
    if problem is not None:
        return Ablehnung(UNGUELTIG, problem)
    try:
        resolve_local_safety(neu["hebelsatz"], neu["verteilsystem"])
    except ValueError:
        feld = "hebelsatz" if neu["hebelsatz"] not in LEVER_SETS else "verteilsystem"
        return Ablehnung(SICHERHEITSWERTE_FEHLEN, befund(feld, "unbekannt"))
    if not passt(rastung, neu):
        return Ablehnung(HEBELSATZ_EINGERASTET)
    try:
        beschreibung(neu["hebelsatz"], neu.get("bindungsparameter"))
    except BindungsparameterFehler as error:
        return Ablehnung(UNGUELTIG, error.details)
    if host == HOST_HA:
        ablehnung = _ha_bindung(neu)
        if ablehnung is None and treiber_pruefen is not None and (details := treiber_pruefen(dict(neu))) is not None:
            ablehnung = Ablehnung(ROLLE_UNZULAESSIG, details)
        return ablehnung
    if host != HOST_GATEWAY:
        return Ablehnung(ROLLE_UNZULAESSIG)
    ablehnung = _gateway_bindung(neu)
    if ablehnung is not None:
        return ablehnung
    if treiber_pruefen is not None and (details := treiber_pruefen(dict(neu["bindung"]))) is not None:
        return Ablehnung(TREIBER_PARAMETER, details)
    return None
