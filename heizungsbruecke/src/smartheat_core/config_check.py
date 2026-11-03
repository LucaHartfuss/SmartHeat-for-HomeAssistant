"""Lokale Pruefung einer Konfiguration vor dem Uebernehmen (Weichenstellung 1), rein: nur
Standardbibliothek und smartheat_core. Das Dokument fuehrt anlagenweit Anlage, Weg, Tagestick und Bindungsparameter und
je Kreis (hoechstens KREISE_MAX) Bindung, Hebelsatz, Verteilsystem, Abo und Raeume; alle Kreise einer Anlage haben
denselben Hebelsatz. Regeln in dieser Reihenfolge: (1) Aufbau und Wertebereiche, (2) Hebelsatz und
Verteilsystem haben lokale Sicherheitswerte (safety.py), (3) eingerastet: Hebelsatz und Verteilsystem muessen zur Rastung
des Geraets passen (smartheat_core.rastung), nach der Pruefung der Bindungen auch die Schreibziele und die Soll-Entity
je Kreis; eine leere Konfiguration loest sie nicht, (3a) Bindungsparameter gegen die
lokalen Grenzen (smartheat_core.bindungsparameter), (4) erlaubte Referenzen je Rolle, (4a) der Weg der Anlage ist ein
bekannter und der aller Kreise (weg_unbekannt, weg_mehrfach), (5) keine Doppelbelegung (innerhalb eines Kreises und
Schreibziele wie Fuehler getrennt ueber die Kreise), (6) Treiberparameter prueft
der Treiber (Rueckruf des Hosts, zuletzt, je Kreis; Gateway: die Bindung, HA: Aufbau der Bindung rein, Entities und
Schreibziele ueber den Rueckruf des Hosts mit der flachen Sicht des Kreises, `raumfuehler` sind dort die Fuehler aller
Raeume). Die Regelung liest nur die Fuehler der Referenzraeume (smartheat_core.kreisansicht); Kontrollraeume sind nur
Anzeige und beeinflussen sie nie. Eine leere Konfiguration (anlage None) heisst "nicht eingerichtet" und ist
immer gueltig; sie aendert die Rastung nicht. Die Gruende sind Dokument-Fehlergruende des Vertrags (smartheat_device.wire.DOKUMENT_ERRORS). Ablehnungen
tragen Grund und details (Feld, Regel, Zahlen), den Text bildet der Server. Sicherheitsgrenzen, Boost und Notbetrieb liest diese Pruefung nur
(G-1, Regel 4). V1 ist strikt (R10): fremde Felder in Bindung, Treiber und Kreis lehnt diese Pruefung ab; die
oberste Ebene der Konfiguration, die Software und die Bedienung prueft smartheat_device.documents gegen den
Vertrag."""
import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from smartheat_core import kreisansicht
from smartheat_core.bindungsparameter import Fehler as BindungsparameterFehler
from smartheat_core.bindungsparameter import beschreibung
from smartheat_core.levers import LEVER_SETS
from smartheat_core.rastung import Rastung, hebel_passt, passt
from smartheat_core.safety import resolve_local_safety
from smartheat_core.verfahren import WegUnbekannt, verfahren_fuer
from smartheat_core.zahlen import ist_endliche_zahl

logger = logging.getLogger(__name__)

UNGUELTIG = "ungueltig"
HEBELSATZ_EINGERASTET = "hebelsatz_eingerastet"
SICHERHEITSWERTE_FEHLEN = "sicherheitswerte_fehlen"
ROLLE_UNZULAESSIG = "rolle_unzulaessig"
DOPPELT_BELEGT = "doppelt_belegt"
TREIBER_PARAMETER = "treiber_parameter"
WEG_UNBEKANNT = "weg_unbekannt"
WEG_MEHRFACH = "weg_mehrfach"
GRUENDE = (UNGUELTIG, HEBELSATZ_EINGERASTET, SICHERHEITSWERTE_FEHLEN, ROLLE_UNZULAESSIG, DOPPELT_BELEGT,
           TREIBER_PARAMETER, WEG_UNBEKANNT, WEG_MEHRFACH)
