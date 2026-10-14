"""Abo-Status des Tenants (aus dem Konfigurationsdokument, Spec 5b 5.2) und die 30-Tage-Frist des Abo-inaktiv-Modus.
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
REJECTED = "rejected"


def query(config: RuntimeConfig) -> str:
    """Der Abo-Status aus dem Konfigurationsdokument (abo_source, Spec 5b 5.2), nie ein Abruf beim Server."""
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


def clear(path: Path) -> None:
    path.unlink(missing_ok=True)


def grace_end(inactive_since: datetime) -> datetime:
    return inactive_since + GRACE_PERIOD


def grace_expired(inactive_since: datetime, now: datetime) -> bool:
    return now >= grace_end(inactive_since)
