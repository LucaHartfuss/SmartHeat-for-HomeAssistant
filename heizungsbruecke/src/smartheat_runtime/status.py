"""Status-Modell Schema 2 (Spec TP7 1.1, 3.1); der Host sendet es ueber seinen StatusSink, beim HA-Host als
Ereignis `smartheat_status` an die SmartHeat-Integration. Gesendet wird der volle Status: nach jedem
Worker-Ereignis, wenn sich der Inhalt geaendert hat, bei jedem (Wieder-)Verbinden des WS-Trigger-Clients und
als Lebenszeichen alle HEARTBEAT_SECONDS. Die Integration haelt daraus ihre Entities; bleibt das Lebenszeichen
aus, meldet sie das selbst. Felder und Wertemengen sind Cross-Repo-Vertrag mit const.py der Integration
(Contract-Check)."""
import logging
from dataclasses import dataclass, replace

from smartheat_runtime import battery, delivery, entitlement, room_sensors
from smartheat_runtime.ports import StatusSink

logger = logging.getLogger(__name__)

EVENT_SCHEMA = 2
HEARTBEAT_SECONDS = 300

STATUS_STARTET = "startet"
STATUS_REGELT = "regelt"
STATUS_KONFIGURATIONSFEHLER = "konfigurationsfehler"
STATUS_ZUGANG_ABGELEHNT = "zugang_abgelehnt"
STATUS_ABO_BEENDET = "abo_beendet"
STATUS_ABO_INAKTIV = "abo_inaktiv"
STATUS_NOTBETRIEB = "notbetrieb"
STATUS_DATENFEHLER = "datenfehler"
STATUS_ABGEMELDET = "abgemeldet"
STATUS_VALUES = (
    STATUS_STARTET, STATUS_REGELT, STATUS_KONFIGURATIONSFEHLER, STATUS_ZUGANG_ABGELEHNT, STATUS_ABO_BEENDET,
    STATUS_ABO_INAKTIV, STATUS_NOTBETRIEB, STATUS_DATENFEHLER, STATUS_ABGEMELDET,
)
_STATUS_WITH_REASON = frozenset({
    STATUS_ABGEMELDET, STATUS_KONFIGURATIONSFEHLER, STATUS_ZUGANG_ABGELEHNT, STATUS_ABO_BEENDET,
})

BOOST_KEINER = "keiner"
BOOST_KOMFORT = "komfort"
BOOST_NOTFALL = "notfall"
BOOST_VALUES = (BOOST_KEINER, BOOST_KOMFORT, BOOST_NOTFALL)

ABO_AKTIV = "aktiv"
ABO_INAKTIV = "inaktiv"
ABO_BEENDET = "beendet"
ABO_UNBEKANNT = "unbekannt"
ABO_VALUES = (ABO_AKTIV, ABO_INAKTIV, ABO_BEENDET, ABO_UNBEKANNT)

DATENFEHLER_ARTEN = ("lokal", "server", "anlage")
_FAULT_ART = dict(
    zip((delivery.SOURCE_LOCAL, delivery.SOURCE_SERVER, delivery.SOURCE_WRITE), DATENFEHLER_ARTEN, strict=True)
)

HINT_FIELDS = ("raumfuehler_ausgefallen", "batterie_niedrig", "manueller_eingriff")
EVENT_FIELDS = (
    "schema", "tenant_id", "setup_id", "addon_version", "status", "grund", "notbetrieb", "datenfehler",
    "boost", "letzte_serverantwort", "hebelsatz", "hebel", "gelernt", "abo", "abo_frist_ende", "hinweise",
)


@dataclass(frozen=True)
class Flags:
    """Laufzeit-Merkmale des Gesamtzustands, die nicht in BridgeState stehen."""
    abgemeldet: bool = False
    konfigurationsfehler: bool = False
    zugang_abgelehnt: bool = False
    # Abschluss-Start, dessen Zuruecksetzen noch scheitert (abo_finished ist dann noch False).
    abo_beendet: bool = False
    # Erster Start abgeschlossen: MQTT verbunden bzw. im Abo-inaktiv-Modus hochgefahren.
    gestartet: bool = False
    abo: str = ABO_UNBEKANNT  # Ergebnis der Abo-Abfrage beim Start (aktiv/unbekannt)
    grund: str | None = None


_STORAGE_FAULT_ROLES = (delivery.ROLE_DATENTRAEGER,)


def overall_status(flags: Flags, state, storage_failed: bool = False) -> str:
    """Erster zutreffender Zustand (Spec TP7 3.1): Endzustaende und Abo gelten immer, danach
    Notbetrieb und Datenfehler (auch ein nicht beschreibbarer Datentraeger, TP12b), erst dann vor
    dem ersten abgeschlossenen Start `startet` (sonst stuende ein Add-on, dessen Tunnel nie aufgebaut wird, im Notbetrieb dauerhaft auf `startet`)."""
    if flags.abgemeldet:
        return STATUS_ABGEMELDET
    if flags.konfigurationsfehler:
        return STATUS_KONFIGURATIONSFEHLER
    if flags.zugang_abgelehnt and state.abo_inactive_since is None:
        return STATUS_ZUGANG_ABGELEHNT
    if flags.abo_beendet or state.abo_finished:
        return STATUS_ABO_BEENDET
    if state.abo_inactive_since is not None:
        return STATUS_ABO_INAKTIV
    if state.delivery.notbetrieb:
        return STATUS_NOTBETRIEB
    if storage_failed or state.delivery.datenfehler is not None:
        return STATUS_DATENFEHLER
    if not flags.gestartet:
        return STATUS_STARTET
    return STATUS_REGELT


