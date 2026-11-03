"""Die Laufzeit einer Anlage: eine Laufzeit-Instanz je Kreis, ein Link, eine Meldesenke.

Jeder Kreis hat sein eigenes Ergebnis von `app.start` (Runtime oder Ruhezustand) mit eigenem Worker-Thread,
Zustandsspeicher, Hebel-Pipeline und Statusmelder; gemeinsam sind der Link des Geraets (mqtt_factory), die Meldesenke
(Schluessel mit Kreis-Praefix) und das Statusereignis der Anlage (`status.Statussammler`). Eine Ausnahme im Worker
eines Kreises (auch Neustart) wird vom Aufseher im Thread von `run` weitergeworfen, der Prozess folgt dem
Neustartweg des Hosts.

Wiederaufnahme: ein Kreis im Ruhezustand des Abos (Frist abgeschlossen oder laufend) nimmt die Arbeit wieder auf, sobald
sein Abo wieder `aktiv` ist: nur dieser Kreis wird neu gestartet (kein Neustart des Prozesses, die Link-Verbindung und
die anderen Kreise bleiben). Frist und Ursprungswerte regelt unveraendert `app.start`.

Neustart je Kreis: bei mehreren Kreisen heisst der Neustart, den ein Kreis in seinem Worker ruft (Neupruefung nach
einem Konfigurationsfehler, Abo nach der Frist wieder aktiv), nur "diesen Kreis neu aufnehmen", in einem eigenen Thread
(der Worker darf sich nicht selbst abwarten). Der Prozess startet dann nicht neu, die anderen Kreise regeln weiter. Eine
Anlage mit einem Kreis und das Ende des Zuruecksetzens (alle Kreise zurueckgesetzt) nehmen weiter den Neustart des
Prozesses."""
import contextlib
import logging
import queue
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import NoReturn, Protocol

from smartheat_core import kreisansicht
from smartheat_runtime import app, dokument_config
from smartheat_runtime.app import STATUS_ABO_BEENDET, Host, IdleBridge
from smartheat_runtime.ports import StatusSink
from smartheat_runtime.runtime import Runtime
from smartheat_runtime.status import Statussammler
from smartheat_runtime.worker import Event, Handler, RegulationWorker, WorkerBeendet

logger = logging.getLogger(__name__)

#: So lange wartet die Wiederaufnahme auf das Ende des Workers eines Kreises (Sekunden)
ABWICKLUNG_SECONDS = 60.0


class AnlagenHost(Protocol):
    """Was ein Host der Anlage liefert: die Kreisnummern und je Kreis die Sicht, die `app.start` kennt. Die
    anlagenweite Statussenke hat nur der Aufseher."""

    status_sink: StatusSink

    def kreise(self) -> Sequence[int]: ...

    def kreis_host(self, kreis: int) -> Host: ...


class _KreisMeldesenke:
    """Meldesenke eines Kreises: der Schluessel bekommt den Kreis als Praefix, damit Meldungen zweier Kreise einander
    nicht ueberschreiben."""

    def __init__(self, senke, kreis: int) -> None:
        self._senke, self._praefix = senke, f"kreis-{kreis}:"

    def push(self, key: str, message: str) -> None:
        self._senke.push(self._praefix + key, message)

    def show(self, key: str, message: str) -> None:
        self._senke.show(self._praefix + key, message)

    def withdraw(self, key: str) -> None:
        self._senke.withdraw(self._praefix + key)


def ruhend(ergebnis: Runtime | IdleBridge) -> bool:
    """Der Kreis wartet auf sein Abo: Frist abgeschlossen (Ruhezustand oder Runtime im Ruhezustand) oder Frist laeuft."""
    if isinstance(ergebnis, IdleBridge):
        return ergebnis.reason == STATUS_ABO_BEENDET
    return ergebnis.idle or ergebnis.store.state.abo_inactive_since is not None


def _anhalten(ergebnis: Runtime | IdleBridge) -> None:
    """Trigger und Kanal eines Runtime beenden (der Link des Geraets bleibt verbunden)."""
    if not isinstance(ergebnis, Runtime):
        return
    for client in (ergebnis.mqtt_client, ergebnis.trigger_client):
        if client is None:
            continue
        try:
            client.stop()
        except Exception:
            logger.exception("Verbindung eines Kreises konnte nicht sauber beendet werden")