HOST_GATEWAY = "gateway"
HOST_HA = "ha"
ABO_WERTE = ("aktiv", "inaktiv")  # Kopie aus wire.ABO (test_core_vertrag_pin.py)
#: Gateway: Raumfuehler aus Zigbee (Temperatur bzw. Isttemperatur eines Thermostats) oder vom Treiber.
GATEWAY_RAUMFUEHLER = re.compile(r"zigbee:0x[0-9a-f]{16}:(temperature|local_temperature)|treiber:room_temperature")
IEEE = re.compile(r"0x[0-9a-f]{16}")
TAGESTICK = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
#: Intervalle (nur E2E, Umgebung SMARTHEAT_E2E_INTERVALLE, smartheat_runtime.dokument_config; kein
#: Dokumentfeld); die Grenzen stehen nur hier, die Laufzeit uebernimmt jeden Wert darin (ein Test startet sie an
#: beiden Grenzen). Obergrenze Telemetrie (Regel 4): der Server wertet Luecken ab
#: samples.MAX_GAP (15 min) als Pause; 600 s lassen Reserve (Contract-Check (Vertrag Domaene): <= 0,8 x
#: MAX_GAP).
PRUEFUNG_S = (1, 3600)
TELEMETRIE_S = (10, 600)
INTERVALLE = {"pruefung_s": PRUEFUNG_S, "telemetrie_s": TELEMETRIE_S}
RAUMFUEHLER_MAX = 8  # Fuehler der Referenzraeume je Kreis
#: Kopie aus smartheat_device.wire (KREISE_MAX, RAEUME_MAX, RAUM_FUEHLER_MAX, RAUM_ROLLEN, KONFIGURATION_KREIS_FIELDS,
#: KONFIGURATION_KREIS_OPTIONAL, KONFIGURATION_RAUM_FIELDS, WEGE, WEG_DER_PLATTFORM, WEG_DES_TREIBERS), ebenfalls in
#: test_core_vertrag_pin.py gehalten. Hoechstens vier Kreise je Geraet (Nutzer-Entscheidung, Regel 4).
KREISE_MAX = 4
RAEUME_MAX, RAUM_FUEHLER_MAX = 8, 8
#: Kopie aus smartheat_device.wire (RAUM_SOLL_MIN/MAX und RAUM_VERSATZ_MAX), ebenfalls in test_core_vertrag_pin.py
#: gehalten: festes Ziel eines Kontrollraums 15 bis 25 Grad, Versatz eines Referenzraums -3 bis +3 K (Regel 4).
RAUM_ZIEL_MIN, RAUM_ZIEL_MAX = 15.0, 25.0
RAUM_VERSATZ_MAX = 3.0
KREIS_NUMMER_MAX = 2**31 - 1  # Kreis- und Raumnummern (wire.KREIS_MAX)
RAUM_ROLLEN = ("referenz", "kontroll")
KONFIG_KREIS_FELDER = ("kreis", "name", "bindung", "raeume", "hebelsatz", "verteilsystem", "abo")
KONFIG_KREIS_OPTIONAL = ("ursprungswerte",)
RAUM_FELDER = ("raum", "name", "rolle", "fuehler", "soll_quelle", "versatz_k", "ziel_fest")
#: Anbindungswege, die ein Geraet heute ausfuehrt (`simulation`: Testweg), und ihre Abbildung aus der Bindung.
WEGE = ("vaillant_myvaillant", "viessmann_vicare", "weishaupt_modbus", "simulation")
WEG_DER_PLATTFORM = {"mypyllant": "vaillant_myvaillant", "vicare": "viessmann_vicare",
                     "weishaupt_modbus": "weishaupt_modbus"}
