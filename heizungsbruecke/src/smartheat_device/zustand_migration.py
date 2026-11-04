"""Einmalige Migration des Zustands im Wartungsfenster, von Hand aufgerufen (kein Teil des Normalstarts):

    python -m smartheat_device.zustand_migration --device-dir <Ordner> --host ha|gateway --release <Release>

Der Stand vor der Umstellung legte Wiederherstellungspunkt, Failsafe-Zustand, Abo-Zustand und Raum-Soll der Anlage in
einzelnen Dateien ab (HA: die ersten drei in /data, das Raum-Soll in /data/runtime; Gateway: alle vier in
/data/runtime). Seitdem hat jeder Heizkreis seinen Ordner `runtime/kreis-<n>/`; die Hosts fuehren heute genau einen
Kreis, also `kreis-1`. Der neue Code liest die alten Orte nie, und eine Sicherung am alten Ort ohne Kreisordner laesst die
Laufzeit nicht starten (Grund `zustand_nicht_migriert`).

Der Lauf sichert jede vorhandene alte Datei als `<Datei>.pre-<Release>` (Rueckweg: zurueckspielen, Kreisordner
loeschen; der alte Stand liest die alten Orte), schreibt die Dateien in den Kreisordner und entfernt erst dann die alten.
Die bestaetigte Konfiguration dafuer kommt aus `konfiguration.json` oder, wenn die Rastungsmigration eine flache Datei
schon beiseite gestellt hat, aus `konfiguration.json.pre-<Release>`.
Die Kennung der Anlage in der Sicherung (`plant_id`) wird von der alten Form (Plattform, Anlage, Nummer bzw. Treiber und
seine Parameter) in die neue umgeschrieben (`smartheat_core.kennung`), aber nur, wenn sie der alten Form dieser
Konfiguration gleicht; jede andere Kennung bleibt, wie sie ist, die Bindung an das Konto entscheidet beim Start. Eine
vorhandene Sicherung oder Zieldatei wird nie ueberschrieben, eine Datei ueber einen Symlink oder mit unbrauchbarem
Inhalt bricht den Lauf vor jeder Aenderung ab; scheitert das Schreiben, werden die neuen Dateien und Sicherungen dieses
Laufs wieder entfernt. Ausgegeben wird je Lauf ein Code ohne Namen (Exit 0 bei einem Erfolgscode, sonst 1)."""
import argparse
import contextlib
import json
import logging
import os
import re
import sys
from pathlib import Path

from smartheat_core import config_check, dateien, kennung, kreisansicht, zustandspfade
from smartheat_device import rastung_migration

logger = logging.getLogger(__name__)

HOSTS = (config_check.HOST_GATEWAY, config_check.HOST_HA)
#: Der eine Kreis, den die Hosts heute fuehren.
KREIS = 1

MIGRIERT = "migriert"
BEREITS_AKTUELL = "bereits_aktuell"
ERFOLG = (MIGRIERT, BEREITS_AKTUELL)

RELEASE_UNGUELTIG = "release_ungueltig"
HOST_UNBEKANNT = "host_unbekannt"
ORDNER_FEHLT = "ordner_fehlt"
KONFIGURATION_NICHT_EINGERICHTET = rastung_migration.KONFIGURATION_NICHT_EINGERICHTET
KONFIGURATION_UNGUELTIG = rastung_migration.KONFIGURATION_UNGUELTIG
KENNUNG_UNGUELTIG = "kennung_ungueltig"
DATEI_UNGUELTIG = "datei_ungueltig"
ZIEL_VORHANDEN = "ziel_vorhanden"
SICHERUNG_VORHANDEN = "sicherung_vorhanden"
SCHREIBEN_FEHLGESCHLAGEN = "schreiben_fehlgeschlagen"

_RELEASE = re.compile(r"[0-9A-Za-z._-]{1,32}")
# Die Felder, die die alte Kennung eines HA-Kreises bildeten.
_ALT_HA = ("plattform", "anlage", "nummer")


def alte_pfade(daten: Path, host: str) -> dict[str, Path]:
    """Wo der Stand vor der Umstellung die vier Dateien ablegte, je Dateiname des Kreisordners."""
    runtime = daten / "runtime"
    ort = daten if host == config_check.HOST_HA else runtime
    return {zustandspfade.BACKUP: ort / "backup.json", zustandspfade.FAILSAFE: ort / "failsafe_state.json",
            zustandspfade.ENTITLEMENT: ort / "entitlement_state.json", zustandspfade.ROOM_TARGET: runtime / "room_target.json"}


def zielordner(daten: Path) -> Path:
    return zustandspfade.kreis_dir(daten / "runtime", KREIS)


def _vorhanden(pfad: Path) -> bool:
    return pfad.is_symlink() or pfad.exists()


