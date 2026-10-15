"""Lokaler Teil der Bindung je Hebelsatz (G-7; Spec Stufe 3 S4b Abschnitt 3): die Felder des Schreibverfahrens
(Vorbereitung, Hilfswerte), die Anzeigetexte (bis G-6, Schritt 5) und die Obergrenzen des Schreibschutzes. Der Rest
der Bindung kommt mit der Konfiguration (smartheat_core.bindungsparameter). Die Obergrenzen schuetzen die Anlage vor
einem fehlerhaften Server (EEPROM, Cloud-Kontingent): er darf nur gleich oder strenger senden. Werte unveraendert aus
smartheat_core/binding.py bis Add-on 1.0.0; Aenderung nur nach Rueckfrage (Regel 4), Tabelle in
tests/test_regel4_werte.py. Das Verfahren selbst (Code) waehlt der Host nach dem Hebelsatz (heizungsbruecke.ha_binding,
Gateway-Treiber)."""
from collections.abc import Mapping
from dataclasses import dataclass

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
    """schemata: Schluessel, die bindungsparameter.schemata fuer diesen Hebelsatz tragen muss."""

    lever_set: LeverSet
    prepared_lever: str | None
    preparation_resets_setpoint: bool
    aux_originals: tuple[str, ...]
    labels: Mapping[str, str]
    preparation_label: str
    obergrenze: Schreibschutz
    schemata: tuple[str, ...] = ()


_WEISHAUPT_AUX = ("mode_select", "setpoint_comfort", "setpoint_setback")
_WEISHAUPT_SCHUTZ = Schreibschutz(MAX_PER_DAY, 10, 50000)  # EEPROM: 10 am Tag, Hinweis ab 50 000 (Grenze 100 000)
_WEISHAUPT_SCHEMATA = ("praefixe", "normal_bereich")

VERFAHREN: dict[str, Verfahren] = {v.lever_set.id: v for v in (
    # Vaillant: Zone auf Manuell; die Umschaltung macht den Sollwert unbrauchbar (ohne Quota-Check neu schreiben).
    Verfahren(VAILLANT_VRC720, "room_setpoint", True, (), {
        "curve": "Heizkurve", "room_setpoint": "Wunschtemperatur der Zone", "heat_limit": "Heizgrenze",
        "min_flow": "Mindestvorlauftemperatur",
    }, "Betriebsart der Zone", Schreibschutz(MAX_PER_DAY, None, None)),
    Verfahren(WEISHAUPT_WWP, "room_setpoint", False, _WEISHAUPT_AUX, {
        "curve": "Heizkennlinie", "room_setpoint": "Raumsolltemperatur Normal", "heat_limit": "Sommer-Winter-Umschaltung",
    }, "Betriebsart", _WEISHAUPT_SCHUTZ, _WEISHAUPT_SCHEMATA),
    Verfahren(WEISHAUPT_WWP_BASIS, "room_setpoint", False, _WEISHAUPT_AUX, {"room_setpoint": "Raumsolltemperatur Normal"},
              "Betriebsart", _WEISHAUPT_SCHUTZ, _WEISHAUPT_SCHEMATA),
    # Viessmann: Komfort-/Eco-Programm verlassen; Cloud-Kontingent: Durchsetzen hoechstens 4 am Tag.
    Verfahren(VIESSMANN_VICARE, "room_setpoint", False, ("mode_select",), {
        "curve": "Neigung der Heizkurve", "level": "Niveau der Heizkurve", "room_setpoint": "Raumtemperatur Normal",
    }, "Heizprogramm", Schreibschutz(4, None, None)),
)}