class _Aufsicht:
    """Die Sicht des Prozesses auf alle Worker: `run` fuer prozess.laufen, `register`/`post` gehen an jeden Kreis
    (auch an einen wiederaufgenommenen)."""

    def __init__(self, laufzeit: "Laufzeit") -> None:
        self._laufzeit = laufzeit
        self._registriert: list[tuple[str, Handler]] = []

    def register(self, kind: str, handler: Handler) -> None:
        with self._laufzeit._sperre:  # gegen die Wiederaufnahme: der Handler erreicht jeden Kreis genau einmal
            self._registriert.append((kind, handler))
            for ergebnis in self._laufzeit.kreise().values():
                ergebnis.worker.register(kind, handler)

    def anwenden(self, worker: RegulationWorker) -> None:
        for kind, handler in self._registriert:
            worker.register(kind, handler)

    def post(self, event: Event) -> None:
        for ergebnis in self._laufzeit.kreise().values():
            ergebnis.worker.post(event)

    def run(self) -> NoReturn:
        self._laufzeit.run()

    def run_pending(self) -> None:
        """Test-Einstieg mit Fake-Uhr: verarbeitet alles Faellige in jedem Kreis, ohne zu blockieren. Ein Worker, den
        eine Wiederaufnahme gerade ersetzt hat, endet still (der neue Worker steht schon im Kreis)."""
        for ergebnis in self._laufzeit.kreise().values():
            with contextlib.suppress(WorkerBeendet):
                ergebnis.worker.run_pending()


