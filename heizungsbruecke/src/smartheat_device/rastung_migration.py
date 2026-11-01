"""Einmalige Migration der Rastung im Wartungsfenster, von Hand aufgerufen (kein Teil des Normalstarts):

    python -m smartheat_device.rastung_migration --device-dir <Ordner> --host ha|gateway --release <Release>

Sie deckt genau zwei Faelle ab. (1) Die Datei fehlt, die Konfiguration ist aber eingerichtet (Stand vor der Rastung):
die erste Rastung kommt aus der bestaetigten, lokal geprueften Konfiguration. (2) Die Datei hat das alte Format (ein Paar
aus Hebelsatz und Verteilsystem): sie wird zur Liste mit einem Kreis, aber nur, wenn die Konfiguration genau einen Kreis
hat und dessen Paar dem alten gleicht; vorher entsteht die Sicherung `sicherheitssatz.json.pre-<Release>` (Rueckweg:
zurueckspielen, der alte Stand liest das alte Format). Eine vorhandene Sicherung wird nie ueberschrieben.

Die Migration lockert nie: eine kaputte, unlesbare, ueber einen Symlink erreichte oder sonst gesperrte Datei bleibt, wie
sie ist; eine gueltige Datei bleibt unberuehrt. Ausgegeben wird je Lauf ein Code ohne Namen (Exit 0 bei einem
Erfolgscode, sonst 1)."""
import argparse
import logging
import re
import sys
from pathlib import Path

from smartheat_core import config_check, dateien, rastung, wallclock

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
SCHREIBEN_FEHLGESCHLAGEN = "schreiben_fehlgeschlagen"

_RELEASE = re.compile(r"[0-9A-Za-z._-]{1,32}")
_KONFIGURATION = "konfiguration.json"


def _bestaetigte_konfiguration(ordner: Path, host: str):
    """(Inhalt, None) oder (None, Code): eingerichtet, in der Form des Speichers und von der lokalen Pruefung ohne
    Rastung angenommen."""
    roh = dateien.lesen_json(ordner / _KONFIGURATION)
    if roh is None:
        return None, KONFIGURATION_NICHT_EINGERICHTET
    version = roh.get("version") if isinstance(roh, dict) else None
    inhalt = roh.get("inhalt") if isinstance(roh, dict) else None
    if not (isinstance(version, int) and not isinstance(version, bool) and version >= 1 and isinstance(inhalt, dict)):
        return None, KONFIGURATION_UNGUELTIG
    if config_check.ist_leer(inhalt):
        return None, KONFIGURATION_NICHT_EINGERICHTET
    if config_check.pruefen(inhalt, None, host=host) is not None:
        return None, KONFIGURATION_UNGUELTIG
    return inhalt, None


def _schreiben(pfad: Path, neu: rastung.Rastung, erfolg: str) -> str:
    try:
        rastung.schreiben(pfad, neu, wallclock.now().isoformat(timespec="seconds"))
    except (OSError, ValueError):
        return SCHREIBEN_FEHLGESCHLAGEN
    return erfolg


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
        return BEREITS_AKTUELL
    if gelesen is not None and gelesen.sperre != "altes_format":
        return RASTUNG_GESPERRT
    inhalt, code = _bestaetigte_konfiguration(ordner, host)
    if inhalt is None:
        return code or KONFIGURATION_UNGUELTIG
    neu = rastung.aus_konfiguration(inhalt)
    if neu is None:
        return KONFIGURATION_UNGUELTIG
    if gelesen is None:
        return _schreiben(pfad, neu, AUS_KONFIGURATION)
    return _altes_format(pfad, release, neu)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="smartheat_device.rastung_migration", description=__doc__.split("\n")[0])
    parser.add_argument("--device-dir", required=True, type=Path)
    parser.add_argument("--host", required=True)
    parser.add_argument("--release", required=True)
    args = parser.parse_args(argv)
    code = migrieren(args.device_dir, host=args.host, release=args.release)
    print(code)
    return 0 if code in ERFOLG else 1


if __name__ == "__main__":
    sys.exit(main())
