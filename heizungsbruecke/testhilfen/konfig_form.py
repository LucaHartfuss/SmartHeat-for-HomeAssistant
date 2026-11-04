"""Testhilfe: das Konfigurationsdokument in der Form je Kreis (smartheat_device.wire.KONFIGURATION_FIELDS) aus den
flachen Angaben, mit denen die Tests ihre Anlagen beschreiben (ein Kreis, ein Referenzraum mit den Fuehlern aus
`raumfuehler`). Nur fuer Tests; die Form selbst baut der Server."""
from collections.abc import Mapping

#: Anbindungswege (Kopie der Abbildung aus der Bindung; die Tests pruefen den Vertrag, nicht diese Tabelle)
_WEG_DER_PLATTFORM = {"mypyllant": "vaillant_myvaillant", "vicare": "viessmann_vicare",
                      "weishaupt_modbus": "weishaupt_modbus"}
_WEG_DES_TREIBERS = {"vicare_cloud": "viessmann_vicare", "simulation": "simulation"}
KREIS_FELDER = ("hebelsatz", "verteilsystem", "bindung", "abo", "ursprungswerte")
HUELLE = ("schema", "version")
#: Flache Angaben, die einen Platz im Dokument haben; alles andere gilt als Fremdfeld und bleibt oben im Dokument.
BEKANNT = frozenset(HUELLE) | frozenset(KREIS_FELDER) | {
    "anlage", "anlage_name", "weg", "kreise", "raumfuehler", "tagestick", "mindest_software", "mindest_integration",
    "software", "bindungsparameter"}


def weg_von(bindung) -> str:
    """Der Weg einer Bindung; eine unbekannte Plattform oder Treiber-ID bleibt unbekannt (`unbekannt`)."""
    if isinstance(bindung, Mapping):
        if isinstance(bindung.get("kreis"), Mapping):
            return _WEG_DER_PLATTFORM.get(bindung["kreis"].get("plattform"), "unbekannt")
        if isinstance(bindung.get("treiber"), Mapping):
            return _WEG_DES_TREIBERS.get(bindung["treiber"].get("id"), "unbekannt")
    return "unbekannt"


def raum(fuehler, *, nummer=1, name="Wohnzimmer", rolle="referenz", soll_quelle=False, versatz_k=0.0,
         ziel_fest=None) -> dict:
    return {"raum": nummer, "name": name, "rolle": rolle, "fuehler": list(fuehler), "soll_quelle": soll_quelle,
            "versatz_k": versatz_k, "ziel_fest": ziel_fest}


def kreis(flach: Mapping, *, nummer=1, raeume=None) -> dict:
    """Ein Kreiseintrag aus den flachen Angaben; `raeume` ersetzt den einen Referenzraum."""
    eintrag = {"kreis": nummer, "name": f"Kreis {nummer}", "bindung": flach.get("bindung"),
               "raeume": list(raeume) if raeume is not None else [raum(flach.get("raumfuehler") or ())],
               "hebelsatz": flach.get("hebelsatz"), "verteilsystem": flach.get("verteilsystem"),
               "abo": flach.get("abo")}
    if "ursprungswerte" in flach:
        eintrag["ursprungswerte"] = flach["ursprungswerte"]
    return eintrag


def nach_form(flach: Mapping, *, kreise=None) -> dict:
    """Das Dokument (mit schema und version, wenn `flach` sie hat) aus flachen Angaben; `kreise` ersetzt die Liste.
    Eine leere Konfiguration (anlage None) hat keine Kreise und keinen Weg."""
    neu = {feld: flach[feld] for feld in HUELLE if feld in flach}
    leer = "anlage" in flach and flach["anlage"] is None
    neu.update({
        "anlage": flach.get("anlage"), "anlage_name": None if leer else flach.get("anlage_name", "Testanlage"),
        "weg": None if leer else flach.get("weg", weg_von(flach.get("bindung"))),
        "kreise": [] if leer else (kreise if kreise is not None else flach["kreise"] if "kreise" in flach
                                   else [kreis(flach)]),
        "tagestick": flach.get("tagestick"), "mindest_software": flach.get("mindest_software"),
        "mindest_integration": flach.get("mindest_integration"), "software": flach.get("software"),
        "bindungsparameter": flach.get("bindungsparameter"),
    })
    neu.update({feld: wert for feld, wert in flach.items() if feld not in BEKANNT})
    return neu
