"""Laufzeit-Kontext der Bruecke und Ereignisarten des Regel-Workers.

Importiert zur Laufzeit nichts aus dem Paket, damit regulation/ticks/abo/triggers ihn ohne
Zyklus nutzen koennen."""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from heizungsbruecke.ha_api import HomeAssistantApi
    from heizungsbruecke.ha_trigger_client import HaTriggerClient
    from heizungsbruecke.manifest import ChannelManifest
    from heizungsbruecke.mqtt_client import BridgeMqttClient
    from heizungsbruecke.notifier import Notifier
    from heizungsbruecke.override import Override
    from heizungsbruecke.state import StateStore
    from heizungsbruecke.status import StatusReporter
    from heizungsbruecke.worker import RegulationWorker

EV_LOCAL_CHECK = "local_check"
EV_SETPOINTS = "setpoints"
EV_AUTH_REJECTED = "auth_rejected"
EV_ACK_TIMEOUT = "ack_timeout"
EV_RETRY_DUE = "retry_due"
EV_WATCHDOG = "watchdog"
EV_TELEMETRY = "telemetry"
EV_GRACE_CHECK = "grace_check"
EV_HEALTH = "health"
EV_MQTT_CONNECTED = "mqtt_connected"
EV_HA_CONNECTED = "ha_connected"
EV_HEARTBEAT = "heartbeat"
EV_RECHECK = "recheck"


@dataclass
class Runtime:
    """Wird nur im Worker-Thread benutzt. Zustand liegt ausschliesslich in `store`, Schreiben
    auf die Anlage ausschliesslich ueber `override`."""
    manifest: ChannelManifest
    ha_api: HomeAssistantApi
    options: dict
    worker: RegulationWorker
    store: StateStore
    override: Override
    notifier: Notifier
    mqtt_client: BridgeMqttClient | None = None
    trigger_client: HaTriggerClient | None = None
    status: StatusReporter | None = None
    # Ruhezustand im Betrieb (Fristende): alle Handler ausser dem Lebenszeichen laufen leer.
    idle: bool = False
    # Durchsetzung (manual_override.py): der mit dem laufenden Tick als KPI gesendete, schon
    # zurueckgesetzte Eingriff (wird nach der Serverantwort geloescht).
    manual_override_sent: dict | None = None
    # Die seq, fuer die manual_override_sent gepinnt ist (gesetzt beim ersten erfolgreichen
    # Publish dieser seq). Ein Retry derselben seq sendet exakt diesen Eintrag erneut -- der
    # Server verarbeitet eine schon gesehene seq idempotent aus dem Cache und wuerde einen
    # geaenderten Eintrag sonst stillschweigend verwerfen (KPI-Verlust). Ein zwischenzeitlich neu
    # erkannter Eingriff reist deshalb erst mit der naechsten seq.
    manual_override_seq: str | None = None
    # Monotone Uhr des Workers (Tests: FakeClock). Fuer die Drosselung in abo.handle_auth_rejected.
    clock: Callable[[], float] = time.monotonic
    # T2-12: Zeitpunkt und Ergebnis der letzten Abo-Abfrage nach einer abgelehnten MQTT-Anmeldung;
    # ein erfolgreicher Connect setzt beides zurueck. Nicht persistiert.
    auth_rejected_queried_at: float | None = None
    auth_rejected_last_status: str | None = None
    # Start (Plan-Praezisierung 11): Zone auf Manuell mit brauchbarer Parallelverschiebung. Bis es
    # einmal geklappt hat, versucht es jeder lokale Check erneut. Nicht persistiert.
    zone_prepared: bool = False
    # Kontingent dieser Versuche (Tag, Anzahl, letzter Versuch auf `clock`, Tageslimit geloggt).
    zone_prepare_log: dict = field(default_factory=dict)
