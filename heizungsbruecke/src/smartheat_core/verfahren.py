"""Lokaler Teil der Bindung je Weg und Hebelsatz (G-7): die Felder des Schreibverfahrens
(Vorbereitung, Hilfswerte), die Anzeigetexte (bis G-6, Schritt 5) und die Obergrenzen des Schreibschutzes. Der Rest
der Bindung kommt mit der Konfiguration (smartheat_core.bindungsparameter). Die Obergrenzen schuetzen die Anlage vor
einem fehlerhaften Server (EEPROM, Cloud-Kontingent): er darf nur gleich oder strenger senden. Werte unveraendert aus
smartheat_core/binding.py bis Add-on 1.0.0; Aenderung nur nach Rueckfrage (Regel 4), Tabelle in
tests/test_regel4_werte.py. Die Tabelle hat eine Zeile je (Weg, Hebelsatz): ein Hebelsatz, den ein Weg nicht traegt,
hat dort keine Zeile und keine Schreibrechte; ein unbekannter Weg scheitert laut (`weg_unbekannt`). Die Werte einer
Zeile gelten fuer den Hebelsatz auf jedem Weg gleich. Das Verfahren selbst (Code) waehlt der Host nach dem Hebelsatz
(heizungsbruecke.ha_binding, Gateway-Treiber)."""
from collections.abc import Mapping
from dataclasses import dataclass, field

from smartheat_core.levers import VAILLANT_VRC720, VIESSMANN_VICARE, WEISHAUPT_WWP, WEISHAUPT_WWP_BASIS, LeverSet
from smartheat_core.write_budget import MAX_PER_DAY


@dataclass(frozen=True)
class Schreibschutz:
    """durchsetzen_tag: Durchsetzungs-Versuche je Hebel(-gruppe) und Tag; tag: physische Schreibvorgaenge je Tag
    ueber alle Hebel (None = kein Tagesbudget); lebensdauer_hinweis: Hinweis ab so vielen Schreibvorgaengen (None =
    nicht zaehlen). Als Obergrenze: None heisst keine lokale Grenze -- der Server darf keinen Wert oder jeden Wert
    >= 1 senden (ein Budget ist strenger als keins)."""

    durchsetzen_tag: int
    tag: int | None
    lebensdauer_hinweis: int | None


@dataclass(frozen=True)
class Verfahren:
    """schemata: Schluessel, die bindungsparameter.schemata fuer diesen Hebelsatz tragen muss. praefixe_erlaubt: lokale
    Positivliste der Optionsschema-Praefixe (Weishaupt); der Server darf nur eine Teilmenge senden, damit ein
    fehlerhafter Server keine Optionsnamen fuer Vorbereitung und Hilfswerte bilden kann (Regel 4).
    schreibgruppen_erlaubt: lokale Positivliste der Schreibgruppen; der Server darf je Gruppe nur dieselbe
    Hebelmenge senden (Reihenfolge egal) oder keine, damit er keine weiten Gruppen bildet, die ungezaehlt viele
    physische Schreibvorgaenge ausloesen (Regel 4). rueckstellung_pflicht: lokale Mindestliste der Hebel, die beim
    Abmelden und Abo-Ende zurueckgestellt werden; `zurueck` des Servers muss sie alle enthalten (mehr darf er senden),
    damit ein fehlerhafter Server die Rueckstellung nicht verkuerzen kann (Regel 4)."""

    lever_set: LeverSet
    prepared_lever: str | None
    preparation_resets_setpoint: bool
    aux_originals: tuple[str, ...]
    labels: Mapping[str, str]
    preparation_label: str
    obergrenze: Schreibschutz
    schemata: tuple[str, ...] = ()
    praefixe_erlaubt: tuple[str, ...] = ()
    schreibgruppen_erlaubt: tuple[tuple[str, ...], ...] = ()
    rueckstellung_pflicht: tuple[str, ...] = field(kw_only=True)  # Pflicht: ein neuer Hebelsatz ohne Liste startet nicht


_WEISHAUPT_AUX = ("mode_select", "setpoint_comfort", "setpoint_setback")
_WEISHAUPT_SCHUTZ = Schreibschutz(MAX_PER_DAY, 10, 50000)  # EEPROM: 10 am Tag, Hinweis ab 50 000 (Grenze 100 000)
_WEISHAUPT_SCHEMATA = ("praefixe", "normal_bereich")
# weishaupt_modbus 1.x ("hz_operationmode_") und 2.0 ("heating_circuit_operation_mode_"); ein neues Schema braucht ein
# Geraete-Update (Regel 4, Tabelle in tests/test_regel4_werte.py).
_WEISHAUPT_PRAEFIXE = ("hz_operationmode_", "heating_circuit_operation_mode_")