WEG_DES_TREIBERS = {"vicare_cloud": "viessmann_vicare", "simulation": "simulation"}
#: Gateway: Kopie aus smartheat_device.wire (BINDUNG_GATEWAY_FIELDS, TREIBER_FIELDS); test_core_vertrag_pin.py haelt sie
#: gleich, die Pruefung importiert den Geraetekern nicht. Die Bindung hat genau diese Felder (V1 ist strikt, R10).
GATEWAY_BINDUNG_FELDER = ("treiber", "thermostat")
TREIBER_FELDER = ("id", "parameter")
#: Home Assistant: Kopie aus smartheat_device.wire (BINDUNG_HA_FIELDS, BINDUNG_HA_OPTIONAL, KREIS_FIELDS, KREIS_OPTIONAL,
#: HA_ROLLEN, HA_HINWEISE), ebenfalls in test_core_vertrag_pin.py gehalten.
HA_BINDUNG_FELDER = ("kreis", "rollen", "batterien", "benachrichtigung", "hinweise_aus")
HA_BINDUNG_OPTIONAL = ("abfrage_s", "einheiten")
KREIS_FELDER = ("plattform", "anlage", "nummer")
KREIS_OPTIONAL = ("config_entry",)
HA_ROLLEN = (
    "room_target", "curve", "room_setpoint", "level", "heat_limit", "min_flow", "mode_select", "setpoint_comfort",
    "setpoint_setback", "outdoor_temp", "flow_temperature", "flow_setpoint", "return_temperature", "operating_mode",
    "system_water_pressure", "efficiency_ratio", "generator_hours", "generator_starts", "generator_state",
    "energy_electrical_heating", "energy_electrical_dhw", "energy_primary_heating", "energy_primary_dhw",
    "energy_thermal_heating", "energy_thermal_dhw", "energy_electrical_total",
)
HA_HINWEISE = ("raumfuehler", "batterie", "manueller_eingriff", "quellwechsel", "schreibbudget", "schreibzaehler")
#: Referenzen wie die bisherigen entity_*-Optionen des Add-ons (entity_id oder entity_id::attribut); als Attribut
#: lesen und schreiben die Rollen nur die Temperaturen (sonst liesse sich jedes Attribut einer Entity binden,
#: etwa Laengen- und Breitengrad einer Zone). Die Form je Rolle prueft der Host mit der Inventur, hier nur der Aufbau.
HA_REF = re.compile(r"[a-z_]+\.[a-z0-9_]+(::(temperature|current_temperature))?")
HA_RAUMFUEHLER = re.compile(r"sensor\.[a-z0-9_]+|climate\.[a-z0-9_]+::current_temperature")
HA_BATTERIE = re.compile(r"(sensor|binary_sensor)\.[a-z0-9_]+")
#: Benachrichtigungsdienste: nur die Handy-Dienste der Companion-App; notify.email, notify.telegram u. a. nicht.
HA_DIENST = re.compile(r"notify\.mobile_app_[a-z0-9_]+")
#: Abfrageintervall der Hersteller-Integration (Sekunden), Grenzen 10-3600 s (Teil von V1).
ABFRAGE_S = (10, 3600)
ANLAGE_MAX_CHARS = 64
NAME_MAX_CHARS = 80  # Anlagenname und Kreisname
RAUM_NAME_MAX_CHARS = 40
TEXT_MAX_CHARS = 200

#: Kopie aus smartheat_device.wire (PRUEF_REGELN), test_core_vertrag_pin.py haelt sie gleich; Feldnamen sind Namen aus
#: wire.PRUEF_FELDER (ein Test prueft jede Ablehnung).
PRUEF_REGELN = ("fehlt", "ungueltig", "anzahl", "bereich", "format", "doppelt", "unbekannt", "nicht_erlaubt")

TreiberPruefung = Callable[[dict], dict | None]


class NichtPruefbar(Exception):
    """Der Treiber-Rueckruf kann jetzt nicht entscheiden (HA nicht erreichbar, faehrt hoch): weder uebernehmen noch
    ablehnen. Der Server stellt eine abgelehnte Version nie erneut zu; das Geraet haelt das Dokument deshalb zurueck
    und prueft spaeter erneut (smartheat_device.device). Der Text gehoert nicht in Logs (Regel 6). Optional traegt die
    Ausnahme das Feld (eine Rolle der Bindung, nie eine Entity), damit der Start einen lesbaren Text bilden kann."""


@dataclass(frozen=True)
class Ablehnung:
    """Grund aus wire.DOKUMENT_ERRORS und details (wire.DETAIL_FIELDS); keine Texte."""
    grund: str
    details: dict | None = None


def befund(feld: str | None = None, regel: str | None = None, **zahlen: float) -> dict:
    assert regel is None or regel in PRUEF_REGELN, regel
    return {**({"feld": feld} if feld else {}), **({"regel": regel} if regel else {}), **zahlen}


def ist_leer(inhalt) -> bool:
    """Leere Konfiguration: `anlage` fehlt oder ist None."""
    return isinstance(inhalt, Mapping) and inhalt.get("anlage") is None


def _text(value, limit: int = TEXT_MAX_CHARS) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit


def _zahl(wert) -> bool:
    return ist_endliche_zahl(wert)


