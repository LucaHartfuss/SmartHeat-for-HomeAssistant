"""Bindungsparameter aus der Konfiguration pruefen und mit dem lokalen Verfahrensteil zur wirksamen
BindingDescription verbinden (G-7; Spec Stufe 3 S4b Abschnitte 3 und 4.2). Rein: nur Standardbibliothek und
smartheat_core. Feldnamen = smartheat_device.wire (BINDUNGSPARAMETER_FIELDS, SCHREIBSCHUTZ_FIELDS, ZAEHLER,
ENERGIE_EINHEITEN); ein Test haelt sie gleich, der Kern importiert den Geraetekern nicht. Jeder Fehler ist ein
`Fehler` (ValueError) mit lokalem Text und details fuer den Server (Spec Stufe 3 S5 6.1); es gibt keine Ersatztabelle
(Nutzer-Entscheidung 2026-10-15)."""
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


class Fehler(ValueError):
    """Verstoss gegen die lokalen Grenzen der Bindungsparameter: der Text ist lokal (Start, Log), details gehen an den
    Server (Spec Stufe 3 S5 6.1; Feldnamen aus wire.PRUEF_FELDER)."""

    def __init__(self, text: str, feld: str, regel: str, **zahlen: float) -> None:
        super().__init__(text)
        self.details = {"feld": feld, "regel": regel, **zahlen}


def _zahl(value) -> bool:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # JSON liefert beliebig grosse Ganzzahlen
        return False


def _ganz(value) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _hebel(value, erlaubt, feld: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value) or not set(value) <= set(erlaubt) \
            or len(set(value)) != len(value):
        raise Fehler(f"Das Feld {feld} der Bindungsparameter ist ungültig.", feld, "ungueltig")
    return tuple(value)


def _zahlen_je_hebel(value, hebel, feld: str) -> dict[str, float]:
    if not isinstance(value, Mapping) or set(value) != set(hebel) or not all(_zahl(v) for v in value.values()):
        raise Fehler(f"Das Feld {feld} der Bindungsparameter passt nicht zum Hebelsatz.", feld, "ungueltig")
    return {lever: float(v) for lever, v in value.items()}


def _schreibschutz(value, grenze: Schreibschutz) -> Schreibschutz:
    if not isinstance(value, Mapping) or set(value) != set(SCHREIBSCHUTZ_FELDER):
        raise Fehler("Der Schreibschutz in den Bindungsparametern ist unvollständig.", "schreibschutz", "ungueltig")
    durchsetzen = value["durchsetzen_tag"]
    if not (_ganz(durchsetzen) and 1 <= durchsetzen <= grenze.durchsetzen_tag):
        raise Fehler(f"Durchsetzen je Tag muss in den Bindungsparametern zwischen 1 und {grenze.durchsetzen_tag} "
                     "liegen.", "durchsetzen_tag", "bereich", min=1, max=grenze.durchsetzen_tag)
    werte: dict[str, int | None] = {}
    for feld, obergrenze in (("tag", grenze.tag), ("lebensdauer_hinweis", grenze.lebensdauer_hinweis)):
        wert = value[feld]
        if wert is None and obergrenze is None:
            werte[feld] = None
        elif _ganz(wert) and wert >= 1 and (obergrenze is None or wert <= obergrenze):
            werte[feld] = wert
        else:
            raise Fehler(f"Der Schreibschutz {feld} in den Bindungsparametern ist ungültig oder lockerer als die "
                         "lokale Grenze.", feld, "bereich", **({"max": obergrenze} if obergrenze is not None else {}))
    return Schreibschutz(durchsetzen, werte["tag"], werte["lebensdauer_hinweis"])


def _schemata(value, verfahren: Verfahren) -> dict:
    if not isinstance(value, Mapping) or set(value) != set(verfahren.schemata):
        raise Fehler("Das Feld schemata der Bindungsparameter passt nicht zum Hebelsatz.", "schemata", "ungueltig")
    if not verfahren.schemata:
        return {}
    praefixe, bereiche = value["praefixe"], value["normal_bereich"]
    if (
        not isinstance(praefixe, list)
        or not praefixe
        or not all(isinstance(p, str) and 0 < len(p) <= 64 for p in praefixe)
        or len(set(praefixe)) != len(praefixe)
        or any(a != b and b.startswith(a) for a in praefixe for b in praefixe)  # ein Praefix darf kein anderes verdecken
    ):
        raise Fehler("Das Feld schemata.praefixe der Bindungsparameter ist ungültig.", "schemata", "ungueltig")
    if not set(praefixe) <= set(verfahren.praefixe_erlaubt):
        raise Fehler("Das Feld schemata.praefixe der Bindungsparameter nennt ein Schema, das dieses Gerät "
                     "nicht kennt.", "schemata", "ungueltig")
    if not isinstance(bereiche, Mapping) or set(bereiche) != set(praefixe) or not all(
            isinstance(b, list) and len(b) == 2 and all(_zahl(x) for x in b) and b[0] <= b[1] for b in bereiche.values()):
        raise Fehler("Das Feld schemata.normal_bereich der Bindungsparameter ist ungültig.", "schemata", "ungueltig")
    return {"praefixe": tuple(praefixe), "normal_bereich": {p: (float(b[0]), float(b[1])) for p, b in bereiche.items()}}


