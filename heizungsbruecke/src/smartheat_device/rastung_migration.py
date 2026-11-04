"""Einmalige Migration der Rastung im Wartungsfenster, von Hand aufgerufen (kein Teil des Normalstarts):

    python -m smartheat_device.rastung_migration --device-dir <Ordner> --host ha|gateway --release <Release>

Sie deckt genau zwei Faelle ab. Die Konfiguration darf dabei noch in der flachen Form des Stands vor den Kreisen liegen
(Heizungsbruecke 1.1.0, so liegt sie auf den Geraeten): sie wird im Speicher zu einem Dokument mit einem Kreis und
einem Referenzraum umgeformt und wie ein neues Dokument geprueft. Nach einem Erfolg stellt der Lauf die flache Datei
als `konfiguration.json.pre-<Release>` beiseite (Rueckweg: zurueckspielen): der neue Stand liest die flache Form nie, das
Geraet gilt danach als nicht eingerichtet und wartet still auf das Dokument mit Kreisen, das der Server schickt. Ein
Dokument mit Kreisen bleibt liegen. (1) Die Datei fehlt, die Konfiguration ist aber eingerichtet (Stand vor der Rastung):
die erste Rastung kommt aus der bestaetigten, lokal geprueften Konfiguration. (2) Die Datei hat das alte Format (ein Paar
aus Hebelsatz und Verteilsystem): sie wird zur Liste mit einem Kreis, aber nur, wenn die Konfiguration genau einen Kreis
hat und dessen Paar dem alten gleicht; vorher entsteht die Sicherung `sicherheitssatz.json.pre-<Release>` (Rueckweg:
zurueckspielen, der alte Stand liest das alte Format). Eine vorhandene Sicherung wird nie ueberschrieben.
Laeuft in der Anlage ein Boost (Komfort oder Notfall), verweigert der Lauf, bevor er etwas aendert: danach wartet das
Geraet still auf das Dokument des Servers, und die Anlage bliebe ohne lokale Sicherheit auf dem Boost stehen.

Die Migration lockert nie: eine kaputte, unlesbare, ueber einen Symlink erreichte oder sonst gesperrte Datei bleibt, wie
sie ist; eine gueltige Datei bleibt unberuehrt. Ausgegeben wird je Lauf ein Code ohne Namen (Exit 0 bei einem
Erfolgscode, sonst 1)."""
import argparse
import logging
import os
import re
import sys
from pathlib import Path

from smartheat_core import config_check, dateien, rastung, wallclock, zustandspfade
from smartheat_core.verfahren import WegUnbekannt, verfahren_fuer

logger = logging.getLogger(__name__)

HOSTS = (config_check.HOST_GATEWAY, config_check.HOST_HA)

AUS_KONFIGURATION = "migriert_aus_konfiguration"
ALTES_FORMAT = "migriert_altes_format"
BEREITS_AKTUELL = "bereits_aktuell"
ERFOLG = (AUS_KONFIGURATION, ALTES_FORMAT, BEREITS_AKTUELL)

RELEASE_UNGUELTIG = "release_ungueltig"
HOST_UNBEKANNT = "host_unbekannt"
ORDNER_FEHLT = "ordner_fehlt"
KONFIGURATION_NICHT_EINGERICHTET = "konfiguration_nicht_eingerichtet"
KONFIGURATION_UNGUELTIG = "konfiguration_ungueltig"
RASTUNG_GESPERRT = "rastung_gesperrt"
ALTES_FORMAT_MEHRERE_KREISE = "altes_format_mehrere_kreise"
ALTES_FORMAT_PASST_NICHT = "altes_format_passt_nicht"
SICHERUNG_VORHANDEN = "sicherung_vorhanden"
BOOST_AKTIV = "boost_aktiv"
SCHREIBEN_FEHLGESCHLAGEN = "schreiben_fehlgeschlagen"

