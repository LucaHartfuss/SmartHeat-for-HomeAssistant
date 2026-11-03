"""Status-Modell Schema 2; der Host sendet es ueber seinen StatusSink, beim HA-Host als
Ereignis `smartheat_status` an die SmartHeat-Integration. Gesendet wird der volle Status: nach jedem
Worker-Ereignis, wenn sich der Inhalt geaendert hat, bei jedem (Wieder-)Verbinden des WS-Trigger-Clients und
als Lebenszeichen alle HEARTBEAT_SECONDS. Die Integration haelt daraus ihre Entities; bleibt das Lebenszeichen
aus, meldet sie das selbst. Felder und Wertemengen sind Cross-Repo-Vertrag mit const.py der Integration
(Contract-Check)."""
import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace

from smartheat_core.zahlen import ist_endliche_zahl
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
#: Schwere der Zustaende, schlechtester zuerst: die Reihenfolge von `overall_status`; `zugang_abgelehnt` (nur der Host
#: kennt ihn) steht nach dem Konfigurationsfehler. Bei mehreren Kreisen folgen die Felder der Anlage dem schlechtesten.
SCHWERE = (
    STATUS_ABGEMELDET, STATUS_KONFIGURATIONSFEHLER, STATUS_ZUGANG_ABGELEHNT, STATUS_ABO_BEENDET, STATUS_ABO_INAKTIV,
    STATUS_NOTBETRIEB, STATUS_DATENFEHLER, STATUS_STARTET, STATUS_REGELT,
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
    "schema", "tenant_id", "addon_version", "status", "grund", "notbetrieb", "datenfehler",
    "boost", "letzte_serverantwort", "hebelsatz", "hebel", "gelernt", "abo", "abo_frist_ende", "hinweise",
)
# Felder der Anlage (folgen dem schlechtesten Kreis); alle anderen gehoeren zum Kreis (niedrigster Kreis).
_ANLAGEN_FELDER = ("status", "grund", "datenfehler", "letzte_serverantwort", "hinweise")


@dataclass(frozen=True)
class Flags:
    """Laufzeit-Merkmale des Gesamtzustands, die nicht in BridgeState stehen."""
    abgemeldet: bool = False
    konfigurationsfehler: bool = False
    # Abschluss-Start, dessen Zuruecksetzen noch scheitert (abo_finished ist dann noch False).
    abo_beendet: bool = False
    # Erster Start abgeschlossen: MQTT verbunden bzw. im Abo-inaktiv-Modus hochgefahren.
    gestartet: bool = False
    abo: str = ABO_UNBEKANNT  # Ergebnis der Abo-Abfrage beim Start (aktiv/unbekannt)
    grund: str | None = None


_STORAGE_FAULT_ROLES = (delivery.ROLE_DATENTRAEGER,)


def _veralteter_notbetrieb(state) -> bool:
    """Notbetrieb ohne offenen Tick stammt aus dem Abo-inaktiv-Modus; der Start beendet oder klaert ihn per Pruef-Tick
    (app._is_stale_notbetrieb). Bis zum ersten abgeschlossenen Start zeigt der Status deshalb `startet` statt eines
    kurzen `notbetrieb`."""
    return state.delivery.notbetrieb and state.delivery.pending is None


def overall_status(flags: Flags, state, storage_failed: bool = False) -> str:
    """Erster zutreffender Zustand: Endzustaende und Abo gelten immer, danach
    Notbetrieb und Datenfehler (auch ein nicht beschreibbarer Datentraeger), erst dann vor
    dem ersten abgeschlossenen Start `startet` (sonst stuende ein Add-on, dessen Verbindung nie aufgebaut wird, im
    Notbetrieb dauerhaft auf `startet`).
    Ausnahme: ein veralteter Notbetrieb (ohne offenen Tick) zaehlt vor dem Start nicht als Notbetrieb."""
    if flags.abgemeldet:
        return STATUS_ABGEMELDET
    if flags.konfigurationsfehler:
        return STATUS_KONFIGURATIONSFEHLER
    if flags.abo_beendet or state.abo_finished:
        return STATUS_ABO_BEENDET
    if state.abo_inactive_since is not None:
        return STATUS_ABO_INAKTIV
    if state.delivery.notbetrieb and (flags.gestartet or not _veralteter_notbetrieb(state)):
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


def _zahl(value) -> float | None:
    """Eine nicht endliche Zahl (NaN, Inf, Ganzzahl ausserhalb des float-Bereichs) geht nie in den Status, sondern als
    None des Feldes: ein Laufzeitwert legt das Ereignis und damit up/status nicht lahm."""
    return value if ist_endliche_zahl(value) else None


def _zahlen(values: dict) -> dict:
    return {key: _zahl(value) for key, value in values.items()}


def _manual(state) -> dict | None:
    override = state.manual_override
    if override is None:
        return None
    return {"hebel": _zahlen(override["levers"]), "erkannt": override["erkannt"]}


def _levers(state, lever_set) -> dict:
    """Stand je Hebel (Wiederherstellungspunkt = zuletzt bestaetigt)."""
    return {lever: _zahl(state.restore_point.get(lever)) for lever in lever_set.levers}


