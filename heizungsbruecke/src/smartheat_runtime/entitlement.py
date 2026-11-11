"""Abo-Status des Tenants (aus dem Konfigurationsdokument) und die 30-Tage-Frist des Abo-inaktiv-Modus.
Die Frist wird in /data/entitlement_state.json persistiert, damit sie Add-on-Neustarts ueberlebt."""
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from smartheat_core import dateien
from smartheat_runtime.runtime_config import RuntimeConfig

logger = logging.getLogger(__name__)

GRACE_PERIOD = timedelta(days=30)

ACTIVE = "active"
INACTIVE = "inactive"
UNKNOWN = "unknown"


def query(config: RuntimeConfig) -> str:
    """Der Abo-Status aus dem Konfigurationsdokument (abo_source), nie ein Abruf beim Server."""
    return config.abo_source()


def load_inactive_since(path: Path) -> datetime | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
        since = datetime.fromisoformat(raw["inactive_since"])
        if since.tzinfo is None:
            raise ValueError("Zeitpunkt ohne Zeitzone")
        return since
    except Exception as error:
        logger.warning("%s nicht lesbar, Frist beginnt neu: %s", path, error)
        return None


def mark_inactive(path: Path, now: datetime) -> tuple[datetime, bool]:
    """Setzt inactive_since nur, wenn noch keiner (lesbar) gespeichert ist. Gibt
    (inactive_since, neu_gesetzt) zurueck. Eine korrupte Datei zaehlt konservativ als
    "jetzt erstmals inaktiv": die Frist beginnt neu, der Notbetrieb bleibt."""
    existing = load_inactive_since(path)
    if existing is not None:
        return existing, False
    dateien.schreiben_json(path, {"inactive_since": now.isoformat()}, modus=dateien.PRIVAT)
    return now, True


def _lesen(path: Path) -> dict:
    """Der Inhalt der Datei als Objekt; fehlende oder unlesbare Datei: leer."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def ist_beendet(path: Path) -> bool:
    """Ob die Regelung dieses Kreises beendet ist (das Ende der Frist wurde schon einmal abgewickelt). Ein fehlendes
    Feld, eine fehlende oder eine unlesbare Datei heisst "nicht beendet": nur ein ausdruecklich gespeichertes True gilt."""
    return _lesen(path).get("beendet") is True


def mark_beendet(path: Path) -> None:
    """Merkt das Ende der Regelung dauerhaft, neben dem Beginn der Frist. Das Ende gilt ueber Neustarts hinweg, auch bei
    unsicherer Uhr, bis das Abo wieder aktiv ist (`clear`). Wirft bei einem Schreibfehler."""
    inhalt = _lesen(path)
    inhalt["beendet"] = True
    dateien.schreiben_json(path, inhalt, modus=dateien.PRIVAT)


def clear(path: Path) -> None:
    path.unlink(missing_ok=True)


def grace_end(inactive_since: datetime) -> datetime:
    return inactive_since + GRACE_PERIOD


def grace_expired(inactive_since: datetime, now: datetime) -> bool:
    return now >= grace_end(inactive_since)


def kritische_mindestversion_abgelaufen(wirksam_ab: datetime | None, jetzt: datetime) -> bool:
    """Nur der Vergleich, noch nicht an die Hosts angeschlossen. Der Beginn der 30 Tage darf nicht ein vom Server genanntes
    "wirksam ab" sein, sondern wird lokal ab dem ersten Empfang der kritischen Mindestversion gezaehlt, gemessen mit
    vertrauenswuerdiger Uhr und dauerhaft gespeichert (wie `inactive_since`). Die lokale Frostnaehe-Sicherung bleibt
    dabei aktiv. Ein Ende nach dieser Frist liefe ueber `abo.finish_grace` (Ende dauerhaft mit `mark_beendet`, das erste
    Ende braucht eine vertrauenswuerdige Uhr). None: keine kritische Mindestversion."""
    return wirksam_ab is not None and jetzt >= wirksam_ab + GRACE_PERIOD