def _fault(fault) -> dict | None:
    if fault is None:
        return None
    if fault.source == delivery.SOURCE_LOCAL:
        roles = list(fault.detail)
    elif fault.source == delivery.SOURCE_WRITE and fault.detail:
        roles = [fault.detail[0].split(" ", 1)[0]]  # "curve (number.x): Ursache" (pipeline.DeviceWriteError)
    else:
        roles = []
    return {"art": _FAULT_ART[fault.source], "rollen": roles}


def _boost(state) -> str:
    if state.emergency_boost_active:
        return BOOST_NOTFALL
    return BOOST_KOMFORT if state.boost_active else BOOST_KEINER


def _abo(flags: Flags, state) -> str:
    if flags.abo_beendet or state.abo_finished:
        return ABO_BEENDET
    if state.abo_inactive_since is not None:
        return ABO_INAKTIV
    return flags.abo


def _open_keys(state, prefix: str, value: str) -> list[str]:
    """Entity-IDs der Meldeschluessel `<prefix>:<entity>` im Zustand `value`, sortiert."""
    return sorted(
        key.partition(":")[2] for key, current in state.notify_states.items()
        if key.startswith(f"{prefix}:") and current == value
    )


def _manual(state) -> dict | None:
    override = state.manual_override
    if override is None:
        return None
    return {"hebel": dict(override["levers"]), "erkannt": override["erkannt"]}


def _levers(state, lever_set) -> dict:
    """Stand je Hebel (Wiederherstellungspunkt = zuletzt bestaetigt) plus die vom Client abgeleiteten (Mindestvorlauf)."""
    levers = {lever: state.restore_point.get(lever) for lever in lever_set.levers}
    if "min_flow" in lever_set.client_derived:
        levers["min_flow"] = state.min_flow_current
    return levers


def build_event(
    tenant_id: str, setup_id: str | None, flags: Flags, state, lever_set, client_version: str,
    storage_failed: bool = False,
) -> dict:
    status = overall_status(flags, state, storage_failed)
    abo = _abo(flags, state)
    return {
        "schema": EVENT_SCHEMA,
        "tenant_id": tenant_id,
        "setup_id": setup_id,
        "addon_version": client_version,
        "status": status,
        "grund": flags.grund if status in _STATUS_WITH_REASON else None,
        "notbetrieb": state.delivery.notbetrieb,
        "datenfehler": (
            _fault(state.delivery.datenfehler) if state.delivery.datenfehler is not None
            else {"art": _FAULT_ART[delivery.SOURCE_LOCAL], "rollen": list(_STORAGE_FAULT_ROLES)} if storage_failed
            else None
        ),
        "boost": _boost(state),
        "letzte_serverantwort": state.last_ack_at,
        "hebelsatz": lever_set.id,
        "hebel": _levers(state, lever_set),
        "gelernt": dict(state.learned) or None,
        "abo": abo,
        "abo_frist_ende": (
            entitlement.grace_end(state.abo_inactive_since).date().isoformat() if abo == ABO_INAKTIV else None
        ),
        "hinweise": {
            "raumfuehler_ausgefallen": _open_keys(state, "raumfuehler", room_sensors.STATE_FAILED),
            "batterie_niedrig": _open_keys(state, "batterie", battery.STATE_LOW),
            "manueller_eingriff": _manual(state),
        },
    }


class StatusReporter:
    """Haelt die Laufzeit-Flags und sendet das Status-Modell. Der uebrige Zustand kommt aus dem
    StateStore; Module aendern nur Flags bzw. BridgeState, gesendet wird zentral."""

    def __init__(
        self, sink: StatusSink, tenant_id: str, setup_id: str | None, store, lever_set, client_version: str,
    ) -> None:
        self._sink = sink
        self._tenant_id = tenant_id
        self._setup_id = setup_id if isinstance(setup_id, str) and setup_id else None
        self._store = store
        self._lever_set = lever_set
        self._client_version = client_version
        self.flags = Flags()
        self._published: dict | None = None

    @property
    def status(self) -> str:
        return overall_status(self.flags, self._store.state, self._store.storage_failed)

    def event(self) -> dict:
        return build_event(
            self._tenant_id, self._setup_id, self.flags, self._store.state, self._lever_set,
            self._client_version, self._store.storage_failed,
        )

    def update(self, **changes) -> None:
        self.flags = replace(self.flags, **changes)
        self.publish_if_changed()

    def publish_if_changed(self) -> None:
        if self.event() != self._published:
            self.publish()

    def publish(self) -> None:
        """Unbedingt (Verbinden, Lebenszeichen). Ein Fehler wird nur geloggt; der naechste Anlass
        sendet erneut, weil der zuletzt gesendete Stand dann nicht aktualisiert ist."""
        event = self.event()
        try:
            self._sink.publish(event)
        except Exception:
            logger.warning("Status (%s) konnte nicht gesendet werden", event["status"])
            return
        self._published = event