def _nummer(wert) -> bool:
    return isinstance(wert, int) and not isinstance(wert, bool) and 1 <= wert <= KREIS_NUMMER_MAX


def _raum_aufbau(raum, gesehen: set[int]) -> dict | None:
    if not isinstance(raum, Mapping) or set(raum) != set(RAUM_FELDER):
        return befund("raeume", "ungueltig")
    if not _nummer(raum["raum"]):
        return befund("raum", "ungueltig")
    if raum["raum"] in gesehen:
        return befund("raum", "doppelt")
    gesehen.add(raum["raum"])
    if not _text(raum["name"], RAUM_NAME_MAX_CHARS):
        return befund("name", "ungueltig")
    if raum["rolle"] not in RAUM_ROLLEN:
        return befund("rolle", "unbekannt")
    fuehler = raum["fuehler"]
    if not isinstance(fuehler, list) or not all(map(_text, fuehler)):
        return befund("fuehler", "ungueltig")
    if len(fuehler) > RAUM_FUEHLER_MAX:
        return befund("fuehler", "anzahl", min=0, max=RAUM_FUEHLER_MAX, anzahl=len(fuehler))
    referenz = raum["rolle"] == kreisansicht.ROLLE_REFERENZ
    if not isinstance(raum["soll_quelle"], bool):
        return befund("soll_quelle", "ungueltig")
    if raum["soll_quelle"] and not referenz:
        return befund("soll_quelle", "nicht_erlaubt")
    if not _zahl(raum["versatz_k"]):
        return befund("versatz_k", "ungueltig")
    if not -RAUM_VERSATZ_MAX <= raum["versatz_k"] <= RAUM_VERSATZ_MAX:
        return befund("versatz_k", "bereich", min=-RAUM_VERSATZ_MAX, max=RAUM_VERSATZ_MAX)
    if raum["versatz_k"] != 0 and (not referenz or raum["soll_quelle"]):
        return befund("versatz_k", "nicht_erlaubt")
    if raum["ziel_fest"] is not None and not _zahl(raum["ziel_fest"]):
        return befund("ziel_fest", "ungueltig")
    if raum["ziel_fest"] is not None and not RAUM_ZIEL_MIN <= raum["ziel_fest"] <= RAUM_ZIEL_MAX:
        return befund("ziel_fest", "bereich", min=RAUM_ZIEL_MIN, max=RAUM_ZIEL_MAX)
    if raum["ziel_fest"] is not None and referenz:
        return befund("ziel_fest", "nicht_erlaubt")
    return None


def _kreis_aufbau(eintrag, vorige: int) -> dict | None:
    if not isinstance(eintrag, Mapping):
        return befund("kreise", "ungueltig")
    if set(eintrag) - set(KONFIG_KREIS_FELDER) - set(KONFIG_KREIS_OPTIONAL):
        return befund("kreise", "unbekannt")  # der fremde Name geht nicht hoch
    fehlend = next((key for key in KONFIG_KREIS_FELDER if key not in eintrag), None)
    if fehlend is not None:
        return befund(fehlend, "fehlt")
    if not _nummer(eintrag["kreis"]):
        return befund("kreis", "ungueltig")
    if eintrag["kreis"] <= vorige:
        return befund("kreis", "doppelt" if eintrag["kreis"] == vorige else "ungueltig")
    if not _text(eintrag["name"], NAME_MAX_CHARS):
        return befund("name", "ungueltig")
    for key in ("hebelsatz", "verteilsystem"):
        if not _text(eintrag[key]):
            return befund(key, "ungueltig")
    if not isinstance(eintrag["bindung"], Mapping):
        return befund("bindung", "fehlt")
    if eintrag["abo"] not in ABO_WERTE:
        return befund("abo", "unbekannt")
    abfrage = eintrag["bindung"].get("abfrage_s")
    if abfrage is not None and not (isinstance(abfrage, int) and not isinstance(abfrage, bool)
                                    and ABFRAGE_S[0] <= abfrage <= ABFRAGE_S[1]):
        return befund("abfrage_s", "bereich", min=ABFRAGE_S[0], max=ABFRAGE_S[1])
    raeume = eintrag["raeume"]
    if not isinstance(raeume, list):
        return befund("raeume", "ungueltig")
    if not 0 < len(raeume) <= RAEUME_MAX:
        return befund("raeume", "anzahl", min=1, max=RAEUME_MAX, anzahl=len(raeume))
    gesehen: set[int] = set()
    for raum in raeume:
        if problem := _raum_aufbau(raum, gesehen):
            return problem
    anzahl = len(kreisansicht.referenz_fuehler(eintrag))
    if not 0 < anzahl <= RAUMFUEHLER_MAX:
        return befund("fuehler", "anzahl", min=1, max=RAUMFUEHLER_MAX, anzahl=anzahl)
    return None


