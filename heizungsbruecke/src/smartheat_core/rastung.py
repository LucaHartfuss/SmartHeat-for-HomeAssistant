"""Rastung des Sicherheitssatzes (G-1) je Kreis: Hebelsatz, Verteilsystem, Schreibziele und Soll-Entity der ersten
eingerichteten Konfiguration, im Geraeteordner neben der Identitaet (device/sicherheitssatz.json). Kein Dokument des
Servers loest sie, auch keine leere Konfiguration; sie geht nur mit den Geraetedaten (Add-on neu installieren, Gateway
neu aufsetzen). Eine kaputte oder manipulierte Datei (auch ein Symlink) sperrt: UNBEKANNT passt zu keiner Konfiguration
(der Client ist quelloffen; wer weiss, wie man die Datei beschaedigt, darf sie damit nicht loesen). Dasselbe gilt fuer
das alte Format (ein Paar ohne Liste), nur mit eigenem Code: es wird nie stillschweigend gelesen, die einmalige Migration
im Wartungsfenster schreibt es um (smartheat_device.rastung_migration).

Ein Dokument passt, wenn jeder seiner Kreise einen noch nicht verbrauchten Eintrag mit demselben Hebelsatz,
Verteilsystem, denselben Schreibzielen und derselben Soll-Entity findet. Die Kreisnummer gehoert nicht dazu: der Server
vergibt sie, das Geraet haelt die Anlage fest. Weniger Kreise als Eintraege gehen (ein Kreis darf entfallen), mehr Kreise
oder ein Kreis ohne Eintrag nicht. Schreibziele sind beim Gateway der Treiber samt Parametern (auch dem Heizkreis) und
das Thermostat, bei Home Assistant der Kreis samt Config-Entry und die Entities der Schreibrollen. Lesende Rollen
(Aussentemperatur, Fuehler) gehoeren nicht dazu: ein Sensortausch soll keine Neueinrichtung des Geraets brauchen. Rein:
Standardbibliothek und smartheat_core."""
import json
import logging
import os
import stat
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from smartheat_core import dateien, kreisansicht
from smartheat_core.safety import resolve_local_safety

logger = logging.getLogger(__name__)

DATEI = "sicherheitssatz.json"
TEXT = ("Hebelsatz, Verteilsystem und Anschluss der Kreise sind auf diesem Gerät festgelegt. Ändern lassen sie sich nur, "
        "wenn das Add-on neu installiert bzw. das Gateway neu aufgesetzt wird.")
TEXT_ALTES_FORMAT = ("Hebelsatz und Verteilsystem sind auf diesem Gerät festgelegt, aber noch im alten Format "
                     "gespeichert (ein Paar ohne Kreise). Sie werden im Wartungsfenster mit der Migration der Rastung "
                     "in die Liste je Kreis übernommen.")
#: Vier Kreise mit je acht Schreibzielen und langen Entity-IDs liegen weit darunter; mehr wird nie gelesen.
_MAX_BYTES = 32768
#: Rollen der HA-Bindung (V1-Namen), die die Regelung schreibt; Vereinigung ueber alle Hebelsaetze (Hebel des
#: Hebelsatzes und weitere Schreibziele). Ein Test haelt sie gegen die Schreibregeln des HA-Hosts.
SCHREIBROLLEN_HA = ("curve", "room_setpoint", "level", "heat_limit", "min_flow", "mode_select", "setpoint_comfort",
                    "setpoint_setback")
_SOLL_ROLLE = "room_target"


@dataclass(frozen=True)
class KreisRastung:
    kreis: int
    hebelsatz: str
    verteilsystem: str
    ziele: tuple[str, ...]  # sortiert und eindeutig
    soll_entity: str | None


@dataclass(frozen=True)
class Rastung:
    """Eintraege je Kreis. Ohne Eintraege (UNBEKANNT) ist die Rastung gesperrt; `sperre` nennt dann den Code (nur fuer
    Meldung und Log, kein Teil der Gleichheit)."""
    kreise: tuple[KreisRastung, ...]
    sperre: str = field(default="", compare=False)


UNBEKANNT = Rastung(())


