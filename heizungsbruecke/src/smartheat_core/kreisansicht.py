"""Flache Sicht auf einen Kreis des Konfigurationsdokuments, rein: nur Standardbibliothek.

Das Dokument fuehrt anlagenweit Anlage, Weg, Tagestick und Bindungsparameter und je Kreis Bindung, Hebelsatz,
Verteilsystem, Abo und Raeume (smartheat_device.wire.KONFIGURATION_FIELDS, KONFIGURATION_KREIS_FIELDS). Pruefung und
Hosts arbeiten je Kreis; diese Sicht legt die Felder eines Kreises mit den anlagenweiten zusammen. Die Regelung liest
nur die Fuehler der Referenzraeume (`raumfuehler`); die Fuehler der Kontrollraeume stehen nur in `alle_fuehler`, sie
beeinflussen die Regelung nie. Die Funktionen sind tolerant gegen Fremdinhalt (das Dokument kommt vom Server) und
liefern dann leere Werte, die Pruefung lehnt den Aufbau davor ab."""
from collections.abc import Mapping

ROLLE_REFERENZ = "referenz"
#: Felder, die die Sicht aus dem Kreiseintrag unveraendert uebernimmt.
KREIS_FELDER = ("kreis", "name", "bindung", "hebelsatz", "verteilsystem", "abo")
#: Anlagenweite Felder der Sicht.
ANLAGE_FELDER = ("anlage", "anlage_name", "weg", "tagestick", "bindungsparameter")


def kreise(konfiguration) -> list[Mapping]:
    """Die Kreiseintraege des Dokuments (nur Objekte), leer bei fehlender oder unlesbarer Liste."""
    liste = konfiguration.get("kreise") if isinstance(konfiguration, Mapping) else None
    return [eintrag for eintrag in liste if isinstance(eintrag, Mapping)] if isinstance(liste, list) else []


def _fuehler(raum) -> list[str]:
    fuehler = raum.get("fuehler") if isinstance(raum, Mapping) else None
    return [ref for ref in fuehler if isinstance(ref, str)] if isinstance(fuehler, list) else []


def _raeume(kreis: Mapping) -> list[Mapping]:
    raeume = kreis.get("raeume")
    return [raum for raum in raeume if isinstance(raum, Mapping)] if isinstance(raeume, list) else []


def referenz_fuehler(kreis: Mapping) -> list[str]:
    """Die Fuehler der Referenzraeume in der Reihenfolge der Raeume: das, was die Regelung liest."""
    return [ref for raum in _raeume(kreis) if raum.get("rolle") == ROLLE_REFERENZ for ref in _fuehler(raum)]


def alle_fuehler(kreis: Mapping) -> list[str]:
    """Alle Fuehler des Kreises, auch die der Kontrollraeume (Pruefung der Referenzen, Doppelbelegung)."""
    return [ref for raum in _raeume(kreis) for ref in _fuehler(raum)]


def ansicht(konfiguration: Mapping, kreis: Mapping) -> dict:
    """Anlagenweite Felder plus die Felder des Kreises; `raumfuehler` sind die Fuehler der Referenzraeume,
    `ursprungswerte` steht nur, wenn der Kreis welche hat. Ein leerer Kreis ({}) ergibt eine Sicht ohne Kreisdaten."""
    sicht = {feld: konfiguration.get(feld) for feld in ANLAGE_FELDER}
    sicht.update({feld: kreis.get(feld) for feld in KREIS_FELDER})
    sicht["raumfuehler"] = referenz_fuehler(kreis)
    if "ursprungswerte" in kreis:
        sicht["ursprungswerte"] = kreis["ursprungswerte"]
    return sicht


def erste(konfiguration) -> dict:
    """Die Sicht des ersten Kreises; ohne Kreis (leere Konfiguration, Fremdinhalt) nur die anlagenweiten Felder.
    Anlagenweite Angaben (Gesamtstatus, Ursprungswerte der Meldung) lesen den ersten Kreis; die Hosts und die Laufzeit
    arbeiten je Kreis mit `sicht`."""
    if not isinstance(konfiguration, Mapping):
        konfiguration = {}
    eintraege = kreise(konfiguration)
    return ansicht(konfiguration, eintraege[0] if eintraege else {})


def fuer(konfiguration, nummer: int) -> dict:
    """Die Sicht des Kreises mit dieser Nummer; ohne ihn nur die anlagenweiten Felder (wie `erste` ohne Kreis)."""
    if not isinstance(konfiguration, Mapping):
        konfiguration = {}
    for eintrag in kreise(konfiguration):
        if eintrag.get("kreis") == nummer and not isinstance(eintrag.get("kreis"), bool):
            return ansicht(konfiguration, eintrag)
    return ansicht(konfiguration, {})


def sicht(konfiguration, kreis: int | None = None) -> dict:
    """Die Sicht des Kreises mit dieser Nummer; ohne Nummer die des ersten Kreises (`erste`)."""
    return erste(konfiguration) if kreis is None else fuer(konfiguration, kreis)


def raeume_fuehler(konfiguration, kreis: int | None = None) -> list[tuple[int, list[str]]]:
    """(Raumnummer, Fuehler) der Raeume des Kreises (ohne Nummer des ersten) mit mindestens einem Fuehler, in der
    Reihenfolge des Dokuments; Referenz- und Kontrollraeume. Ohne Kreis leer."""
    if not isinstance(konfiguration, Mapping):
        return []
    eintraege = kreise(konfiguration)
    if kreis is None:
        gewaehlt = eintraege[0] if eintraege else None
    else:
        gewaehlt = next((e for e in eintraege if e.get("kreis") == kreis and not isinstance(e.get("kreis"), bool)), None)
    if gewaehlt is None:
        return []
    return [(raum["raum"], fuehler) for raum in _raeume(gewaehlt)
            if isinstance(raum.get("raum"), int) and not isinstance(raum.get("raum"), bool)
            and (fuehler := _fuehler(raum))]


def dokument_aus_sicht(sicht: Mapping) -> dict:
    """Die Umkehrung von `ansicht` fuer genau einen Kreis: ein Dokument mit einem Kreis, dessen einziger Raum ein
    Referenzraum mit den `raumfuehler` der Sicht ist (fuer die Pruefung gegen den Host)."""
    eintrag = {feld: sicht.get(feld) for feld in KREIS_FELDER}
    eintrag["raeume"] = [{"rolle": ROLLE_REFERENZ, "fuehler": list(sicht.get("raumfuehler") or [])}]
    dokument = {feld: sicht.get(feld) for feld in ANLAGE_FELDER}
    dokument["kreise"] = [eintrag]
    return dokument
