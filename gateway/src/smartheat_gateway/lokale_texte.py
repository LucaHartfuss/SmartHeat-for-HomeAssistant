"""Die festen Texte der lokalen Seite (Diagnose und Anmeldung) in Deutsch und Englisch, aus `lokale_texte.json` neben
diesem Modul: flache Schluessel, je Sprache dieselben, nur der Platzhalter `{produkt}` (eine Konstante des Images).
Gewaehlt wird allein nach dem Kopf Accept-Language, und nur zwischen `de` und `en`: ohne passenden Eintrag gilt `en`.
Der Kopf selbst gelangt nie in eine Antwort. Kein Text stammt von Geraet oder Server; wer einen Text in HTML setzt,
maskiert ihn am Aufrufer (`html.escape`)."""
import functools
import json
import re
from pathlib import Path

PRODUKT = "SmartHeat"
SPRACHEN = ("de", "en")
STANDARD = "en"
KOPF_MAX_ZEICHEN = 512  # laenger ist kein echter Browserkopf; die Seite faellt auf STANDARD zurueck
EINTRAEGE_MAX = 32
_TAG = re.compile(r"[A-Za-z]{1,8}(?:-[A-Za-z0-9]{1,8})*")
_GEWICHT = re.compile(r"(?:0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)")


def sprache_waehlen(kopf: str | None) -> str:
    """`de` oder `en` nach dem Kopf Accept-Language: der Eintrag mit dem hoechsten Gewicht (q), bei Gleichstand der
    erste; q=0, unlesbare Eintraege und alle anderen Sprachen zaehlen nicht. Sonst STANDARD."""
    if not kopf or len(kopf) > KOPF_MAX_ZEICHEN:
        return STANDARD
    bester, bestes_gewicht = STANDARD, 0.0
    for eintrag in kopf.split(",")[:EINTRAEGE_MAX]:
        tag, _, parameter = eintrag.partition(";")
        tag, parameter = tag.strip(), parameter.strip()
        if not _TAG.fullmatch(tag):
            continue
        gewicht = 1.0
        if parameter:
            name, _, wert = parameter.partition("=")
            if name.strip().lower() != "q" or not _GEWICHT.fullmatch(wert.strip()):
                continue
            gewicht = float(wert.strip())
        sprache = tag.split("-", 1)[0].lower()
        if sprache in SPRACHEN and gewicht > bestes_gewicht:
            bester, bestes_gewicht = sprache, gewicht
    return bester


@functools.cache
def _katalog() -> dict[str, dict[str, str]]:
    roh = json.loads((Path(__file__).parent / "lokale_texte.json").read_text(encoding="utf-8"))
    return {sprache: {schluessel: text.replace("{produkt}", PRODUKT) for schluessel, text in roh[sprache].items()}
            for sprache in SPRACHEN}


def alle(sprache: str) -> dict[str, str]:
    """Alle Texte einer Sprache, `{produkt}` eingesetzt; eine unbekannte Sprache zaehlt als STANDARD."""
    return _katalog()[sprache if sprache in SPRACHEN else STANDARD]


def gruppe(sprache: str, praefix: str) -> dict[str, str]:
    """Die Texte mit dem Schluesselanfang `<praefix>.`, ohne diesen Anfang."""
    anfang = f"{praefix}."
    return {schluessel[len(anfang):]: wert for schluessel, wert in alle(sprache).items()
            if schluessel.startswith(anfang)}