def _kennungen(inhalt: dict, host: str) -> tuple[str, str]:
    """(alte, neue) Kennung der Anlage aus der bestaetigten Konfiguration; KennungFehler, wenn sie keine ergibt."""
    bindung = kreisansicht.erste(inhalt).get("bindung")
    if not isinstance(bindung, dict):
        raise kennung.KennungFehler("Bindung fehlt")
    if host == config_check.HOST_HA:
        kreis = bindung.get("kreis")
        if not isinstance(kreis, dict):
            raise kennung.KennungFehler("Kreis fehlt")
        neu = kennung.aus_ha_kreis(kreis.get("plattform"), kreis.get("anlage"), kreis.get("nummer"))
        return json.dumps({key: kreis.get(key) for key in _ALT_HA}, sort_keys=True), neu
    treiber = bindung.get("treiber")
    if not isinstance(treiber, dict):
        raise kennung.KennungFehler("Treiber fehlt")
    treiber_id, parameter = treiber.get("id"), treiber.get("parameter")
    if not isinstance(treiber_id, str) or not isinstance(parameter, dict):
        raise kennung.KennungFehler("Treiber ungueltig")
    neu = kennung.aus_treiber(treiber_id, parameter)
    namen = (*kennung.TREIBER[treiber_id][1], "heizkreis")
    return json.dumps({"id": treiber_id, **{name: parameter.get(name) for name in namen}}, sort_keys=True), neu


def _neue_sicherung(roh: bytes, ordner: Path, host: str, release: str) -> bytes | str:
    """Der Inhalt der neuen `backup.json` oder der Fehlercode."""
    try:
        werte = json.loads(roh)
    except ValueError:
        return DATEI_UNGUELTIG
    if not isinstance(werte, dict):
        return DATEI_UNGUELTIG
    alt = werte.get("plant_id")
    if alt is None:
        return roh
    if not isinstance(alt, str) or not alt:
        return DATEI_UNGUELTIG
    inhalt, code = rastung_migration.bestaetigte_konfiguration(ordner, host, release=release)
    if inhalt is None:
        return code or KONFIGURATION_UNGUELTIG
    try:
        alte_kennung, neue_kennung = _kennungen(inhalt, host)
    except kennung.KennungFehler:
        return KENNUNG_UNGUELTIG
    if alt == alte_kennung:
        werte["plant_id"] = neue_kennung
        return json.dumps(werte).encode()
    return roh


def _zuruecknehmen(angelegt: list[Path], ziel: Path, ziel_neu: bool) -> None:
    for pfad in reversed(angelegt):
        with contextlib.suppress(OSError):
            pfad.unlink()
    if ziel_neu:
        with contextlib.suppress(OSError):
            ziel.rmdir()


def migrieren(ordner: Path, *, host: str, release: str) -> str:
    """Der Code des Laufs (siehe Konstanten). Ändert nichts, wenn der Code kein Erfolgscode ist, ausser bei
    `schreiben_fehlgeschlagen` nach dem Schreiben der neuen Dateien (siehe unten)."""
    if not _RELEASE.fullmatch(release):
        return RELEASE_UNGUELTIG
    if host not in HOSTS:
        return HOST_UNBEKANNT
    if not ordner.is_dir():
        return ORDNER_FEHLT
    daten = ordner.parent
    ziel = zielordner(daten)
    alt = {name: pfad for name, pfad in alte_pfade(daten, host).items() if _vorhanden(pfad)}
    if not alt:
        return BEREITS_AKTUELL
    if any(pfad.is_symlink() or not pfad.is_file() for pfad in alt.values()):
        return DATEI_UNGUELTIG
    if any(_vorhanden(ziel / name) for name in alt):
        return ZIEL_VORHANDEN
    sicherungen = {name: pfad.with_name(f"{pfad.name}.pre-{release}") for name, pfad in alt.items()}
    if any(_vorhanden(pfad) for pfad in sicherungen.values()):
        return SICHERUNG_VORHANDEN
    try:
        inhalte = {name: pfad.read_bytes() for name, pfad in alt.items()}
    except OSError:
        return DATEI_UNGUELTIG
    neu: dict[str, bytes] = dict(inhalte)
    if zustandspfade.BACKUP in alt:
        sicherung = _neue_sicherung(inhalte[zustandspfade.BACKUP], ordner, host, release)
        if isinstance(sicherung, str):
            return sicherung
        neu[zustandspfade.BACKUP] = sicherung
    ziel_neu = not ziel.exists()
    angelegt: list[Path] = []
    try:
        for name in alt:
            dateien.schreiben(sicherungen[name], inhalte[name], modus=dateien.PRIVAT)
            angelegt.append(sicherungen[name])
        for name in alt:
            dateien.schreiben(ziel / name, neu[name], modus=dateien.PRIVAT)
            angelegt.append(ziel / name)
    except OSError:
        _zuruecknehmen(angelegt, ziel, ziel_neu)
        return SCHREIBEN_FEHLGESCHLAGEN
    # Erst jetzt entfernen: bis hierher ist der alte Stand vollstaendig, danach der neue. Scheitert eine Entfernung,
    # bleibt der neue Stand vollstaendig neben dem Rest des alten; der Betreiber raeumt von Hand auf.
    try:
        for pfad in alt.values():
            os.unlink(pfad)
    except OSError:
        return SCHREIBEN_FEHLGESCHLAGEN
    return MIGRIERT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="smartheat_device.zustand_migration", description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--device-dir", required=True, type=Path)
    parser.add_argument("--host", required=True)
    parser.add_argument("--release", required=True)
    args = parser.parse_args(argv)
    code = migrieren(args.device_dir, host=args.host, release=args.release)
    print(code)
    return 0 if code in ERFOLG else 1


if __name__ == "__main__":
    sys.exit(main())