def _aufbau(neu: Mapping) -> dict | None:
    """details des ersten Verstosses gegen Aufbau und Wertebereiche (Grund ungueltig), sonst None."""
    if not _text(neu.get("anlage"), ANLAGE_MAX_CHARS):
        return befund("anlage", "ungueltig")
    if not _text(neu.get("anlage_name"), NAME_MAX_CHARS):
        return befund("anlage_name", "ungueltig")
    if not _text(neu.get("weg")):
        return befund("weg", "ungueltig")
    tick = neu.get("tagestick")
    if not isinstance(tick, str) or not TAGESTICK.fullmatch(tick):
        return befund("tagestick", "format")
    if neu.get("mindest_integration") is not None and not _text(neu["mindest_integration"]):
        return befund("mindest_integration", "ungueltig")
    kreise = neu.get("kreise")
    if not isinstance(kreise, list):
        return befund("kreise", "ungueltig")
    if not 0 < len(kreise) <= KREISE_MAX:
        return befund("kreise", "anzahl", min=1, max=KREISE_MAX, anzahl=len(kreise))
    vorige = 0
    for eintrag in kreise:
        if problem := _kreis_aufbau(eintrag, vorige):
            return problem
        vorige = eintrag["kreis"]
    if len({eintrag["hebelsatz"] for eintrag in kreise}) > 1:  # anlagenweit ein Hebelsatz
        return befund("hebelsatz", "ungueltig")
    return None


def _fremd(inhalt: Mapping, erlaubt: tuple[str, ...], feld: str) -> Ablehnung | None:
    """V1 ist strikt (R10): ein Feld ausserhalb von `erlaubt` lehnt ab. Gemeldet wird das Feld, in dem es steht; der
    fremde Name geht nicht hoch."""
    if set(inhalt) - set(erlaubt):
        return Ablehnung(ROLLE_UNZULAESSIG, befund(feld, "unbekannt"))
    return None


def _gateway_bindung(neu: Mapping) -> Ablehnung | None:
    bindung = neu["bindung"]
    if ablehnung := _fremd(bindung, GATEWAY_BINDUNG_FELDER, "bindung"):
        return ablehnung
    fehlend = next((key for key in GATEWAY_BINDUNG_FELDER if key not in bindung), None)
    if fehlend is not None:
        return Ablehnung(ROLLE_UNZULAESSIG, befund(fehlend, "fehlt"))
    treiber = bindung["treiber"]
    if not (isinstance(treiber, Mapping) and _text(treiber.get("id")) and isinstance(treiber.get("parameter"), Mapping)):
        return Ablehnung(ROLLE_UNZULAESSIG, befund("treiber", "ungueltig"))
    if ablehnung := _fremd(treiber, TREIBER_FELDER, "treiber"):
        return ablehnung
    thermostat = bindung["thermostat"]
    if thermostat is not None and not (isinstance(thermostat, str) and IEEE.fullmatch(thermostat)):
        return Ablehnung(ROLLE_UNZULAESSIG, befund("thermostat", "format"))
    seen: set[str] = set()
    for ref in neu["raumfuehler"]:
        if not GATEWAY_RAUMFUEHLER.fullmatch(ref):
            return Ablehnung(ROLLE_UNZULAESSIG, befund("fuehler", "nicht_erlaubt"))
        if ref in seen:
            return Ablehnung(DOPPELT_BELEGT, befund("fuehler", "doppelt"))
        seen.add(ref)
    return None