def _im_vertrag(feld: str, wert: str, erlaubt: tuple[str, ...], ersatz: str) -> str:
    """Wert eines Ereignisfelds aus der Wertemenge des Vertrags (Integration, Contract-Check V5); ein anderer wird
    geloggt (Feldname, nie der Wert) und ersetzt. Wirft nie: das Ereignis wird in Start und Worker ausserhalb eines try
    gebaut (StatusReporter.publish)."""
    if wert in erlaubt:
        return wert
    logger.error("Status-Ereignis: Wert des Feldes %s ausserhalb der Wertemenge des Vertrags, ersetzt", feld)
    return ersatz


def build_event(
    tenant_id: str, flags: Flags, state, lever_set, client_version: str,
    storage_failed: bool = False,
) -> dict:
    status = _im_vertrag("status", overall_status(flags, state, storage_failed), STATUS_VALUES, STATUS_DATENFEHLER)
    abo = _im_vertrag("abo", _abo(flags, state), ABO_VALUES, ABO_UNBEKANNT)
    return {
        "schema": EVENT_SCHEMA,
        "tenant_id": tenant_id,
        "addon_version": client_version,
        "status": status,
        "grund": flags.grund if status in _STATUS_WITH_REASON else None,
        "notbetrieb": state.delivery.notbetrieb,
        "datenfehler": (
            _fault(state.delivery.datenfehler) if state.delivery.datenfehler is not None
            else {"art": _FAULT_ART[delivery.SOURCE_LOCAL], "rollen": list(_STORAGE_FAULT_ROLES)} if storage_failed
            else None
        ),
        "boost": _im_vertrag("boost", _boost(state), BOOST_VALUES, BOOST_KEINER),
        "letzte_serverantwort": state.last_ack_at,
        "hebelsatz": lever_set.id,
        "hebel": _levers(state, lever_set),
        "gelernt": _zahlen(state.learned) or None,
        "abo": abo,
        "abo_frist_ende": (
            entitlement.grace_end(state.abo_inactive_since).date().isoformat() if abo == ABO_INAKTIV else None
        ),
        "hinweise": dict(zip(HINT_FIELDS, (
            _open_keys(state, "raumfuehler", room_sensors.STATE_FAILED),
            _open_keys(state, "batterie", battery.STATE_LOW),
            _manual(state),
        ), strict=True)),
    }


class StatusReporter:
    """Haelt die Laufzeit-Flags und sendet das Status-Modell. Der uebrige Zustand kommt aus dem
    StateStore; Module aendern nur Flags bzw. BridgeState, gesendet wird zentral."""

    def __init__(
        self, sink: StatusSink, tenant_id: str, store, lever_set, client_version: str,
    ) -> None:
        self._sink = sink
        self._tenant_id = tenant_id
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
            self._tenant_id, self.flags, self._store.state, self._lever_set,
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


def zusammenfassen(ereignisse: Mapping[int, dict]) -> dict:
    """Ein Ereignis der Anlage aus den Ereignissen ihrer Kreise (Schluessel: Kreisnummer). Status, Grund, Datenfehler,
    letzte Serverantwort und Hinweise folgen dem schlechtesten Kreis (bei Gleichstand dem mit Grund, dann dem niedrigeren),
    der Notbetrieb steht, sobald ein Kreis ihn meldet; Boost, Hebelsatz, Hebel, Gelerntes und Abo kommen vom niedrigsten
    Kreis. Abgemeldet ist die Anlage erst, wenn jeder Kreis es ist: solange ein anderer Kreis regelt oder wartet, zaehlt
    der schlechteste der uebrigen (sonst gaelte die Anlage als zurueckgesetzt, obwohl ein Kreis noch regelt)."""
    if not ereignisse:
        raise ValueError("Ohne Kreis kein Ereignis der Anlage")
    kreise = sorted(ereignisse)
    kandidaten = [kreis for kreis in kreise if ereignisse[kreis]["status"] != STATUS_ABGEMELDET] or kreise
    schlechtester = min(kandidaten, key=lambda kreis: (
        SCHWERE.index(ereignisse[kreis]["status"]), ereignisse[kreis]["grund"] is None, kreis))
    erg = dict(ereignisse[kreise[0]])
    for feld in _ANLAGEN_FELDER:
        erg[feld] = ereignisse[schlechtester][feld]
    erg["notbetrieb"] = any(ereignisse[kreis]["notbetrieb"] for kreis in kreise)
    return erg


class Statussammler:
    """Fasst die Status der Kreise in ein Ereignis der Anlage fuer die eine Senke des Hosts. Jeder Kreis meldet ueber
    `senke(kreis)`; nach jeder Meldung geht das Ereignis der Anlage hinaus. Ein Fehler der Senke erreicht den Kreis,
    dessen StatusReporter dann beim naechsten Anlass erneut sendet."""

    def __init__(self, sink: StatusSink) -> None:
        self._sink = sink
        self._lock = threading.Lock()
        self._ereignisse: dict[int, dict] = {}

    def senke(self, kreis: int) -> StatusSink:
        return _KreisSenke(self, kreis)

    def melden(self, kreis: int, event: dict) -> None:
        with self._lock:
            self._ereignisse[kreis] = event
            self._sink.publish(zusammenfassen(self._ereignisse))


class _KreisSenke:
    def __init__(self, sammler: Statussammler, kreis: int) -> None:
        self._sammler = sammler
        self._kreis = kreis

    def publish(self, event: dict) -> None:
        self._sammler.melden(self._kreis, event)
