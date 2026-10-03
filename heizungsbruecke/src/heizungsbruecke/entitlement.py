"""Abo-Status des Tenants (GET /tenants/<id>/status mit dem Installations-Token der Anlage (Spec AWS-IoT 4.3),
Spec TP8 3.1) und die 30-Tage-Frist des Abo-inaktiv-Modus. Die Frist wird in
/data/entitlement_state.json persistiert, damit sie Add-on-Neustarts ueberlebt."""
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

GRACE_PERIOD = timedelta(days=30)

STATUS_PATH = "/tenants/{tenant_id}/status"
TOKEN_OPTION = "installation_token"

ACTIVE = "active"
INACTIVE = "inactive"
UNKNOWN = "unknown"
REJECTED = "rejected"


def query_status(tenant_id: str, base_url: str, token: str) -> str:
    """Fail-open: nur eine eindeutige Antwort zaehlt. 200 mit active=true/false ergibt ACTIVE/
    INACTIVE, 401 ergibt REJECTED (Token ersetzt, entfernt oder falsch). Alles andere --
    Ausfall, jeder 404 (der Server kennt keinen mehr), Catch-all -- ist UNKNOWN: ein accounts-api-
    Ausfall darf einen zahlenden Kunden nicht in den Notbetrieb schicken."""
    url = f"{base_url}{STATUS_PATH.format(tenant_id=tenant_id)}"
    try:
        response = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
    except Exception as error:
        logger.warning("Abo-Status nicht abrufbar (wird als unbekannt behandelt): %s", error)
        return UNKNOWN
    if response.status_code == 401:
        logger.warning("Abo-Status-Abfrage: Server lehnt die Anmeldung ab (HTTP 401)")
        return REJECTED
    if response.status_code != 200:
        logger.warning("Abo-Status-Abfrage lieferte HTTP %s (wird als unbekannt behandelt)", response.status_code)
        return UNKNOWN
    try:
        body = response.json()
    except Exception as error:
        logger.warning("Abo-Status-Antwort nicht lesbar (wird als unbekannt behandelt): %s", error)
        return UNKNOWN
    active = body.get("active") if isinstance(body, dict) else None
    if active is True:
        return ACTIVE
    if active is False:
        return INACTIVE
    logger.warning("Abo-Status-Antwort ohne gueltiges 'active'-Feld: %r", body)
    return UNKNOWN


def query_from_options(options: dict) -> str:
    """Ohne Token (Konfiguration von vor AWS-2, wird beim Start als veraltet gemeldet) keine Anfrage."""
    token = options.get(TOKEN_OPTION)
    if not token:
        return UNKNOWN
    return query_status(options["tenant_id"], options["accounts_api_base_url"], token)


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
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps({"inactive_since": now.isoformat()}))
    os.replace(tmp_path, path)
    return now, True


def clear(path: Path) -> None:
    path.unlink(missing_ok=True)


def grace_end(inactive_since: datetime) -> datetime:
    return inactive_since + GRACE_PERIOD


def grace_expired(inactive_since: datetime, now: datetime) -> bool:
    return now >= grace_end(inactive_since)