class Laufzeit:
    def __init__(self, host: AnlagenHost, clock: Callable[[], float], *, mqtt_factory, neustart: Callable[[], None],
                 gestartet: Callable[[int, Runtime | IdleBridge], None] | None = None):
        self._host, self._clock, self._mqtt_factory, self._neustart = host, clock, mqtt_factory, neustart
        self._gestartet = gestartet  # Rueckruf nach jedem Start eines Kreises, auch einer Wiederaufnahme
        self._ergebnisse: dict[int, Runtime | IdleBridge] = {}
        self._threads: dict[int, threading.Thread] = {}
        self._in_aufnahme: set[int] = set()
        self._ausfall: queue.Queue[BaseException] = queue.Queue()
        self._sperre = threading.RLock()
        self._laeuft = False
        self._sammler = Statussammler(host.status_sink)
        self._mehrere = False
        self.worker = _Aufsicht(self)

    def kreise(self) -> Mapping[int, Runtime | IdleBridge]:
        with self._sperre:
            return dict(self._ergebnisse)

    def starten(self) -> "Laufzeit":
        nummern = list(self._host.kreise())
        self._mehrere = len(nummern) > 1
        for kreis in nummern:
            self._ergebnisse[kreis] = self._starte_kreis(kreis)
            self._melden_gestartet(kreis, self._ergebnisse[kreis])
        return self

    def _melden_gestartet(self, kreis: int, ergebnis: Runtime | IdleBridge) -> None:
        if self._gestartet is not None:
            self._gestartet(kreis, ergebnis)

    def _starte_kreis(self, kreis: int) -> Runtime | IdleBridge:
        kreis_host = self._host.kreis_host(kreis)
        melden = _KreisMeldesenke(kreis_host.notify_sink, kreis) if self._mehrere else None
        return app.start(kreis_host, self._clock, mqtt_factory=self._mqtt_factory, neustart=self._kreis_neustart(kreis),
                         status_sink=self._sammler.senke(kreis), notify_sink=melden, prozess_neustart=self._neustart)

    def _kreis_neustart(self, kreis: int) -> Callable[[], None]:
        """Der Neustart, den die Laufzeit des Kreises in ihrem Worker ruft: bei einem Kreis der des Prozesses, sonst die
        erzwungene Wiederaufnahme dieses Kreises in einem eigenen Thread."""
        if not self._mehrere:
            return self._neustart

        def neu_aufnehmen() -> None:
            threading.Thread(target=self.wiederaufnehmen, args=(kreis,), kwargs={"erzwingen": True},
                             name=f"kreis-{kreis}-neu", daemon=True).start()

        return neu_aufnehmen

    def _thread_starten(self, kreis: int) -> None:
        ergebnis = self._ergebnisse[kreis]

        def lauf() -> None:
            try:
                ergebnis.worker.run()
            except WorkerBeendet:
                return
            except BaseException as fehler:  # noqa: BLE001 - auch Neustart und SystemExit gehen an den Aufseher
                self._ausfall.put(fehler)

        thread = threading.Thread(target=lauf, name=f"kreis-{kreis}", daemon=True)
        self._threads[kreis] = thread
        thread.start()

    def run(self) -> NoReturn:
        with self._sperre:
            self._laeuft = True
            for kreis in self._ergebnisse:
                self._thread_starten(kreis)
        raise self._ausfall.get()

    def wiederaufnehmen(self, kreis: int, *, erzwingen: bool = False) -> bool:
        """Nimmt den Kreis wieder auf, wenn er auf sein Abo wartet (`erzwingen`: in jedem Zustand, so startet der
        Kreis nach einer Neupruefung oder geaenderten Konfiguration): sein Worker endet, `app.start` laeuft fuer diesen
        Kreis neu (liest Abo und Konfiguration frisch). False, wenn der Kreis nichts abwartet, schon aufgenommen wird
        oder der Neustart scheitert. Scheitert er, nachdem der alte Worker beendet ist, hat der Kreis keine Regelung mehr:
        der Ausfall geht an den Aufseher (Neustart des Prozesses). Warten auf den Worker und `app.start` laufen
        ausserhalb der Sperre, damit die anderen Kreise (`register`, `post`, `kreise`) erreichbar bleiben."""
        with self._sperre:
            alt = self._ergebnisse.get(kreis)
            if alt is None or kreis in self._in_aufnahme or not (erzwingen or ruhend(alt)):
                return False
            self._in_aufnahme.add(kreis)
            thread = self._threads.get(kreis)
        try:
            alt.worker.beenden()
            if thread is not None:
                thread.join(ABWICKLUNG_SECONDS)
                if thread.is_alive():
                    self._ausfall.put(RuntimeError("Worker eines Kreises endet nicht"))
                    return False
            try:
                _anhalten(alt)
                neu = self._starte_kreis(kreis)
            except BaseException as fehler:  # noqa: BLE001 - ohne Worker regelt der Kreis nicht: Prozess neu starten
                logger.exception("Kreis %s konnte die Arbeit nicht wieder aufnehmen", kreis)
                self._ausfall.put(fehler)
                return False
            with self._sperre:  # Handler und Eintrag zusammen: ein gleichzeitiges `register` erreicht den neuen Kreis
                self.worker.anwenden(neu.worker)
                self._ergebnisse[kreis] = neu
                self._melden_gestartet(kreis, neu)
                if self._laeuft:
                    self._thread_starten(kreis)
            logger.info("Kreis %s nimmt die Arbeit wieder auf", kreis)
            return True
        finally:
            with self._sperre:
                self._in_aufnahme.discard(kreis)

    def prozessneustart_noetig(self, alt, neu) -> bool:
        """True, wenn eine neue Konfiguration den Prozessneustart verlangt: anderer Kreissatz, geaendertes
        anlagenweites Laufzeitfeld, bei einem einzigen Kreis jede laufzeitrelevante Aenderung. Reine Frage ohne
        Wirkung, damit der Host den Neustart gleich anfordern kann."""
        nummern = set(self.kreise())
        if nummern != set(_nummern(neu)) or dokument_config.laufzeit_relevant_anlage(alt, neu):
            return True
        return not self._mehrere and any(
            not dokument_config.abo_wiederaufnahme(alt, neu, kreis)
            and dokument_config.laufzeit_relevant_kreis(alt, neu, kreis) for kreis in nummern)

    def konfiguration_geaendert(self, alt, neu) -> bool:
        """Ein neues Konfigurationsdokument. True, wenn der Host trotzdem den Prozess neu starten muss
        (`prozessneustart_noetig`). Sonst nimmt jeder Kreis mit wieder aktivem Abo die Arbeit auf, und ein Kreis mit
        laufzeitrelevant geaendertem eigenem Eintrag startet allein neu; die anderen Kreise regeln durch."""
        if self.prozessneustart_noetig(alt, neu):
            return True
        for kreis in sorted(self.kreise()):
            if dokument_config.abo_wiederaufnahme(alt, neu, kreis):
                self.wiederaufnehmen(kreis)
            elif dokument_config.laufzeit_relevant_kreis(alt, neu, kreis):
                self.wiederaufnehmen(kreis, erzwingen=True)
        return False


def _nummern(konfiguration) -> list[int]:
    return [k for eintrag in kreisansicht.kreise(konfiguration) if isinstance(k := eintrag.get("kreis"), int)]


def start(host: AnlagenHost, clock: Callable[[], float], *, mqtt_factory, neustart: Callable[[], None],
          gestartet: Callable[[int, Runtime | IdleBridge], None] | None = None) -> Laufzeit:
    """Startet jeden Kreis des Hosts (`app.start`) und gibt die Laufzeit der Anlage zurueck; die Worker laufen erst in
    `Laufzeit.run`. `gestartet(kreis, ergebnis)` bindet den Geraetekern an den Worker des Kreises (auch nach einer
    Wiederaufnahme, mit dem neuen Worker)."""
    return Laufzeit(host, clock, mqtt_factory=mqtt_factory, neustart=neustart, gestartet=gestartet).starten()
