"""Bindungsparameter aus der Konfiguration pruefen und mit dem lokalen Verfahrensteil zur wirksamen
BindingDescription verbinden (G-7; Spec Stufe 3 S4b Abschnitte 3 und 4.2). Rein: nur Standardbibliothek und
smartheat_core. Feldnamen = smartheat_device.wire (BINDUNGSPARAMETER_FIELDS, SCHREIBSCHUTZ_FIELDS, ZAEHLER,
ENERGIE_EINHEITEN); ein Test haelt sie gleich, der Kern importiert den Geraetekern nicht. Jeder Fehler ist ein
ValueError mit einem Text fuer Portal und Assistent; es gibt keine Ersatztabelle (Nutzer-Entscheidung 2026-10-15)."""
import math
from collections.abc import Mapping
from typing import TypeGuard

from smartheat_core.binding import ENERGY_DAILY, ENERGY_TOTAL, BindingDescription
from smartheat_core.levers import LEVERS
from smartheat_core.verfahren import VERFAHREN, Schreibschutz, Verfahren

FELDER = ("schritte", "toleranzen", "wartezeit_s", "schreibgruppen", "zurueck", "zurueck_optional", "nur_lesen",
          "zaehler", "schreibschutz", "schemata", "energie")
SCHREIBSCHUTZ_FELDER = ("tag", "lebensdauer_hinweis", "durchsetzen_tag")
ZAEHLER = (ENERGY_TOTAL, ENERGY_DAILY)
#: Einheit -> (Zieleinheit, Faktor): gesendet wird in kWh bzw. m³ (Spec 5).
EINHEITEN = {"Wh": ("kWh", 0.001), "kWh": ("kWh", 1.0), "m³": ("m³", 1.0)}
#: Energiekanaele = Energierollen ohne "energy_" (Telemetrie, Server generic_energy_log).
ENERGIE_KANAELE = ("electrical_heating", "electrical_dhw", "primary_heating", "primary_dhw", "thermal_heating",
                   "thermal_dhw", "electrical_total")
SCHRITT_MAX = 5.0
WARTEZEIT_S = (60, 7200)


