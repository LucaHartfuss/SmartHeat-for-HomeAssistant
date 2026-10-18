"""Das HA-Add-on als Host des Geraetekerns (Spec 5c 1.3): liefert Faehigkeiten (keine Treiber, kein Zigbee, kein
updater), Uhr aus dem Supervisor, den HA-Teil der Konfigurationspruefung, die Befehle (inventory, sign_off,
diagnostics; HA-fremde Befehle nicht_unterstuetzt) und nimmt Konfiguration und Bedienung entgegen. Alle Rueckrufe laufen
im Geraete-Thread und werfen nicht (ausser NichtPruefbar aus treiber_pruefen, siehe dokument.zuordnung_pruefen, und
einem OSError der Markierung aus konfiguration_vorbereiten, siehe geraet_host_basis). Eine laufzeitrelevante neue
Konfiguration fordert einen Neustart an (geraet_host_basis.EV_RELOAD im Regel-Worker); der Wechsel von eingerichtet auf
leer markiert das Aufraeumen vor dem Speichern (A4-07).
Eine neue Bedienung geht als Soll-Eingang (portal) an den Worker, der Soll-Eingang schreibt sie in den Soll-Speicher und
gleicht die Soll-Entity an; kommt sie vor dem Binden, stellt worker_binden die letzte nach. Status
(runtime_status_event, Task 7) und Raum (raum_merken, HaHost) legt der Worker-Thread hier ab, der Geraete-Thread liest
sie fuer up/status. Meldungen der Laufzeit gehen wie beim Gateway (MeldungSink) als up/notifications an den Server und
zusaetzlich an HA (notify_sink_fuer). anzeigen meldet Geraete-ID, Uebernahmestand und Code an die Supervisor-Discovery
(discovery.Discovery)."""
import logging
import time
from collections.abc import Callable
from pathlib import Path

from heizungsbruecke import config, dokument, inventur
from heizungsbruecke.discovery import Discovery
from heizungsbruecke.geraet_status import GeraetStatus
from heizungsbruecke.ha_sinks import GeraetUndHaSink, HaNotifySink
from heizungsbruecke.version import ADDON_VERSION
from smartheat_core import dateien
from smartheat_device import fehler, nachrichten, wire
from smartheat_device.commands import CommandRegister, Done, Failed, Outcome, Waiting
from smartheat_device.geraet_host_basis import GeraetHostBasis
from smartheat_device.host import Anzeige
from smartheat_runtime import dokument_config
from smartheat_runtime.status import STATUS_ABGEMELDET

logger = logging.getLogger(__name__)

UHR_CACHE_SECONDS = 60
INVENTUR_SECONDS = 120
SIGN_OFF_WAIT_SECONDS = 24 * 3600
_FREMD = ("zigbee_permit_join", "zigbee_devices", "driver_login")


def _nicht_unterstuetzt(payload: dict) -> Failed:
    return Failed("nicht_unterstuetzt")