def _energie(value) -> dict[str, str]:
    if not isinstance(value, Mapping) or not set(value) <= set(ENERGIE_KANAELE) or not all(
            isinstance(v, str) and v in EINHEITEN for v in value.values()):
        raise Fehler("Das Feld energie der Bindungsparameter ist ungültig.", "energie", "ungueltig")
    return dict(value)


def beschreibung(hebelsatz: str, parameter) -> BindingDescription:
    """Wirksame Beschreibung fuer den Hebelsatz aus den Parametern der Konfiguration; Fehler (ValueError) bei jedem
    Verstoss."""
    verfahren = VERFAHREN.get(hebelsatz)
    if verfahren is None:
        raise Fehler(f"Für den Hebelsatz {hebelsatz} gibt es kein lokales Schreibverfahren; die Bindungsparameter "
                     "lassen sich nicht prüfen.", "hebelsatz", "unbekannt")
    if not isinstance(parameter, Mapping):
        raise Fehler("Die Bindungsparameter fehlen.", "bindungsparameter", "fehlt")
    if set(parameter) != set(FELDER):
        raise Fehler("Die Bindungsparameter sind unvollständig oder enthalten fremde Felder.", "bindungsparameter",
                     "ungueltig")
    lever_set = verfahren.lever_set
    hebel = lever_set.levers + lever_set.client_derived
    schritte = _zahlen_je_hebel(parameter["schritte"], hebel, "schritte")
    toleranzen = _zahlen_je_hebel(parameter["toleranzen"], hebel, "toleranzen")
    for lever in hebel:
        if not 0 < schritte[lever] <= SCHRITT_MAX:
            raise Fehler(f"Die Schrittweite für {lever} in den Bindungsparametern muss größer 0 und höchstens "
                         f"{SCHRITT_MAX:g} sein.", "schritte", "bereich", max=SCHRITT_MAX)
        if not 0 < toleranzen[lever] <= schritte[lever]:
            raise Fehler(f"Die Toleranz für {lever} in den Bindungsparametern muss größer 0 und höchstens die "
                         "Schrittweite sein.", "toleranzen", "bereich", max=schritte[lever])
    wartezeit = parameter["wartezeit_s"]
    if not (_zahl(wartezeit) and WARTEZEIT_S[0] <= wartezeit <= WARTEZEIT_S[1]):
        raise Fehler(f"Die Wartezeit in den Bindungsparametern muss zwischen {WARTEZEIT_S[0]} und {WARTEZEIT_S[1]} "
                     "Sekunden liegen.", "wartezeit_s", "bereich", min=WARTEZEIT_S[0], max=WARTEZEIT_S[1])
    gruppen = parameter["schreibgruppen"]
    if not isinstance(gruppen, list):
        raise Fehler("Das Feld schreibgruppen der Bindungsparameter ist ungültig.", "schreibgruppen", "ungueltig")
    schreibgruppen = tuple(_hebel(g, lever_set.levers, "schreibgruppen") for g in gruppen)
    if any(len(g) < 2 for g in schreibgruppen) or len({h for g in schreibgruppen for h in g}) != sum(
        len(g) for g in schreibgruppen
    ):
        raise Fehler("Jede Schreibgruppe der Bindungsparameter braucht mindestens zwei Hebel, und kein Hebel "
                     "steht in zwei Gruppen.", "schreibgruppen", "ungueltig")
    if parameter["zaehler"] not in ZAEHLER:
        raise Fehler("Die Zählerart in den Bindungsparametern ist unbekannt.", "zaehler", "unbekannt")
    zurueck = _hebel(parameter["zurueck"], lever_set.levers, "zurueck")
    zurueck_optional = _hebel(parameter["zurueck_optional"], lever_set.levers, "zurueck_optional")
    if not set(zurueck_optional) <= set(zurueck):
        raise Fehler("Ein optionaler Rückstell-Hebel der Bindungsparameter muss auch unter den zurückzustellenden "
                     "Hebeln stehen.", "zurueck_optional", "ungueltig")
    nur_lesen = _hebel(parameter["nur_lesen"], tuple(h for h in LEVERS if h not in hebel), "nur_lesen")
    schemata = _schemata(parameter["schemata"], verfahren)
    energie = _energie(parameter["energie"])
    schutz = _schreibschutz(parameter["schreibschutz"], verfahren.obergrenze)
    try:
        return BindingDescription(
            lever_set=lever_set, steps=schritte, enforce_tolerance=toleranzen, settle_seconds=float(wartezeit),
            restore_originals=zurueck, optional_restore=zurueck_optional,
            prepared_lever=verfahren.prepared_lever, labels=verfahren.labels,
            preparation_label=verfahren.preparation_label,
            preparation_resets_setpoint=verfahren.preparation_resets_setpoint,
            daily_write_limit=schutz.tag, enforce_per_day=schutz.durchsetzen_tag,
            write_groups=schreibgruppen,
            lifetime_hint_at=schutz.lebensdauer_hinweis, aux_originals=verfahren.aux_originals,
            energy_counters=parameter["zaehler"],
            readonly_levers=nur_lesen, schemata=schemata, energie=energie,
        )
    except ValueError as error:  # Rueckhalt: BindingDescription.__post_init__
        raise Fehler(f"Die Bindungsparameter sind ungültig ({error}).", "bindungsparameter", "ungueltig") from None
