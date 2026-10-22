"""Laufzeit-Kontext der Bruecke und Ereignisarten des Regel-Workers.

Importiert zur Laufzeit nichts aus dem Paket, damit regulation/ticks/abo/triggers ihn ohne
Zyklus nutzen koennen."""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from smartheat_runtime.texts import HA_TEXTS, HostTexts

if TYPE_CHECKING:
    from smartheat_core.pipeline import LeverPipeline
    from smartheat_runtime.notifier import Notifier
    from smartheat_runtime.ports import MqttChannel, SignalSource, TriggerSource
    from smartheat_runtime.roles import ChannelManifest
    from smartheat_runtime.runtime_config import RuntimeConfig
    from smartheat_runtime.state import StateStore
    from smartheat_runtime.status import StatusReporter
    from smartheat_runtime.worker import RegulationWorker

EV_LOCAL_CHECK = "local_check"
EV_SETPOINTS = "setpoints"
EV_ACK_TIMEOUT = "ack_timeout"
EV_RETRY_DUE = "retry_due"
EV_WATCHDOG = "watchdog"
EV_TELEMETRY = "telemetry"
EV_GRACE_CHECK = "grace_check"
EV_HEALTH = "health"
EV_CONNECTION_CHECK = "connection_check"
EV_MQTT_CONNECTED = "mqtt_connected"
EV_SOURCE_CONNECTED = "source_connected"
EV_HEARTBEAT = "heartbeat"
EV_RECHECK = "recheck"
# Spec Stufe 3 S3 Abschnitt 4: nach dem Zuruecksetzen einer leeren Konfiguration neu starten (app._nach_zuruecksetzen).
EV_AUFRAEUMEN = "aufraeumen"


@dataclass
class Runtime:
    """Wird nur im Worker-Thread benutzt. Zustand liegt ausschliesslich in `store`, Schreiben
    auf die Anlage ausschliesslich ueber `override` (die Hebel-Pipeline; der Name stammt aus der Zeit vor Plan 2)."""
    manifest: ChannelManifest
    signals: SignalSource
    config: RuntimeConfig
    worker: RegulationWorker
    store: StateStore
    override: LeverPipeline
    notifier: Notifier
    # Neustart des Hosts (Spec Stufe 3 S6 4.2), Rueckruf aus app.start; ruft nur der Worker-Thread
    # (abo.check_grace_end).
    neustart: Callable[[], None]
    mqtt_client: MqttChannel | None = None
    trigger_client: TriggerSource | None = None
    status: StatusReporter | None = None
    # Ruhezustand im Betrieb (Fristende): alle Handler ausser dem Lebenszeichen laufen leer.
    idle: bool = False
    # Durchsetzung (enforce.py): der mit dem laufenden Tick als KPI gesendete, schon
    # zurueckgesetzte Eingriff (wird nach der Serverantwort geloescht).
    manual_override_sent: dict | None = None
    # Die seq, fuer die manual_override_sent gepinnt ist (gesetzt beim ersten erfolgreichen
    # Publish dieser seq). Ein Retry derselben seq sendet exakt diesen Eintrag erneut -- der
    # Server verarbeitet eine schon gesehene seq idempotent aus dem Cache und wuerde einen
    # geaenderten Eintrag sonst stillschweigend verwerfen (KPI-Verlust). Ein zwischenzeitlich neu
    # erkannter Eingriff reist deshalb erst mit der naechsten seq.
    manual_override_seq: str | None = None
    # Monotone Uhr des Workers (Tests: FakeClock). Fuer die Drosselung in abo.handle_connection_failing.
    clock: Callable[[], float] = time.monotonic
    # Transportneutrale Abo-Erkennung (Spec AWS-IoT 5.1): letzte /status-Abfrage nach langem,
    # wiederholtem Verbindungsfehler; ein erfolgreicher Connect setzt sie zurueck. Nicht persistiert.
    connection_failing_queried_at: float | None = None
    # Start (Plan-Praezisierung 11): Zone auf Manuell mit brauchbarer Parallelverschiebung. Bis es
    # einmal geklappt hat, versucht es jeder lokale Check erneut. Nicht persistiert.
    zone_prepared: bool = False
    # Comfort-Boost (A6-35 a): Stand der monotonen Uhr (clock) beim Start des Boosts in diesem Prozess, None ohne
    # Boost oder bei einem Boost aus einem frueheren Lauf. Die 4-h-Grenze zaehlt auch hier, falls die Wanduhr
    # zurueckspringt. Nicht persistiert.
    boost_mono_start: float | None = None
    # Verbindungswaechter (TP12b, AU-033): seit wann (clock) die MQTT-Verbindung ununterbrochen
    # fehlt, None bei Verbindung. Nicht persistiert.
    mqtt_down_since: float | None = None
    # Gescheiterte Wiederherstellung am Fristende (abo.log_restore_failure): in diesem Prozess schon geloggt. Nicht
    # persistiert: nach einem Neustart erscheint die Zeile einmal als WARNING (TP12b).
    restore_failure_logged: bool = False
    # Datenfehler (SignalUngueltig, ein Fuehler ohne gueltigen Wert) des lokalen Checks in diesem Prozess schon als WARNING
    # geloggt, bis zum naechsten erfolgreichen Check (app._run_local_check, L4-17). Nicht persistiert.
    local_check_data_fault_logged: bool = False
    # Kundentexte des Hosts (Spec SHG G2 2.1); Standard = HA-Texte.
    texts: HostTexts = HA_TEXTS