class _Ungueltig(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _gesperrt(code: str) -> Rastung:
    logger.error("Rastung des Sicherheitssatzes gesperrt (%s): jede Einrichtung wird abgelehnt", code)
    return replace(UNBEKANNT, sperre=code)


def _json(pfad: Path):
    """Folgt keinem Symlink am letzten Namen (O_NOFOLLOW: ELOOP, auch bei einem ins Leere zeigenden Link). O_NONBLOCK:
    eine FIFO ohne Schreiber blockierte sonst das open (und den Start); nur eine regulaere Datei zaehlt."""
    fd = os.open(pfad, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as datei:
        if not stat.S_ISREG(os.fstat(datei.fileno()).st_mode):
            raise OSError("keine regulaere Datei")
        return json.loads(datei.read(_MAX_BYTES + 1).decode())


def _nummer(wert, kleinste: int) -> bool:
    return isinstance(wert, int) and not isinstance(wert, bool) and wert >= kleinste


def _kanonisch(ziele) -> bool:
    return (isinstance(ziele, list) and bool(ziele) and all(isinstance(z, str) and z for z in ziele)
            and ziele == sorted(set(ziele)))


def _altes_format(roh) -> bool:
    return (isinstance(roh, dict) and "kreise" not in roh and isinstance(roh.get("hebelsatz"), str)
            and isinstance(roh.get("verteilsystem"), str))


def _eintrag(roh) -> KreisRastung:
    if not (isinstance(roh, dict) and _nummer(roh.get("kreis"), 1) and isinstance(roh.get("hebelsatz"), str)
            and isinstance(roh.get("verteilsystem"), str) and _kanonisch(roh.get("ziele"))
            and "soll_entity" in roh and (roh["soll_entity"] is None or isinstance(roh["soll_entity"], str))):
        raise _Ungueltig("unvollstaendig")
    try:
        resolve_local_safety(roh["hebelsatz"], roh["verteilsystem"])
    except ValueError:
        raise _Ungueltig("ohne_sicherheitswerte") from None
    return KreisRastung(roh["kreis"], roh["hebelsatz"], roh["verteilsystem"], tuple(roh["ziele"]), roh["soll_entity"])


def lesen(pfad: Path) -> Rastung | None:
    """None: keine Rastung (Datei fehlt). UNBEKANNT (mit `sperre`): Datei da, aber Symlink, unlesbar, im alten Format,
    unvollstaendig oder ohne lokale Werte."""
    try:
        roh = _json(pfad)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, RecursionError):
        return _gesperrt("nicht_lesbar")
    if _altes_format(roh):
        return _gesperrt("altes_format")
    liste = roh.get("kreise") if isinstance(roh, dict) else None
    if not (isinstance(liste, list) and liste):
        return _gesperrt("unvollstaendig")
    try:
        return Rastung(tuple(_eintrag(eintrag) for eintrag in liste))
    except _Ungueltig as fehler:
        return _gesperrt(fehler.code)


def altes_paar(pfad: Path) -> tuple[str, str] | None:
    """Hebelsatz und Verteilsystem einer Datei im alten Format mit gueltigen lokalen Werten, sonst None. Nur fuer die
    einmalige Migration; die Laufzeit liest das alte Format nie."""
    try:
        roh = _json(pfad)
        if not _altes_format(roh):
            return None
        resolve_local_safety(roh["hebelsatz"], roh["verteilsystem"])
    except (OSError, ValueError, RecursionError):
        return None
    return roh["hebelsatz"], roh["verteilsystem"]


def _ziele_gateway(bindung: Mapping, treiber: Mapping) -> tuple[str, ...] | None:
    kennung, parameter, thermostat = treiber.get("id"), treiber.get("parameter"), bindung.get("thermostat")
    if not (isinstance(kennung, str) and isinstance(parameter, Mapping)
            and (thermostat is None or isinstance(thermostat, str))):
        return None
    try:
        text = json.dumps(parameter, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError, RecursionError):
        return None
    ziele = {f"treiber={kennung}", f"parameter={text}"}
    if thermostat is not None:
        ziele.add(f"thermostat={thermostat}")
    return tuple(sorted(ziele))


def _ziele_ha(kreis: Mapping, rollen: Mapping) -> tuple[str, ...] | None:
    plattform, anlage, nummer, eintrag = (kreis.get(feld) for feld in ("plattform", "anlage", "nummer", "config_entry"))
    if not (isinstance(plattform, str) and isinstance(anlage, str) and isinstance(nummer, str)
            and (eintrag is None or isinstance(eintrag, str))):
        return None
    ziele = {"kreis=" + json.dumps([plattform, anlage, nummer, eintrag])}
    for rolle in (*SCHREIBROLLEN_HA, _SOLL_ROLLE):
        if rolle not in rollen:
            continue
        if not isinstance(rollen[rolle], str):
            return None
        if rolle != _SOLL_ROLLE:
            ziele.add(f"{rolle}={rollen[rolle]}")
    return tuple(sorted(ziele))


def ziele(bindung) -> tuple[str, ...] | None:
    """Die Schreibziele einer Kreisbindung, sortiert und eindeutig; None, wenn die Form nicht lesbar ist (die Form der
    Bindung entscheidet ueber den Host: `treiber` Gateway, `kreis` mit `rollen` Home Assistant)."""
    if not isinstance(bindung, Mapping):
        return None
    treiber = bindung.get("treiber")
    if isinstance(treiber, Mapping):
        return _ziele_gateway(bindung, treiber)
    kreis, rollen = bindung.get("kreis"), bindung.get("rollen")
    if isinstance(kreis, Mapping) and isinstance(rollen, Mapping):
        return _ziele_ha(kreis, rollen)
    return None


def soll_entity(bindung) -> str | None:
    """Die Soll-Entity einer HA-Bindung (Rolle room_target, mit `::Attribut`), sonst None (das Gateway hat keine)."""
    rollen = bindung.get("rollen") if isinstance(bindung, Mapping) else None
    soll = rollen.get(_SOLL_ROLLE) if isinstance(rollen, Mapping) else None
    return soll if isinstance(soll, str) else None


def aus_konfiguration(konfiguration) -> Rastung | None:
    """Die Rastung einer geprueften Konfiguration; None, wenn ein Kreis nicht lesbar ist oder es keinen gibt."""
    eintraege = []
    for kreis in kreisansicht.kreise(konfiguration):
        nummer, hebelsatz, verteilsystem = kreis.get("kreis"), kreis.get("hebelsatz"), kreis.get("verteilsystem")
        bindung = kreis.get("bindung")
        ziel = ziele(bindung)
        if not (ziel and isinstance(nummer, int) and _nummer(nummer, 1)
                and isinstance(hebelsatz, str) and isinstance(verteilsystem, str)):
            return None
        try:
            resolve_local_safety(hebelsatz, verteilsystem)
        except ValueError:
            return None
        eintraege.append(KreisRastung(nummer, hebelsatz, verteilsystem, ziel, soll_entity(bindung)))
    return Rastung(tuple(eintraege)) if eintraege else None


def hebel_passt(rastung: Rastung | None, hebelsatz, verteilsystem) -> bool:
    """Nur das Paar: kennt irgendein Eintrag es? (Vorabpruefung vor den Bindungen; die Ziele pruefen `passt`.)"""
    if rastung is None:
        return True
    return any((kreis.hebelsatz, kreis.verteilsystem) == (hebelsatz, verteilsystem) for kreis in rastung.kreise)


def passt(rastung: Rastung | None, konfiguration) -> bool:
    """Das ganze Dokument gegen die Rastung. Ein Dokument ohne lesbaren Kreis passt nie zu einer vorhandenen Rastung:
    leer lockert nichts."""
    if rastung is None:
        return True
    gesucht = Counter((k.hebelsatz, k.verteilsystem, k.ziele, k.soll_entity) for k in rastung.kreise)
    verlangt: Counter = Counter()
    for kreis in kreisansicht.kreise(konfiguration):
        hebelsatz, verteilsystem, bindung = kreis.get("hebelsatz"), kreis.get("verteilsystem"), kreis.get("bindung")
        ziel = ziele(bindung)
        if ziel is None or not (isinstance(hebelsatz, str) and isinstance(verteilsystem, str)):
            return False
        verlangt[(hebelsatz, verteilsystem, ziel, soll_entity(bindung))] += 1
    return bool(verlangt) and not verlangt - gesucht


def text_zu(rastung: Rastung | None) -> str:
    """Der Text zur Ablehnung durch diese Rastung; das alte Format nennt den Weg der Migration."""
    return TEXT_ALTES_FORMAT if rastung is not None and rastung.sperre == "altes_format" else TEXT


def schreiben(pfad: Path, rastung: Rastung, seit: str) -> None:
    """Schreibt nur, was `lesen` wieder annimmt (sonst ValueError): sonst sperrte sich das Geraet selbst."""
    if not rastung.kreise or any(not _nummer(k.kreis, 1) or not _kanonisch(list(k.ziele)) for k in rastung.kreise):
        raise ValueError("Rastung ohne Kreise oder mit unzulaessigen Eintraegen")
    dateien.schreiben_json(pfad, {"kreise": [{"kreis": k.kreis, "hebelsatz": k.hebelsatz,
                                              "verteilsystem": k.verteilsystem, "ziele": list(k.ziele),
                                              "soll_entity": k.soll_entity} for k in rastung.kreise],
                                  "seit": seit}, modus=dateien.PRIVAT)