_RELEASE = re.compile(r"[0-9A-Za-z._-]{1,32}")
_BOOST_FELDER = ("boost_active", "emergency_boost_active")
_KONFIGURATION = "konfiguration.json"
#: Die flache Form des Stands vor den Kreisen: Pflichtfelder, dazu die, die fehlen duerfen. Jedes andere Feld macht die
#: Konfiguration ungueltig (nichts wird geraten); `profil` war die abgeleitete Bezeichnung und entfaellt.
_FLACH_PFLICHT = ("anlage", "abo", "bindung", "hebelsatz", "verteilsystem", "raumfuehler", "tagestick")
_FLACH_OPTIONAL = ("profil", "mindest_software", "mindest_integration", "software", "bindungsparameter",
                   "ursprungswerte")
_FLACH_ANLAGENWEIT = ("anlage", "tagestick", "mindest_software", "mindest_integration", "software",
                      "bindungsparameter")


def _weg(bindung, host: str) -> str | None:
    """Der Anbindungsweg einer flachen Bindung nach den Tabellen der Pruefung; None: keine bekannte Bindung."""
    if host == config_check.HOST_HA and isinstance(bindung.get("kreis"), dict):
        return config_check.WEG_DER_PLATTFORM.get(bindung["kreis"].get("plattform"))
    if host == config_check.HOST_GATEWAY and isinstance(bindung.get("treiber"), dict):
        return config_check.WEG_DES_TREIBERS.get(bindung["treiber"].get("id"))
    return None


def _parameter_zum_pruefen(parameter, weg: str, hebelsatz) -> object:
    """Die Bindungsparameter des alten Stands, so weit angepasst, dass die Pruefung sie liest: der alte Server kannte die
    lokale Mindestliste der zurueckzustellenden Hebel noch nicht (Vaillant: nur der Hebel heat_limit). Die Liste wird
    um diese Mindestliste ergaenzt; jede andere Abweichung bleibt ein Fehler. Die Rastung haengt nicht von den
    Parametern ab, und das Dokument mit Kreisen, das der Server danach schickt, traegt seine eigenen und wird voll geprueft."""
    if not isinstance(parameter, dict) or not isinstance(parameter.get("zurueck"), list):
        return parameter
    try:
        verfahren = verfahren_fuer(weg, hebelsatz)
    except WegUnbekannt:
        return parameter
    if verfahren is None:
        return parameter
    fehlend = [hebel for hebel in verfahren.rueckstellung_pflicht if hebel not in parameter["zurueck"]]
    return {**parameter, "zurueck": [*parameter["zurueck"], *fehlend]}


def _aus_flacher_form(flach: dict, host: str) -> dict | None:
    """Die flache Konfiguration als Dokument mit genau einem Kreis und einem Referenzraum; None, wenn sie nicht genau
    die Felder des Stands vor den Kreisen hat. Fuehler, Bindung, Hebelsatz, Verteilsystem, Abo und Ursprungswerte
    uebernimmt sie unveraendert, also beschreibt die Rastung daraus nie mehr als die Konfiguration selbst."""
    if set(_FLACH_PFLICHT) - set(flach) or set(flach) - set(_FLACH_PFLICHT) - set(_FLACH_OPTIONAL):
        return None
    bindung, fuehler = flach["bindung"], flach["raumfuehler"]
    if not isinstance(bindung, dict) or not isinstance(fuehler, list) or not isinstance(flach["anlage"], str):
        return None
    weg = _weg(bindung, host)
    if weg is None:
        return None
    kreis = {"kreis": 1, "name": "Kreis 1", "bindung": bindung, "hebelsatz": flach["hebelsatz"],
             "verteilsystem": flach["verteilsystem"], "abo": flach["abo"],
             "raeume": [{"raum": 1, "name": "Raum 1", "rolle": "referenz", "fuehler": fuehler, "soll_quelle": False,
                         "versatz_k": 0.0, "ziel_fest": None}]}
    if "ursprungswerte" in flach:
        kreis["ursprungswerte"] = flach["ursprungswerte"]
    neu = {"anlage_name": flach["anlage"], "weg": weg, "kreise": [kreis]}
    neu.update({feld: flach.get(feld) for feld in _FLACH_ANLAGENWEIT})
    neu["bindungsparameter"] = _parameter_zum_pruefen(neu["bindungsparameter"], weg, flach["hebelsatz"])
    return neu