def ha_bindung_pruefen(neu: Mapping) -> Ablehnung | None:
    """Aufbau der HA-Bindung rein (ohne Rueckruf des Hosts), oeffentlich fuer die Pruefung beim Start des HA-Hosts."""
    bindung = neu["bindung"]
    if ablehnung := _fremd(bindung, (*HA_BINDUNG_FELDER, *HA_BINDUNG_OPTIONAL), "bindung"):
        return ablehnung
    fehlend = next((key for key in HA_BINDUNG_FELDER if key not in bindung), None)
    if fehlend is not None:
        return Ablehnung(ROLLE_UNZULAESSIG, befund(fehlend, "fehlt"))
    kreis = bindung["kreis"]
    if not isinstance(kreis, Mapping):
        return Ablehnung(ROLLE_UNZULAESSIG, befund("kreis", "ungueltig"))
    if ablehnung := _fremd(kreis, (*KREIS_FELDER, *KREIS_OPTIONAL), "kreis"):
        return ablehnung
    # Pflichtfelder und config_entry (wenn vorhanden) sind nicht leere Texte; ein Kreis ohne config_entry hat keins.
    if not all(isinstance(kreis.get(key), str) and kreis[key] for key in KREIS_FELDER) or any(
            not (isinstance(kreis[key], str) and kreis[key]) for key in KREIS_OPTIONAL if key in kreis):
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
            return Ablehnung(ROLLE_UNZULAESSIG, befund("fuehler", "format"))
        if ref in seen:
            return Ablehnung(DOPPELT_BELEGT, befund("fuehler", "doppelt"))
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
    """None = uebernehmen; sonst der erste verletzte Grund (immer aus GRUENDE, Vertrag: Teilmenge von DOKUMENT_ERRORS).
    `rastung` ist die Rastung des Geraets (None: noch keine). Ein Grund ausserhalb GRUENDE wirft nicht in den
    Dokumentpfad, sondern gilt als ungueltig (Log ohne den Grund)."""
    ablehnung = _pruefen(neu, rastung, host=host, treiber_pruefen=treiber_pruefen)
    if ablehnung is not None and ablehnung.grund not in GRUENDE:
        logger.error("config_check: Grund ausserhalb GRUENDE, als ungueltig gemeldet")
        return Ablehnung(UNGUELTIG)
    return ablehnung


def _weg_des_kreises(bindung: Mapping, host: str) -> str | None:
    """Der Weg einer (aufgebauten) Bindung; None: Plattform bzw. Treiber ohne bekannten Weg."""
    if host == HOST_HA:
        return WEG_DER_PLATTFORM.get(bindung["kreis"]["plattform"])
    return WEG_DES_TREIBERS.get(bindung["treiber"]["id"])


def _ref(wert: str) -> str:
    """Die Entity hinter einer Referenz (entity_id oder entity_id::attribut): Thermostat-Soll und -Ist sind dieselbe."""
    return wert.split("::")[0]


def _ziele(sicht: Mapping, host: str) -> set:
    """Die Schreibziele eines Kreises: bei HA die Entities der Rollen und der Herstellerkreis, beim Gateway der Treiber
    mit seinen Parametern und das Thermostat."""
    bindung = sicht["bindung"]
    if host == HOST_HA:
        kreis = bindung["kreis"]
        return {*(("entity", _ref(ref)) for ref in bindung["rollen"].values()),
                ("kreis", *(kreis.get(key) for key in (*KREIS_FELDER, *KREIS_OPTIONAL)))}
    ziele: set[tuple] = {("treiber", bindung["treiber"]["id"],
                          json.dumps(bindung["treiber"]["parameter"], sort_keys=True))}
    if bindung["thermostat"] is not None:
        ziele.add(("thermostat", bindung["thermostat"]))
    return ziele


def _kreisuebergreifend(sichten: Sequence[Mapping], host: str) -> Ablehnung | None:
    """Kein Schreibziel und kein Fuehler gehoert zwei Kreisen."""
    ziele: set = set()
    fuehler: set = set()
    for sicht in sichten:
        eigene = _ziele(sicht, host)
        if ziele & eigene:
            return Ablehnung(DOPPELT_BELEGT, befund("bindung", "doppelt"))
        ziele |= eigene
        eigene_fuehler = {_ref(ref) if host == HOST_HA else ref for ref in sicht["raumfuehler"]}
        if fuehler & eigene_fuehler:
            return Ablehnung(DOPPELT_BELEGT, befund("fuehler", "doppelt"))
        fuehler |= eigene_fuehler
    return None


def _host_bindung(sicht: dict, host: str) -> Ablehnung | None:
    return ha_bindung_pruefen(sicht) if host == HOST_HA else _gateway_bindung(sicht)