def _zahl(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _ganz(value) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _hebel(value, erlaubt, feld: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value) or not set(value) <= set(erlaubt) \
            or len(set(value)) != len(value):
        raise ValueError(f"Bindungsparameter {feld} ist ungültig.")
    return tuple(value)


def _zahlen_je_hebel(value, hebel, feld: str) -> dict[str, float]:
    if not isinstance(value, Mapping) or set(value) != set(hebel) or not all(_zahl(v) for v in value.values()):
        raise ValueError(f"Bindungsparameter {feld} passt nicht zum Hebelsatz.")
    return {lever: float(v) for lever, v in value.items()}


def _schreibschutz(value, grenze: Schreibschutz) -> Schreibschutz:
    if not isinstance(value, Mapping) or set(value) != set(SCHREIBSCHUTZ_FELDER):
        raise ValueError("Bindungsparameter schreibschutz ist unvollständig.")
    durchsetzen = value["durchsetzen_tag"]
    if not (_ganz(durchsetzen) and 1 <= durchsetzen <= grenze.durchsetzen_tag):
        raise ValueError(f"Durchsetzen je Tag muss zwischen 1 und {grenze.durchsetzen_tag} liegen.")
    werte: dict[str, int | None] = {}
    for feld, obergrenze in (("tag", grenze.tag), ("lebensdauer_hinweis", grenze.lebensdauer_hinweis)):
        wert = value[feld]
        if wert is None and obergrenze is None:
            werte[feld] = None
        elif _ganz(wert) and wert >= 1 and (obergrenze is None or wert <= obergrenze):
            werte[feld] = wert
        else:
            raise ValueError(f"Schreibschutz {feld} ist ungültig oder lockerer als die lokale Grenze.")
    return Schreibschutz(durchsetzen, werte["tag"], werte["lebensdauer_hinweis"])


def _schemata(value, verfahren: Verfahren) -> dict:
    if not isinstance(value, Mapping) or set(value) != set(verfahren.schemata):
        raise ValueError("Bindungsparameter schemata passt nicht zum Hebelsatz.")
    if not verfahren.schemata:
        return {}
    praefixe, bereiche = value["praefixe"], value["normal_bereich"]
    if not isinstance(praefixe, list) or not praefixe or not all(isinstance(p, str) and 0 < len(p) <= 64 for p in praefixe):
        raise ValueError("Bindungsparameter schemata.praefixe ist ungültig.")
    if not isinstance(bereiche, Mapping) or set(bereiche) != set(praefixe) or not all(
            isinstance(b, list) and len(b) == 2 and all(_zahl(x) for x in b) and b[0] <= b[1] for b in bereiche.values()):
        raise ValueError("Bindungsparameter schemata.normal_bereich ist ungültig.")
    return {"praefixe": tuple(praefixe), "normal_bereich": {p: (float(b[0]), float(b[1])) for p, b in bereiche.items()}}


def _energie(value) -> dict[str, str]:
    if not isinstance(value, Mapping) or not set(value) <= set(ENERGIE_KANAELE) or not all(
            isinstance(v, str) and v in EINHEITEN for v in value.values()):
        raise ValueError("Bindungsparameter energie ist ungültig.")
    return dict(value)


def beschreibung(hebelsatz: str, parameter) -> BindingDescription:
    """Wirksame Beschreibung fuer den Hebelsatz aus den Parametern der Konfiguration; ValueError bei jedem Verstoss."""
    verfahren = VERFAHREN.get(hebelsatz)
    if verfahren is None:
        raise ValueError(f"Für den Hebelsatz {hebelsatz} gibt es kein lokales Schreibverfahren.")
    if not isinstance(parameter, Mapping):
        raise ValueError("Die Bindungsparameter fehlen.")
    if set(parameter) != set(FELDER):
        raise ValueError("Die Bindungsparameter sind unvollständig oder enthalten fremde Felder.")
    lever_set = verfahren.lever_set
    hebel = lever_set.levers + lever_set.client_derived
    schritte = _zahlen_je_hebel(parameter["schritte"], hebel, "schritte")
    toleranzen = _zahlen_je_hebel(parameter["toleranzen"], hebel, "toleranzen")
    for lever in hebel:
        if not 0 < schritte[lever] <= SCHRITT_MAX:
            raise ValueError(f"Die Schrittweite für {lever} muss größer 0 und höchstens {SCHRITT_MAX:g} sein.")
        if not 0 < toleranzen[lever] <= schritte[lever]:
            raise ValueError(f"Die Toleranz für {lever} muss größer 0 und höchstens die Schrittweite sein.")
    wartezeit = parameter["wartezeit_s"]
    if not (_zahl(wartezeit) and WARTEZEIT_S[0] <= wartezeit <= WARTEZEIT_S[1]):
        raise ValueError(f"Die Wartezeit muss zwischen {WARTEZEIT_S[0]} und {WARTEZEIT_S[1]} Sekunden liegen.")
    gruppen = parameter["schreibgruppen"]
    if not isinstance(gruppen, list):
        raise ValueError("Bindungsparameter schreibgruppen ist ungültig.")
    if parameter["zaehler"] not in ZAEHLER:
        raise ValueError("Bindungsparameter zaehler ist unbekannt.")
    schutz = _schreibschutz(parameter["schreibschutz"], verfahren.obergrenze)
    fremde_hebel = tuple(lever for lever in LEVERS if lever not in hebel)
    try:
        return BindingDescription(
            lever_set=lever_set, steps=schritte, enforce_tolerance=toleranzen, settle_seconds=float(wartezeit),
            restore_originals=_hebel(parameter["zurueck"], lever_set.levers, "zurueck"),
            optional_restore=_hebel(parameter["zurueck_optional"], lever_set.levers, "zurueck_optional"),
            prepared_lever=verfahren.prepared_lever, labels=verfahren.labels,
            preparation_label=verfahren.preparation_label,
            preparation_resets_setpoint=verfahren.preparation_resets_setpoint,
            daily_write_limit=schutz.tag, enforce_per_day=schutz.durchsetzen_tag,
            write_groups=tuple(_hebel(g, lever_set.levers, "schreibgruppen") for g in gruppen),
            lifetime_hint_at=schutz.lebensdauer_hinweis, aux_originals=verfahren.aux_originals,
            energy_counters=parameter["zaehler"],
            readonly_levers=_hebel(parameter["nur_lesen"], fremde_hebel, "nur_lesen"),
            schemata=_schemata(parameter["schemata"], verfahren), energie=_energie(parameter["energie"]),
        )
    except ValueError as error:  # BindingDescription.__post_init__ (z. B. Schreibgruppen)
        raise ValueError(f"Die Bindungsparameter sind ungültig ({error}).") from None