class HaDeviceHost(GeraetHostBasis):
    host = "ha"
    version = ADDON_VERSION
    wunsch_herkunft = "soll_entity"

    def __init__(self, ha_api, supervisor, *, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time) -> None:
        # wall: Teil der Schnittstelle (wie GatewayDeviceHost), hier ungenutzt: der HA-Host braucht keine Wanduhr (den
        # Zeitstempel des Raums setzt HaHost aus smartheat_core.wallclock), deshalb nicht gespeichert.
        super().__init__()
        self._ha, self._supervisor, self._clock = ha_api, supervisor, clock
        self.status_sink: GeraetStatus | None = None  # StatusSink der Laufzeit und Anzeige (__main__.verdrahten)
        self.discovery: Discovery | None = None  # Supervisor-Meldung (__main__.verdrahten)
        self.claim_code: Callable[[], str | None] = lambda: None  # Uebernahme-Code des Geraets (__main__.verdrahten)
        self.runtime_status_event: dict | None = None  # letztes Status-Ereignis der Laufzeit (Task 7: laufzeit_merken)
        self._raum: dict | None = None
        self._uhr: tuple[float, bool | None] | None = None

    def aufraeumen_pfad(self) -> Path:
        return config.AUFRAEUMEN_PATH

    # --- Sicht der Laufzeit (HaHost) ---

    def notify_sink_fuer(self, services: list[str]) -> GeraetUndHaSink:
        """Meldungen der Laufzeit an den Geraetekern (Portal, Betreiber-Alarm geraetemeldung; Spec 5c 3: HA-Geraete wie
        Gateways) und an HA (Notify-Dienste, persistent_notification)."""
        return GeraetUndHaSink(self.notify_sink, HaNotifySink(self._ha, services))

    def raum_merken(self, raum: dict | None) -> None:
        """Worker-Thread (raum.RaumPublisher): {ist, soll, soll_quelle, ts} fuer up/status."""
        self._raum = raum

    # --- DeviceHost ---

    def faehigkeiten(self) -> dict:
        return nachrichten.pruefen("faehigkeiten", {"drivers": [], "zigbee": False}, wire.CAPABILITY_FIELDS,
                                   wire.CAPABILITY_OPTIONAL)

    def uhr_synchron(self) -> bool | None:
        """dt_synchronized aus GET /host/info; kein bool oder Fehler = unbekannt (gilt als nicht synchron). Der Aufruf
        blockiert den Geraete-Thread hoechstens supervisor.TIMEOUT_SECONDS (5 s) und hoechstens einmal je
        UHR_CACHE_SECONDS: vertretbar (Bootstrap und Bedienwunsch fragen selten, ein Hintergrund lohnt nicht)."""
        now = self._clock()
        if self._uhr is not None and now - self._uhr[0] < UHR_CACHE_SECONDS:
            return self._uhr[1]
        try:
            value = self._supervisor.host_info().get("dt_synchronized")
            result = value if isinstance(value, bool) else None
        except Exception as error:
            logger.warning("Uhrzeit des Hosts unbekannt (%s)", type(error).__name__)
            result = None
        self._uhr = (now, result)
        return result

    def treiber_pruefen(self, konfiguration: dict) -> dict | None:
        """HA-Teil der Pruefung (config_check gibt fuer Host ha die ganze Konfiguration): details oder None. Gegen die
        uebernommene Konfiguration (Dokumentspeicher, ohne Sperre lesbar): bleiben Bindung, Raumfuehler und Hebelsatz
        gleich, entfaellt der HA-Teil. Wirft NichtPruefbar, wenn HA gerade nicht antwortet (der Geraetekern prueft
        spaeter erneut)."""
        return dokument.zuordnung_pruefen(konfiguration, self._ha, self.konfiguration())

    def befehle_eintragen(self, register: CommandRegister) -> None:
        register.register("inventory", self._inventory)
        # sign_off ist idempotent (die Markierung setzt den Neustart nur einmal): nach dem Neustart laeuft es erneut,
        # damit das Ergebnis kommt, sobald der Status "abgemeldet" zeigt (D1: wiederholbar).
        register.register("sign_off", self._sign_off, wiederholbar=True)
        register.register("diagnostics", self._diagnostics)
        for kind in _FREMD:
            register.register(kind, _nicht_unterstuetzt)

    def software_soll(self, software: dict | None) -> None:
        """HA-Add-ons aktualisiert der Supervisor; die Faehigkeit updater fehlt (Spec 5c 1.3)."""

    def runtime_status(self) -> dict | None:
        return self.runtime_status_event

    def raum(self) -> dict | None:
        return self._raum

    def anzeigen(self, anzeige: Anzeige) -> None:
        self.letzte_anzeige = anzeige
        sink = self.status_sink
        if sink is not None and hasattr(sink, "anzeige"):
            try:
                sink.anzeige(anzeige)
            except Exception as error:
                logger.warning("Status-Ereignis nicht gesendet (%s)", type(error).__name__)
        if self.discovery is not None and self.device is not None:
            # sendet nur bei Aenderung (und drosselt Wiederholungen nach Fehlern): ein Aufruf je Takt ist billig.
            # Gesperrt steht nur im Speicher (device_state bleibt nicht_uebernommen), die Discovery meldet es trotzdem.
            zustand = "gesperrt" if anzeige.zustand == "gesperrt" else anzeige.device_state
            try:
                self.discovery.aktualisieren(self.device.identity.device_id, zustand,
                                             self.claim_code() if anzeige.code_zeigen else None)
            except Exception as error:
                logger.warning("Discovery nicht aktualisiert (%s)", type(error).__name__)

    # --- Befehle ---

    def _inventory(self, payload: dict) -> Outcome:
        """Momentaufnahme nach dem Filter des Servers (stunden 0, Spec 5c 1.3) im Hintergrund: die HA-API (Registry per
        WebSocket, Zustaende, Dienste) haelt den Geraete-Thread nie auf. Antwortet HA bis INVENTUR_SECONDS nicht, endet
        der Befehl mit ha_nicht_erreichbar. Nur die Fehlerart ins Log, nie Inhalte (Regel 6). Verletzt die Inventur
        den Vertrag (Vertragsfehler: nur Feldnamen), endet der Befehl mit intern."""
        filter_ = payload.get("filter")
        if not isinstance(filter_, dict):
            return Failed("ungueltige_nutzlast", fehler.details("filter", "fehlt" if filter_ is None else "ungueltig"))
        if (details := inventur.filter_pruefen(filter_)) is not None:
            return Failed("ungueltige_nutzlast", details)
        future = self.hintergrund.submit(lambda: inventur.inventur(self._ha, filter_))
        frist = self._clock() + INVENTUR_SECONDS

        def check() -> Done | Failed | None:
            if not future.done():
                if self._clock() >= frist:
                    future.cancel()
                    return Failed("ha_nicht_erreichbar")
                return None
            try:
                return Done({}, inventur=future.result())
            except nachrichten.Vertragsfehler:
                raise  # Programmierfehler: das Register meldet intern, Fehlerart und Ort stehen im Log (kein Wert)
            except Exception as error:
                logger.warning("Inventur gescheitert (%s)", type(error).__name__)
                return Failed("ha_nicht_erreichbar")

        return Waiting(check, frist)

    def _sign_off(self, payload: dict) -> Outcome:
        """Abmelden (Spec TP7 3.3, 5b 4.5) wie beim Gateway: Markierung fuer die Anlage, dann startet die Laufzeit als
        "abgemeldet" neu und setzt die Anlage zurueck. Fertig, sobald der Status "abgemeldet" fuer diese Anlage zeigt;
        werte ist der Wiederherstellungspunkt danach."""
        konfiguration = self.konfiguration()
        if not dokument_config.eingerichtet(konfiguration):
            return Failed("nicht_eingerichtet")
        assert konfiguration is not None
        anlage = konfiguration["anlage"]
        if dokument_config.abmelden(config.ABGEMELDET_PATH, anlage):
            self.neustart_anfordern()

        def check() -> Done | None:
            status = self.runtime_status_event
            if not status or status.get("status") != STATUS_ABGEMELDET or status.get("tenant_id") != anlage:
                return None
            return Done({"zurueckgesetzt": status.get("grund") is None, "werte": _restore_values()})

        return Waiting(check, self._clock() + SIGN_OFF_WAIT_SECONDS)

    def _diagnostics(self, payload: dict) -> Done:
        anzeige = self.letzte_anzeige
        status = self.runtime_status_event or {}
        return Done({"addon_version": ADDON_VERSION, "zustand": anzeige.zustand if anzeige else "startet",
                     "eingerichtet": anzeige.eingerichtet if anzeige else False,
                     "server_ok": anzeige.server_ok if anzeige else False, "laufzeit": status.get("status"),
                     "uhr_synchron": self._uhr[1] if self._uhr else None})


def _restore_values() -> dict:
    backup = dateien.lesen_json(config.BACKUP_PATH)
    point = backup.get("restore_point") if isinstance(backup, dict) else None
    return dict(point) if isinstance(point, dict) else {}