def _pruefen(neu: Mapping, rastung: Rastung | None, *, host: str,
             treiber_pruefen: TreiberPruefung | None = None) -> Ablehnung | None:
    if not isinstance(neu, Mapping):
        return Ablehnung(UNGUELTIG)
    if ist_leer(neu):
        return None
    problem = _aufbau(neu)
    if problem is not None:
        return Ablehnung(UNGUELTIG, problem)
    if host not in (HOST_GATEWAY, HOST_HA):
        return Ablehnung(ROLLE_UNZULAESSIG)
    eintraege = kreisansicht.kreise(neu)
    sichten = [kreisansicht.ansicht(neu, eintrag) for eintrag in eintraege]
    for sicht in sichten:
        try:
            resolve_local_safety(sicht["hebelsatz"], sicht["verteilsystem"])
        except ValueError:
            feld = "hebelsatz" if sicht["hebelsatz"] not in LEVER_SETS else "verteilsystem"
            return Ablehnung(SICHERHEITSWERTE_FEHLEN, befund(feld, "unbekannt"))
        if not hebel_passt(rastung, sicht["hebelsatz"], sicht["verteilsystem"]):
            return Ablehnung(HEBELSATZ_EINGERASTET)
    # Die lokale Zeile (Weg, Hebelsatz) bestimmt Obergrenzen und Positivlisten. Ein Hebelsatz ohne Zeile am Weg hat
    # keine lokalen Sicherheitswerte. Bei unbekanntem Weg gibt es keine Zeile: die Bindung wird unten zuerst geprueft
    # (Rang der Bindungsfehler), danach scheitert der Weg laut.
    try:
        zeilen = [verfahren_fuer(neu["weg"], sicht["hebelsatz"]) for sicht in sichten]
    except WegUnbekannt:
        zeilen = None
    if zeilen is not None:
        if None in zeilen:
            return Ablehnung(SICHERHEITSWERTE_FEHLEN, befund("hebelsatz", "unbekannt"))
        try:
            beschreibung(neu["weg"], sichten[0]["hebelsatz"], neu.get("bindungsparameter"))
        except BindungsparameterFehler as error:
            return Ablehnung(UNGUELTIG, error.details)
    # Die Host-Pruefung sieht die Fuehler aller Raeume (auch der Kontrollraeume); die Regelung liest nur die der
    # Referenzraeume (`sichten`).
    pruefsichten = [{**sicht, "raumfuehler": kreisansicht.alle_fuehler(eintrag)}
                    for sicht, eintrag in zip(sichten, eintraege, strict=True)]
    for pruefsicht in pruefsichten:
        if ablehnung := _host_bindung(pruefsicht, host):
            return ablehnung
    # Die Rastung je Kreis: Hebelsatz und Verteilsystem standen oben schon an, Schreibziele und Soll-Entity lassen sich
    # erst bei wohlgeformter Bindung lesen. Das ganze Dokument zaehlt (mehr Kreise als Eintraege, ein Kreis ohne
    # Eintrag).
    if not passt(rastung, neu):
        return Ablehnung(HEBELSATZ_EINGERASTET)
    wege = [_weg_des_kreises(sicht["bindung"], host) for sicht in sichten]
    if neu["weg"] not in WEGE or None in wege:
        return Ablehnung(WEG_UNBEKANNT, befund("weg", "unbekannt"))
    if any(weg != neu["weg"] for weg in wege):
        return Ablehnung(WEG_MEHRFACH, befund("weg", "doppelt"))
    if ablehnung := _kreisuebergreifend(pruefsichten, host):
        return ablehnung
    if treiber_pruefen is None:
        return None
    for pruefsicht in pruefsichten:
        if host == HOST_HA:
            if (details := treiber_pruefen(kreisansicht.dokument_aus_sicht(pruefsicht))) is not None:
                return Ablehnung(ROLLE_UNZULAESSIG, details)
        elif (details := treiber_pruefen(dict(pruefsicht["bindung"]))) is not None:
            # Das gebundene Thermostat ist kein Thermostat: eine Rolle, kein Treiberparameter (sonst liest der Kunde
            # einen Text ueber den Treiber).
            return Ablehnung(ROLLE_UNZULAESSIG if details.get("feld") == "thermostat" else TREIBER_PARAMETER, details)
    return None
