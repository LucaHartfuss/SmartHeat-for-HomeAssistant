"""Gemeinsame Strategien und Pfadhilfen der Property-Tests (eigene Kopie, die
Repos teilen keine Testhilfen): beliebige JSON-Werte und Texte, die Stellen einer Vorlage und das Ersetzen oder
Entfernen an einer Stelle. Texte enthalten auch einzelne Surrogate (json.loads liefert sie aus "\\ud800"-Escapes).
Einstellungen (Beispielzahl, Seed) kommen aus den Profilen in conftest.py."""
import json

from hypothesis import strategies as st


def text(max_size: int) -> st.SearchStrategy[str]:
    """Beliebiger Text bis `max_size` Zeichen, auch mit einzelnen Surrogaten."""
    return st.text(alphabet=st.characters(blacklist_categories=()), max_size=max_size)


JSON_WERT = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=True, allow_infinity=True)
    | text(300) | st.just(10**400) | st.sampled_from(["\ud800", "a\udfffb", "-----BEGIN CERTIFICATE-----\ud800"]),
    lambda kinder: st.lists(kinder, max_size=5) | st.dictionaries(text(20), kinder, max_size=5),
    max_leaves=30,
)
#: json.loads liefert aus "\ud800" ein einzelnes Surrogat; schreiben_json kodiert als UTF-8 (ensure_ascii=False)
SURROGATTEXT = st.text(alphabet=st.sampled_from(["a", "é", "\ud800", "\udfff"]), min_size=1, max_size=8)
WEG = object()  # an der Stelle entfernen statt ersetzen


def pfade(wert, vorne=()):
    """Alle Pfade (Schluessel/Indizes) in einer Vorlage, auch die inneren Ebenen."""
    yield vorne
    if isinstance(wert, dict):
        for schluessel, kind in wert.items():
            yield from pfade(kind, (*vorne, schluessel))
    elif isinstance(wert, list):
        for index, kind in enumerate(wert):
            yield from pfade(kind, (*vorne, index))


def ersetze(wert, pfad, neu):
    """Kopie von `wert` mit `neu` an der Stelle `pfad` (leerer Pfad: `neu` selbst; `WEG` entfernt die Stelle)."""
    if not pfad:
        return neu
    kopie = json.loads(json.dumps(wert))
    ziel = kopie
    for schluessel in pfad[:-1]:
        ziel = ziel[schluessel]
    if neu is WEG:
        del ziel[pfad[-1]]
    else:
        ziel[pfad[-1]] = neu
    return kopie