def boost_aktiv(daten: Path, host: str) -> bool:
    """True, wenn der Zustand der Anlage einen laufenden Boost (Komfort oder Notfall) fuehrt: `backup.json` am alten Ort
    des Hosts (HA: im Datenordner, Gateway: in runtime/) oder in einem Kreisordner. Eine fehlende oder unlesbare Datei
    fuehrt keinen Boost; jeder Wert ausser False zaehlt als aktiv."""
    runtime = daten / "runtime"
    kandidaten = [(daten if host == config_check.HOST_HA else runtime) / zustandspfade.BACKUP]
    kandidaten += [ordner / zustandspfade.BACKUP for ordner in zustandspfade.kreisordner(runtime)]
    for pfad in kandidaten:
        roh = dateien.lesen_json(pfad)
        if isinstance(roh, dict) and any(roh.get(feld) not in (None, False) for feld in _BOOST_FELDER):
            return True
    return False


def _sicherung(ordner: Path, release: str) -> Path:
    return ordner / f"{_KONFIGURATION}.pre-{release}"


def bestaetigte_konfiguration(ordner: Path, host: str, *, release: str | None = None):
    """(Inhalt, None) oder (None, Code): eingerichtet, in der Form mit Kreisen und von der lokalen Pruefung ohne
    Rastung angenommen; eine flache Konfiguration des Stands vor den Kreisen kommt umgeformt zurueck. Mit `release`
    gilt bei fehlender Datei die Sicherung dieses Release (die Rastungsmigration hat die flache Datei beiseite gestellt)."""
    roh = dateien.lesen_json(ordner / _KONFIGURATION)
    if roh is None and release is not None:
        roh = dateien.lesen_json(_sicherung(ordner, release))
    if roh is None:
        return None, KONFIGURATION_NICHT_EINGERICHTET
    version = roh.get("version") if isinstance(roh, dict) else None
    inhalt = roh.get("inhalt") if isinstance(roh, dict) else None
    if not (isinstance(version, int) and not isinstance(version, bool) and version >= 1 and isinstance(inhalt, dict)):
        return None, KONFIGURATION_UNGUELTIG
    if config_check.ist_leer(inhalt):
        return None, KONFIGURATION_NICHT_EINGERICHTET
    if "kreise" not in inhalt:
        inhalt = _aus_flacher_form(inhalt, host)
        if inhalt is None:
            return None, KONFIGURATION_UNGUELTIG
    if config_check.pruefen(inhalt, None, host=host) is not None:
        return None, KONFIGURATION_UNGUELTIG
    return inhalt, None


def _schreiben(pfad: Path, neu: rastung.Rastung, erfolg: str) -> str:
    try:
        rastung.schreiben(pfad, neu, wallclock.now().isoformat(timespec="seconds"))
    except (OSError, ValueError):
        return SCHREIBEN_FEHLGESCHLAGEN
    return erfolg


def _ist_flach(ordner: Path) -> bool:
    roh = dateien.lesen_json(ordner / _KONFIGURATION)
    inhalt = roh.get("inhalt") if isinstance(roh, dict) else None
    return isinstance(inhalt, dict) and "kreise" not in inhalt


def _sicherung_belegt(ordner: Path, release: str) -> bool:
    sicherung = _sicherung(ordner, release)
    return sicherung.is_symlink() or sicherung.exists()