_JE_HEBELSATZ: dict[str, Verfahren] = {v.lever_set.id: v for v in (
    # Vaillant: Zone auf Manuell; die Umschaltung macht den Sollwert unbrauchbar (ohne Quota-Check neu schreiben).
    Verfahren(VAILLANT_VRC720, "room_setpoint", True, (), {
        "curve": "Heizkurve", "room_setpoint": "Wunschtemperatur der Zone", "heat_limit": "Heizgrenze",
        "min_flow": "Mindestvorlauftemperatur",
    }, "Betriebsart der Zone", Schreibschutz(MAX_PER_DAY, None, None), rueckstellung_pflicht=("heat_limit",)),
    Verfahren(WEISHAUPT_WWP, "room_setpoint", False, _WEISHAUPT_AUX, {
        "curve": "Heizkennlinie", "room_setpoint": "Raumsolltemperatur Normal", "heat_limit": "Sommer-Winter-Umschaltung",
    }, "Betriebsart", _WEISHAUPT_SCHUTZ, _WEISHAUPT_SCHEMATA,
              _WEISHAUPT_PRAEFIXE, rueckstellung_pflicht=("curve", "room_setpoint", "heat_limit")),
    Verfahren(WEISHAUPT_WWP_BASIS, "room_setpoint", False, _WEISHAUPT_AUX, {"room_setpoint": "Raumsolltemperatur Normal"},
              "Betriebsart", _WEISHAUPT_SCHUTZ, _WEISHAUPT_SCHEMATA,
              _WEISHAUPT_PRAEFIXE, rueckstellung_pflicht=("room_setpoint",)),
    # Viessmann: Komfort-/Eco-Programm verlassen; Cloud-Kontingent: Durchsetzen hoechstens 4 am Tag.
    Verfahren(VIESSMANN_VICARE, "room_setpoint", False, ("mode_select",), {
        "curve": "Neigung der Heizkurve", "level": "Niveau der Heizkurve", "room_setpoint": "Raumtemperatur Normal",
    }, "Heizprogramm", Schreibschutz(4, None, None), schreibgruppen_erlaubt=(("curve", "level"),),
              rueckstellung_pflicht=("curve", "level", "room_setpoint")),
)}

#: Weg -> Hebelsaetze, die er traegt (Wege wie smartheat_device.wire.WEGE; `simulation` ist der Testweg und traegt jeden
#: Hebelsatz, sein Treiber nimmt den Hebelsatz als Parameter). Ein neuer Weg oder Hebelsatz braucht ein Geraete-Update
#: (Regel 4, Tabelle in tests/test_regel4_werte.py).
_HEBELSAETZE_JE_WEG: dict[str, tuple[str, ...]] = {
    "vaillant_myvaillant": (VAILLANT_VRC720.id,),
    "viessmann_vicare": (VIESSMANN_VICARE.id,),
    "weishaupt_modbus": (WEISHAUPT_WWP.id, WEISHAUPT_WWP_BASIS.id),
    "simulation": tuple(_JE_HEBELSATZ),
}

VERFAHREN: dict[tuple[str, str], Verfahren] = {
    (weg, hebelsatz): _JE_HEBELSATZ[hebelsatz] for weg, hebelsaetze in _HEBELSAETZE_JE_WEG.items()
    for hebelsatz in hebelsaetze
}

#: Code des Fehlers bei unbekanntem Weg (derselbe Grund wie config_check.WEG_UNBEKANNT).
WEG_UNBEKANNT = "weg_unbekannt"


class WegUnbekannt(ValueError):
    """Der Weg steht nicht in der Tabelle: es gibt kein lokales Verfahren, auch nicht fuer einen bekannten Hebelsatz."""

    code = WEG_UNBEKANNT


def verfahren_fuer(weg: object, hebelsatz: str) -> Verfahren | None:
    """Die Zeile der Tabelle; None, wenn der Weg den Hebelsatz nicht traegt; unbekannter Weg: WegUnbekannt."""
    if not isinstance(weg, str) or weg not in _HEBELSAETZE_JE_WEG:
        raise WegUnbekannt("Der Weg ist unbekannt; es gibt kein lokales Schreibverfahren.")
    return VERFAHREN.get((weg, hebelsatz))