def _beiseite(ordner: Path, release: str) -> str | None:
    """Sichert die flache Konfiguration als `konfiguration.json.pre-<Release>` und entfernt sie dann; erst danach gilt
    der neue Stand. None bei Erfolg, sonst der Fehlercode (ein zweiter Lauf holt das Entfernen nach)."""
    pfad = ordner / _KONFIGURATION
    try:
        if _sicherung_belegt(ordner, release):
            return SICHERUNG_VORHANDEN
        dateien.schreiben(_sicherung(ordner, release), pfad.read_bytes(), modus=dateien.PRIVAT)
        os.unlink(pfad)
    except OSError:
        return SCHREIBEN_FEHLGESCHLAGEN
    return None


def _nachholen(ordner: Path, host: str, release: str) -> str:
    """Die Rastung ist schon aktuell: eine gueltige flache Konfiguration, die noch da liegt (ein frueherer Lauf scheiterte
    beim Beiseitestellen), wird jetzt beiseite gestellt."""
    inhalt, _ = bestaetigte_konfiguration(ordner, host)
    if inhalt is None or not _ist_flach(ordner):
        return BEREITS_AKTUELL
    if boost_aktiv(ordner.parent, host):
        return BOOST_AKTIV
    return _beiseite(ordner, release) or BEREITS_AKTUELL


def _altes_format(pfad: Path, release: str, neu: rastung.Rastung) -> str:
    paar = rastung.altes_paar(pfad)
    if paar is None:
        return RASTUNG_GESPERRT
    if len(neu.kreise) != 1:
        return ALTES_FORMAT_MEHRERE_KREISE
    if paar != (neu.kreise[0].hebelsatz, neu.kreise[0].verteilsystem):
        return ALTES_FORMAT_PASST_NICHT
    sicherung = pfad.with_name(f"{pfad.name}.pre-{release}")
    try:
        if sicherung.is_symlink() or sicherung.exists():
            return SICHERUNG_VORHANDEN
        dateien.schreiben(sicherung, pfad.read_bytes(), modus=dateien.PRIVAT)
    except OSError:
        return SCHREIBEN_FEHLGESCHLAGEN
    return _schreiben(pfad, neu, ALTES_FORMAT)


def migrieren(ordner: Path, *, host: str, release: str) -> str:
    """Der Code des Laufs (siehe Konstanten). Ändert nichts, wenn der Code kein Erfolgscode ist."""
    if not _RELEASE.fullmatch(release):
        return RELEASE_UNGUELTIG
    if host not in HOSTS:
        return HOST_UNBEKANNT
    if not ordner.is_dir():
        return ORDNER_FEHLT
    pfad = ordner / rastung.DATEI
    gelesen = rastung.lesen(pfad)
    if gelesen is not None and not gelesen.sperre:
        return _nachholen(ordner, host, release)
    if gelesen is not None and gelesen.sperre != "altes_format":
        return RASTUNG_GESPERRT
    inhalt, code = bestaetigte_konfiguration(ordner, host)
    if inhalt is None:
        return code or KONFIGURATION_UNGUELTIG
    neu = rastung.aus_konfiguration(inhalt)
    if neu is None:
        return KONFIGURATION_UNGUELTIG
    if boost_aktiv(ordner.parent, host):
        return BOOST_AKTIV
    flach = _ist_flach(ordner)
    if flach and _sicherung_belegt(ordner, release):
        return SICHERUNG_VORHANDEN  # vor jeder Aenderung
    code = _schreiben(pfad, neu, AUS_KONFIGURATION) if gelesen is None else _altes_format(pfad, release, neu)
    if flach and code in ERFOLG:
        return _beiseite(ordner, release) or code
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="smartheat_device.rastung_migration", description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--device-dir", required=True, type=Path)
    parser.add_argument("--host", required=True)
    parser.add_argument("--release", required=True)
    args = parser.parse_args(argv)
    code = migrieren(args.device_dir, host=args.host, release=args.release)
    print(code)
    return 0 if code in ERFOLG else 1


if __name__ == "__main__":
    sys.exit(main())
